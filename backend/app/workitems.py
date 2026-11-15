"""Standing goals: a Kanban board of work items that agents keep working until they're done.

A **goal** is an outcome with a definition of done (a GTD project). It is broken into
**next actions** (child work items). The board is the agent's external memory: every goal
turn starts with the card itself (objective, done criteria, actions, recent log), so the
agent never has to reconstruct the original problem from a long conversation.

Columns: backlog -> ready -> in_progress <-> waiting -> review -> done (+ blocked, dropped).

- **You decide what gets worked.** Backlog is captured work; moving a goal to Ready says
  "yes, do this". An agent pulls from Ready (never from Backlog) when it has a free slot.
- **WIP limits.** Each agent works on at most `AgentDef.goal_wip` goals at once (in
  progress, waiting or blocked all count), and on one next action per goal at a time.
- **The runner** (API process, every AHQ_GOALS_TICK seconds) keeps goals moving: when a
  goal is in progress and nothing is running in its conversation, it queues a goal turn.
  Background delegations, approvals, desktop hand-offs, "wait for the user" and "check
  back in N minutes" show as Waiting and resume on their own.
- **Guards.** A goal is blocked when it hits its limits (turns, hours, model requests) or
  goes `stall_turns` turns without changing anything on the board. Stop in its
  conversation blocks it too. You resume it from the board.
- **Done is your call.** The agent claims done with evidence; you confirm or send it back
  with a note (Requests -> Goals).
- **Agents propose goals** (they land in Backlog, flagged, until you approve them).
- **Self-forming teams.** An agent allowed sub-agents, with a team size above 1, hands
  startable next actions to sub-agents (assign_action) that work them in parallel while it
  works its own. Actions name what they depend on (`after`), so serial work stays serial.
  A sub-agent's result lands on its action: done completes it, partial or failed blocks it
  for the lead to re-plan. Team size, sub-agent limits and approvals all still apply.

The same rows will carry incidents later (kind="incident", grouped by correlation_key).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic_ai import FunctionToolset, RunContext
from sqlalchemy import func
from sqlmodel import col, select

from . import db
from .config import settings
from .models import (AgentDef, Job, JobKind, JobStatus, Message, Role, SubagentMode, Thread,
                     UsageRecord, WorkItem, WorkItemEvent, WorkStatus)

log = logging.getLogger(__name__)

GOAL = "goal"
ACTION = "action"

DEFAULT_LIMITS: dict[str, int] = {
    "max_turns": 40,       # goal turns the runner may queue
    "max_hours": 72,       # wall-clock hours since it was started
    "max_requests": 400,   # model requests by everyone working in its conversation
    "stall_turns": 3,      # goal turns in a row that change nothing on the board
}
LIMIT_RANGES = {"max_turns": (1, 1000), "max_hours": (1, 24 * 60), "max_requests": (10, 100000),
                "stall_turns": (1, 50)}
MAX_PROPOSALS_PER_AGENT = 5
LOG_IN_PROMPT = 12

# Goals that hold one of their agent's WIP slots.
HOLDS_SLOT = (WorkStatus.in_progress, WorkStatus.waiting, WorkStatus.blocked)
ACTIVE = (WorkStatus.in_progress, WorkStatus.waiting)
OPEN_ACTION = (WorkStatus.backlog, WorkStatus.ready, WorkStatus.in_progress, WorkStatus.waiting,
               WorkStatus.blocked)
LABELS = {WorkStatus.backlog: "to do", WorkStatus.ready: "to do", WorkStatus.in_progress: "in progress",
          WorkStatus.waiting: "waiting", WorkStatus.blocked: "blocked", WorkStatus.done: "done",
          WorkStatus.dropped: "dropped", WorkStatus.review: "review"}
# Board columns, for goals.
COLUMNS = {WorkStatus.backlog: "Backlog", WorkStatus.ready: "Ready", WorkStatus.in_progress: "In progress",
           WorkStatus.waiting: "Waiting", WorkStatus.review: "Review", WorkStatus.blocked: "Blocked",
           WorkStatus.done: "Done", WorkStatus.dropped: "Dropped"}


class WorkError(ValueError):
    """Something you or an agent asked for that the board doesn't allow (shown as is)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.utcoffset() is None else dt.astimezone(timezone.utc)


def limits_of(item: WorkItem) -> dict[str, int]:
    return {**DEFAULT_LIMITS, **{k: int(v) for k, v in (item.limits or {}).items() if k in DEFAULT_LIMITS}}


def check_limits(values: dict[str, Any] | None) -> dict[str, int]:
    """Only the limits that differ from the defaults are stored."""
    out: dict[str, int] = {}
    for k, v in (values or {}).items():
        if k not in LIMIT_RANGES or v in (None, ""):
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise WorkError(f"{k} must be a number") from None
        lo, hi = LIMIT_RANGES[k]
        if not lo <= n <= hi:
            raise WorkError(f"{k} must be between {lo} and {hi}")
        if n != DEFAULT_LIMITS[k]:
            out[k] = n
    return out


# --------------------------------------------------------------------------- reading

def get(item_id: str) -> WorkItem | None:
    with db.session() as s:
        return s.get(WorkItem, item_id)


def actions(goal_id: str) -> list[WorkItem]:
    with db.session() as s:
        return list(s.exec(select(WorkItem).where(WorkItem.parent_id == goal_id)
                           .order_by(WorkItem.position, WorkItem.created_at)).all())


def events(goal_id: str, limit: int = 200) -> list[WorkItemEvent]:
    """Newest last."""
    with db.session() as s:
        rows = s.exec(select(WorkItemEvent).where(WorkItemEvent.item_id == goal_id)
                      .order_by(col(WorkItemEvent.at).desc()).limit(limit)).all()
    return list(reversed(rows))


def goal_for_thread(thread_id: str | None) -> WorkItem | None:
    """The goal whose turns run in this conversation, if any."""
    if not thread_id:
        return None
    with db.session() as s:
        return s.exec(select(WorkItem).where(WorkItem.thread_id == thread_id,
                                             WorkItem.kind == GOAL)
                      .order_by(col(WorkItem.created_at).desc())).first()


PENDING = "pending"  # job_id of an action reserved for a sub-agent that's being created


def team_size(agent: AgentDef | None) -> int:
    """How many of a goal's actions this agent's team may work at once (its own one plus
    sub-agents). 1 unless sub-agents are allowed for it."""
    from . import subagents

    if agent is None or agent.is_subagent or subagents.mode_of(agent) == SubagentMode.off:
        return 1
    size = max(1, min(int(agent.goal_team_size or 1), 10))
    if subagents.mode_of(agent) == SubagentMode.allowed:
        # No bigger than its sub-agent limit allows at once (in "ask" mode, your grant decides).
        size = min(size, 1 + max(0, int(subagents.limits_of(agent).get("concurrent") or 0)))
    return size


def on_team(a: WorkItem) -> bool:
    """A sub-agent is working this action right now."""
    return a.status == WorkStatus.in_progress and bool(a.job_id)


def unmet(a: WorkItem, acts: list[WorkItem]) -> list[WorkItem]:
    """The actions `a` depends on that aren't done yet (dropped ones don't hold it up)."""
    by_id = {x.id: x for x in acts}
    return [by_id[d] for d in (a.depends_on or [])
            if d in by_id and by_id[d].status not in (WorkStatus.done, WorkStatus.dropped)]


def startable(a: WorkItem, acts: list[WorkItem]) -> bool:
    return a.status == WorkStatus.backlog and not unmet(a, acts)


def lead_has_work(g: WorkItem) -> bool:
    """Is there anything for the goal's own agent to do while its team works? Its own
    action in progress, a blocked action to re-plan, or a startable one to take or assign."""
    acts = actions(g.id)
    for a in acts:
        if a.status == WorkStatus.in_progress and not a.job_id:
            return True
        if a.status == WorkStatus.blocked:
            return True
        if startable(a, acts):
            return True
    return not any(on_team(a) for a in acts)


def wip_used(agent_id: str) -> int:
    with db.session() as s:
        return s.exec(select(func.count()).select_from(WorkItem).where(
            WorkItem.owner_agent_id == agent_id, WorkItem.kind == GOAL,
            col(WorkItem.status).in_(HOLDS_SLOT))).one()


def attention_count() -> int:
    """Goals that need you: done claims to confirm, proposals, goals blocked by a guard,
    and goals waiting for your answer."""
    with db.session() as s:
        rows = s.exec(select(WorkItem).where(WorkItem.kind == GOAL, col(WorkItem.status).in_(
            [WorkStatus.review, WorkStatus.blocked, WorkStatus.waiting, WorkStatus.backlog]))).all()
    return sum(1 for g in rows if needs_you(g))


def needs_you(g: WorkItem) -> str | None:
    """Why this goal is waiting for you, or None."""
    w = g.waiting_on or {}
    if g.status == WorkStatus.review:
        return "review"
    if g.status == WorkStatus.backlog and g.proposed:
        return "proposal"
    if g.status == WorkStatus.blocked and w.get("kind") != "stopped":
        return "blocked"
    if g.status == WorkStatus.waiting and w.get("kind") == "user":
        return "question"
    return None


# --------------------------------------------------------------------------- writing

def _log(s, item_id: str, kind: str, text: str, actor: str, action_id: str | None = None,
         **data: Any) -> None:
    s.add(WorkItemEvent(item_id=item_id, action_id=action_id, actor=actor, kind=kind,
                        text=text, data=data))


def record(item_id: str, kind: str, text: str, actor: str, action_id: str | None = None,
           **data: Any) -> None:
    with db.session() as s:
        _log(s, item_id, kind, text, actor, action_id, **data)
        s.commit()


def _touch(item: WorkItem, *, progress: bool = False) -> None:
    item.updated_at = _now()
    if progress:
        item.revision += 1


def _next_position(s, parent_id: str | None, status: WorkStatus | None = None,
                   owner: str | None = None) -> float:
    q = select(func.max(WorkItem.position)).where(WorkItem.parent_id == parent_id)
    if status is not None:
        q = q.where(WorkItem.status == status)
    if owner is not None:
        q = q.where(WorkItem.owner_agent_id == owner)
    top = s.exec(q).one()
    return (top or 0) + 1


def create_goal(*, title: str, agent_id: str, objective: str = "", done_criteria: str = "",
                status: WorkStatus = WorkStatus.backlog, priority: int = 0,
                limits: dict[str, Any] | None = None, source: dict[str, Any] | None = None,
                proposed: bool = False, proposal_reason: str = "", actor: str = "you") -> WorkItem:
    title = " ".join((title or "").split())
    if not title:
        raise WorkError("give the goal a title")
    if not (objective or "").strip():
        raise WorkError("describe the objective: the outcome you want")
    if not (done_criteria or "").strip():
        raise WorkError("say how to tell it's done (the done criteria)")
    if status not in (WorkStatus.backlog, WorkStatus.ready):
        raise WorkError("a new goal starts in Backlog or Ready")
    if proposed and status != WorkStatus.backlog:
        raise WorkError("a proposed goal starts in Backlog")
    with db.session() as s:
        agent = s.get(AgentDef, agent_id)
        if agent is None or agent.is_subagent:
            raise WorkError("choose an agent for this goal")
        g = WorkItem(kind=GOAL, owner_agent_id=agent_id, title=title[:200],
                     objective=objective.strip(), done_criteria=done_criteria.strip(),
                     status=status, priority=int(priority or 0), limits=check_limits(limits),
                     source=source or {"kind": "user"}, proposed=proposed,
                     proposal_reason=(proposal_reason or "").strip())
        g.position = _next_position(s, None, status, agent_id)
        s.add(g)
        s.flush()
        _log(s, g.id, "created", ("Proposed: " + (proposal_reason or "").strip()) if proposed
             else f"Created in {COLUMNS[status]}.", actor)
        s.commit()
        s.refresh(g)
    return g


def update_goal(goal_id: str, data: dict[str, Any], *, actor: str = "you") -> WorkItem:
    """Your edits: title, objective, done criteria, priority, limits, owner (when idle)."""
    with db.session() as s:
        g = s.get(WorkItem, goal_id)
        if g is None or g.kind != GOAL:
            raise LookupError("no such goal")
        changed = []
        for k in ("title", "objective", "done_criteria"):
            if k in data and data[k] is not None:
                v = " ".join(str(data[k]).split()) if k == "title" else str(data[k]).strip()
                if not v:
                    raise WorkError(f"{k.replace('_', ' ')} can't be empty")
                if v != getattr(g, k):
                    setattr(g, k, v)
                    changed.append(k.replace("_", " "))
        if data.get("priority") is not None and int(data["priority"]) != g.priority:
            g.priority = int(data["priority"])
            changed.append("priority")
        if data.get("limits") is not None:
            g.limits = check_limits(data["limits"])
            changed.append("limits")
        if data.get("owner_agent_id") and data["owner_agent_id"] != g.owner_agent_id:
            if g.status in HOLDS_SLOT or g.status == WorkStatus.review:
                raise WorkError("a goal can change agent only while it's in Backlog or Ready")
            agent = s.get(AgentDef, data["owner_agent_id"])
            if agent is None or agent.is_subagent:
                raise WorkError("no such agent")
            g.owner_agent_id = agent.id
            g.thread_id = None  # its next start opens a conversation with the new agent
            changed.append(f"agent ({agent.name})")
        if changed:
            _touch(g, progress=bool({"objective", "done criteria"} & set(changed)))
            s.add(g)
            _log(s, g.id, "edited", "Changed " + ", ".join(changed) + ".", actor)
            s.commit()
            s.refresh(g)
    return g


def _ensure_thread(s, g: WorkItem) -> str:
    thread = s.get(Thread, g.thread_id) if g.thread_id else None
    if thread is not None and thread.agent_id == g.owner_agent_id and thread.deleted_at is None:
        return thread.id
    thread = Thread(agent_id=g.owner_agent_id, title=f"Goal: {g.title}"[:120])
    s.add(thread)
    s.flush()
    g.thread_id = thread.id
    return thread.id


def set_status(goal_id: str, status: WorkStatus, *, actor: str = "you", why: str = "",
               waiting_on: dict[str, Any] | None = None, force: bool = False,
               progress: bool = True) -> WorkItem:
    """Move a goal to another column, enforcing the board's rules."""
    with db.session() as s:
        g = s.get(WorkItem, goal_id)
        if g is None:
            raise LookupError("no such goal")
        old = g.status
        if old == status and status != WorkStatus.waiting:
            return g
        if g.proposed and status not in (WorkStatus.backlog, WorkStatus.dropped):
            raise WorkError("approve this proposed goal first (Requests → Goals)")
        if status == WorkStatus.in_progress and old not in HOLDS_SLOT and not force:
            agent = s.get(AgentDef, g.owner_agent_id)
            limit = agent.goal_wip if agent else 0
            if limit <= 0:
                raise WorkError(f"{agent.name if agent else 'its agent'} doesn't take goals "
                                "(its goal WIP limit is 0)")
            used = s.exec(select(func.count()).select_from(WorkItem).where(
                WorkItem.owner_agent_id == g.owner_agent_id, WorkItem.kind == GOAL,
                col(WorkItem.status).in_(HOLDS_SLOT))).one()
            if used >= limit:
                raise WorkError(f"{agent.name} is at its WIP limit ({limit} goal"
                                f"{'' if limit == 1 else 's'} in progress). Finish or move one, "
                                "or raise the limit under Settings → Agents.")
        if status == WorkStatus.in_progress:
            _ensure_thread(s, g)
            if g.started_at is None:
                g.started_at = _now()
            if old in (WorkStatus.blocked, WorkStatus.done, WorkStatus.dropped):
                g.stalled_turns = 0  # a limit is checked again on the next turn: raise it first
        if status in (WorkStatus.done, WorkStatus.dropped):
            g.finished_at = _now()
        elif old in (WorkStatus.done, WorkStatus.dropped):
            g.finished_at = None
        if status in (WorkStatus.backlog, WorkStatus.ready) and old != status:
            g.position = _next_position(s, None, status, g.owner_agent_id)
        g.status = status
        g.waiting_on = ({**(waiting_on or {}), "since": _now().isoformat()}
                        if status in (WorkStatus.waiting, WorkStatus.blocked) else None)
        _touch(g, progress=progress)
        s.add(g)
        text = f"{COLUMNS[old]} → {COLUMNS[status]}"
        _log(s, g.id, "status", text + (f": {why}" if why else ""), actor,
             old=old.value, new=status.value, **({"waiting_on": waiting_on} if waiting_on else {}))
        s.commit()
        s.refresh(g)
    return g


def move(goal_id: str, status: WorkStatus, *, position: float | None = None,
         actor: str = "you") -> WorkItem:
    """You moving a card on the board. Review and Waiting are the agent's (and runner's)."""
    if status in (WorkStatus.review, WorkStatus.waiting):
        raise WorkError("cards move to Review and Waiting by themselves")
    g = get(goal_id)
    if g is None or g.kind != GOAL:
        raise LookupError("no such goal")
    if g.status == WorkStatus.review and status == WorkStatus.done:
        raise WorkError("confirm the agent's done claim from the card instead (Approve)")
    if status != g.status:
        g = set_status(goal_id, status, actor=actor,
                       why="resumed by you" if status == WorkStatus.in_progress
                       and g.status in (WorkStatus.blocked, WorkStatus.waiting) else "")
    if position is not None:
        with db.session() as s:
            row = s.get(WorkItem, goal_id)
            row.position = float(position)
            s.add(row)
            s.commit()
            s.refresh(row)
            g = row
    return g


def decide_review(goal_id: str, approve: bool, note: str = "") -> WorkItem:
    g = get(goal_id)
    if g is None or g.kind != GOAL:
        raise LookupError("no such goal")
    if g.status != WorkStatus.review:
        raise WorkError("this goal isn't waiting for review")
    note = (note or "").strip()
    with db.session() as s:
        row = s.get(WorkItem, goal_id)
        row.review_note = "" if approve else note
        s.add(row)
        s.commit()
    if approve:
        return set_status(goal_id, WorkStatus.done, why=note or "confirmed by you")
    return set_status(goal_id, WorkStatus.in_progress, force=True,
                      why="sent back by you" + (f": {note}" if note else ""))


def decide_proposal(goal_id: str, approve: bool, *, ready: bool = False, note: str = "") -> WorkItem:
    with db.session() as s:
        g = s.get(WorkItem, goal_id)
        if g is None or g.kind != GOAL:
            raise LookupError("no such goal")
        if not g.proposed:
            raise WorkError("this goal isn't a proposal")
        g.proposed = False
        g.review_note = (note or "").strip()
        s.add(g)
        _log(s, g.id, "proposal", ("Approved" if approve else "Rejected")
             + (f": {note.strip()}" if (note or "").strip() else "."), "you")
        s.commit()
    if not approve:
        return set_status(goal_id, WorkStatus.dropped, why="proposal rejected")
    if ready:
        return set_status(goal_id, WorkStatus.ready)
    return get(goal_id)


# --------------------------------------------------------------------------- actions

def _goal_and_action(s, goal_id: str, action_id: str) -> tuple[WorkItem, WorkItem]:
    g = s.get(WorkItem, goal_id)
    a = s.get(WorkItem, action_id)
    if a is None or a.parent_id != goal_id:
        raise WorkError(f"no action {action_id} on this goal (see board_view for the IDs)")
    return g, a


def new_action(goal_id: str, title: str, details: str = "", *, actor: str,
               after: list[str] | None = None) -> WorkItem:
    title = " ".join((title or "").split())
    if not title:
        raise WorkError("give the action a title: the next physical, visible step")
    with db.session() as s:
        g = s.get(WorkItem, goal_id)
        deps: list[str] = []
        dep_titles: list[str] = []
        for d in after or []:
            d = (d or "").strip()
            dep = s.get(WorkItem, d) if d else None
            if dep is None or dep.parent_id != goal_id:
                raise WorkError(f"after: there's no action {d} on this goal (see board_view)")
            if d not in deps:
                deps.append(d)
                dep_titles.append(dep.title)
        open_n = s.exec(select(func.count()).select_from(WorkItem).where(
            WorkItem.parent_id == goal_id, col(WorkItem.status).in_(OPEN_ACTION))).one()
        if open_n >= 30:
            raise WorkError("this goal already has 30 open actions; finish or drop some first")
        a = WorkItem(kind=ACTION, parent_id=goal_id, owner_agent_id=g.owner_agent_id,
                     title=title[:200], objective=(details or "").strip(),
                     status=WorkStatus.backlog, position=_next_position(s, goal_id),
                     source={"kind": "agent" if actor != "you" else "user"}, depends_on=deps)
        s.add(a)
        s.flush()
        _touch(g, progress=True)
        s.add(g)
        _log(s, goal_id, "action_added", f"Added: {a.title}"
             + (f" (after {'; '.join(dep_titles)})" if deps else ""), actor, a.id)
        s.commit()
        s.refresh(a)
    return a


def set_action(goal_id: str, action_id: str, status: WorkStatus, *, actor: str,
               result: str = "") -> WorkItem:
    with db.session() as s:
        g, a = _goal_and_action(s, goal_id, action_id)
        if a.job_id and a.status == WorkStatus.in_progress and actor != "you":
            raise WorkError(f"a sub-agent on your team is working on '{a.title}'; its result "
                            "lands here by itself")
        if a.job_id and status != WorkStatus.in_progress:
            a.job_id = None  # you took it over: a late result from the sub-agent is ignored
        if status == WorkStatus.in_progress:
            acts = list(s.exec(select(WorkItem).where(WorkItem.parent_id == goal_id)).all())
            waiting_on = unmet(a, acts)
            if waiting_on:
                raise WorkError(f"'{a.title}' comes after " + ", ".join(
                    f"'{d.title}' ({d.id})" for d in waiting_on) + ", which isn't done yet")
            busy = next((x for x in acts if x.id != action_id and x.status == WorkStatus.in_progress
                         and not x.job_id), None)
            if busy is not None:
                raise WorkError(f"one action at a time for yourself: finish, block or drop "
                                f"'{busy.title}' ({busy.id}) first"
                                + (" (or assign_action other startable ones to your team)"
                                   if team_size(s.get(AgentDef, g.owner_agent_id)) > 1 else ""))
            a.assignee_agent_id = None
            a.started_at = a.started_at or _now()
        if status in (WorkStatus.done, WorkStatus.dropped):
            a.finished_at = _now()
        if a.status in (WorkStatus.done, WorkStatus.dropped) and status != a.status:
            a.finished_at = None
        old = a.status
        a.status = status
        if result:
            a.result = result.strip()
        _touch(a)
        _touch(g, progress=True)
        s.add(a)
        s.add(g)
        verb = {WorkStatus.in_progress: "Started", WorkStatus.done: "Done",
                WorkStatus.blocked: "Blocked", WorkStatus.dropped: "Dropped",
                WorkStatus.backlog: "Reopened"}.get(status, LABELS.get(status, status))
        _log(s, goal_id, "action", f"{verb}: {a.title}" + (f" — {result.strip()}" if result else ""),
             actor, a.id, old=old.value, new=status.value)
        s.commit()
        s.refresh(a)
    return a


# --------------------------------------------------------------------------- the team

def reserve_for_team(goal_id: str, action_id: str, *, actor: str) -> WorkItem:
    """Hold a startable action for a sub-agent that's about to be created, if the team has
    a free slot. release_team() undoes it when the sub-agent can't be created."""
    with db.session() as s:
        g, a = _goal_and_action(s, goal_id, action_id)
        agent = s.get(AgentDef, g.owner_agent_id)
        size = team_size(agent)
        if size <= 1:
            raise WorkError("you have no team for goals (team size 1, or sub-agents are off)")
        acts = list(s.exec(select(WorkItem).where(WorkItem.parent_id == goal_id)).all())
        if a.status != WorkStatus.backlog:
            raise WorkError(f"'{a.title}' is {LABELS[a.status]}; only to-do actions can be assigned")
        waiting_on = unmet(a, acts)
        if waiting_on:
            raise WorkError(f"'{a.title}' comes after " + ", ".join(
                f"'{d.title}' ({d.id})" for d in waiting_on) + "; assign it once that's done")
        working = [x for x in acts if on_team(x)]
        if len(working) >= size - 1:
            raise WorkError(f"your team is full: {len(working)} sub-agent(s) working (team size "
                            f"{size} = you + {size - 1}). Work one yourself or wait for a result.")
        a.status, a.job_id, a.started_at = WorkStatus.in_progress, PENDING, _now()
        s.add(a)
        s.commit()
        s.refresh(a)
    return a


def release_team(goal_id: str, action_id: str) -> None:
    with db.session() as s:
        a = s.get(WorkItem, action_id)
        if a is not None and a.job_id == PENDING:
            a.status, a.job_id, a.started_at = WorkStatus.backlog, None, None
            s.add(a)
            s.commit()


def assigned(goal_id: str, action_id: str, sub: AgentDef, job_id: str, *, actor: str) -> None:
    with db.session() as s:
        g, a = _goal_and_action(s, goal_id, action_id)
        if a.job_id == PENDING:
            a.job_id = job_id
        a.assignee_agent_id = sub.id
        _touch(a)
        _touch(g, progress=True)
        s.add(a)
        s.add(g)
        _log(s, goal_id, "action", f"Assigned to {sub.name}: {a.title}", actor, a.id,
             subagent=sub.name, job_id=job_id)
        s.commit()


def team_task(g: WorkItem, a: WorkItem, lead: AgentDef, instructions: str) -> str:
    """What a team sub-agent is told: the goal, its one action, and what the team found so
    far that it builds on."""
    acts = actions(g.id)
    before = [x for x in acts if x.id in (a.depends_on or []) and x.result]
    others = [x for x in acts if x.status == WorkStatus.done and x.result and x not in before][-5:]
    found = "\n".join(f"- {x.title}: {x.result[:600]}" for x in before + others)
    return (
        f"You're on {lead.name}'s team, working one next action of a standing goal.\n\n"
        f"Goal: {g.title}\nObjective: {g.objective}\n\n"
        f"Your action: {a.title}\n" + (f"Details: {a.objective}\n" if a.objective else "")
        + (f"\nHow to do it ({lead.name}'s instructions):\n{instructions.strip()}\n" if instructions.strip() else "")
        + (f"\nWhat the team has found so far:\n{found}\n" if found else "")
        + "\nDo only this action; other team members are working on other parts of the goal at "
        "the same time, so don't change anything outside it. Return a WorkResult: status done "
        "if you finished it, partial or failed with the reason if not. Put what you found or did "
        "in summary (it goes on the board) and supporting detail in details.")


def team_result(job: Job, status: str, summary: str, details: str = "") -> None:
    """A team sub-agent finished: its result goes on its action. Done completes it;
    partial or failed blocks it for the goal's agent to re-plan."""
    action_id = (job.meta or {}).get("goal_action")
    with db.session() as s:
        a = s.get(WorkItem, action_id) if action_id else None
        if a is None:
            return
        g = s.get(WorkItem, a.parent_id)
        sub = s.get(AgentDef, job.assignee_agent_id)
        who = sub.name if sub else "a sub-agent"
        if a.status != WorkStatus.in_progress or a.job_id not in (job.id, PENDING):
            _log(s, g.id, "note", f"{who} reported on '{a.title}' after it was taken over; "
                 f"result not applied: {summary[:300]}", "runner", a.id)
            s.commit()
            return
        text = summary.strip() + (f"\n{details.strip()[:1500]}" if details.strip() else "")
        a.result = text[:4000]
        a.job_id = None
        if status == "done":
            a.status, a.finished_at = WorkStatus.done, _now()
            verb = "Done"
        else:
            a.status = WorkStatus.blocked
            verb = "Blocked" if status == "failed" else "Partly done, blocked"
        _touch(a)
        _touch(g, progress=True)
        s.add(a)
        s.add(g)
        _log(s, g.id, "action", f"{verb}: {a.title} — {summary.strip()[:500]}", who, a.id,
             old="in_progress", new=a.status.value, team=True)
        s.commit()


def team_stopped(job: Job) -> None:
    """A team sub-agent was stopped (Stop, or the goal was dropped): its action goes back
    to do."""
    action_id = (job.meta or {}).get("goal_action")
    with db.session() as s:
        a = s.get(WorkItem, action_id) if action_id else None
        if a is None or a.job_id not in (job.id, PENDING):
            return
        a.status, a.job_id, a.started_at = WorkStatus.backlog, None, None
        s.add(a)
        _log(s, a.parent_id, "action", f"Stopped: {a.title} (back to do)", "runner", a.id)
        s.commit()


# --------------------------------------------------------------------------- the card

def _ago(dt: datetime | None) -> str:
    if dt is None:
        return ""
    secs = int((_now() - aware(dt)).total_seconds())
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs // 60} min ago"
    if secs < 129600:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def card(g: WorkItem, *, log_lines: int = LOG_IN_PROMPT) -> dict[str, Any]:
    """The goal as the agent sees it (board_view)."""
    acts = actions(g.id)
    lim = limits_of(g)
    with db.session() as s:
        agent = s.get(AgentDef, g.owner_agent_id) if g.owner_agent_id else None
        names = {x.id: x.name for x in s.exec(select(AgentDef).where(col(AgentDef.id).in_(
            [a.assignee_agent_id for a in acts if a.assignee_agent_id]))).all()} if acts else {}
    size = team_size(agent)

    def one(a: WorkItem) -> dict[str, Any]:
        out: dict[str, Any] = {"id": a.id, "title": a.title, "status": LABELS[a.status]}
        if a.objective:
            out["details"] = a.objective
        if a.depends_on:
            out["after"] = list(a.depends_on)
            if a.status == WorkStatus.backlog and unmet(a, acts):
                out["waiting_for"] = [d.id for d in unmet(a, acts)]
        if on_team(a):
            out["worked_by"] = names.get(a.assignee_agent_id, "a sub-agent (starting)")
        elif a.assignee_agent_id and a.status in (WorkStatus.done, WorkStatus.blocked):
            out["by"] = names.get(a.assignee_agent_id, "a sub-agent")
        if a.result:
            out["result"] = a.result
        return out

    return {
        "goal_id": g.id, "title": g.title, "status": g.status.value,
        "objective": g.objective, "done_criteria": g.done_criteria,
        "sent_back_with": g.review_note or None,
        "actions": [one(a) for a in acts],
        "current_action": next((a.id for a in acts if a.status == WorkStatus.in_progress
                                and not a.job_id), None),
        **({"team": {"size": size, "working": sum(1 for a in acts if on_team(a)),
                     "free_slots": max(0, size - 1 - sum(1 for a in acts if on_team(a)))}}
           if size > 1 else {}),
        "log": [f"{_ago(e.at)} · {e.actor}: {e.text}" for e in events(g.id, log_lines)],
        "turn": g.turns, "turn_limit": lim["max_turns"],
    }


def card_prompt(g: WorkItem) -> str:
    c = card(g)

    def line(a: dict) -> str:
        tag = a["status"]
        if a.get("worked_by"):
            tag += f" · {a['worked_by']} (sub-agent)"
        elif a.get("waiting_for"):
            tag += " · waiting for " + ", ".join(a["waiting_for"])
        elif a.get("by"):
            tag += f" · by {a['by']}"
        return (f"- [{tag}] {a['id']}: {a['title']}"
                + (f" (after {', '.join(a['after'])})" if a.get("after") and not a.get("waiting_for") else "")
                + (f" — {a['result']}" if a.get("result") else ""))

    acts = "\n".join(line(a) for a in c["actions"]) \
        or "- (none yet: break the goal into next actions with add_action)"
    log_text = "\n".join(f"- {line}" for line in c["log"]) or "- (nothing yet)"
    sent_back = (f"\nThe user sent your last done claim back: {c['sent_back_with']}\n"
                 if c["sent_back_with"] else "")
    return (
        f"[Goal turn {g.turns} of up to {c['turn_limit']}: \"{g.title}\" ({g.id})]\n"
        "You own this standing goal. This card is your memory of it: trust it over your "
        "memory of earlier turns. The goal runner started this turn; the user didn't type it.\n\n"
        f"Objective:\n{g.objective}\n\nDone when:\n{g.done_criteria}\n{sent_back}\n"
        f"Next actions:\n{acts}\n\nRecent log:\n{log_text}\n\n"
        "This turn:\n"
        "1. If an action is in progress, work on it. Finish it with complete_action (say what you "
        "found or did), or block_action / drop_action with the reason.\n"
        "2. If none is in progress, start the next one with start_action, or add the next "
        "actions with add_action first. One action at a time for yourself; when you add an "
        "action that can't start until others are done, list them in after.\n"
        + (team_text(c["team"]) if c.get("team") else "") +
        "3. Use your tools and your team as usual. If you need the user (a decision, access, "
        "credentials), call wait_for(kind=\"user\"). To check back later, wait_for(kind=\"time\").\n"
        "4. When the done criteria are met, call claim_done with the evidence. Never change the "
        "objective: if it's wrong or impossible, say so with log_note and wait_for the user.\n"
        "End with one or two sentences on where the goal stands.")


def team_text(team: dict) -> str:
    free = team["free_slots"]
    return (
        f"2b. Your team: up to {team['size'] - 1} sub-agent(s) can work other startable actions "
        f"in parallel while you work yours ({team['working']} working, {free} free). Hand one "
        "over with assign_action: only actions with nothing unfinished in their after list, "
        "never two at once that change the same system, and never work an existing agent "
        "should do (delegate that to it instead). Pick a cloud model for them if the "
        "local GPU is busy. Their results land on the board by themselves; a blocked one needs "
        "you to re-plan it. Sub-agents have no desktop: keep visual browsing for yourself.\n")


GOAL_RULES = (
    "## Standing goal\n"
    "This conversation belongs to a standing goal on the board. Work it one next action at a "
    "time with the goal tools (board_view, add_action, start_action, complete_action, "
    "block_action, drop_action, log_note, wait_for, claim_done). The board, not this "
    "conversation, is the record: put results there, so the next turn knows what happened.")

PROPOSE_RULES = (
    "## Proposing goals\n"
    "When you find work that needs many steps over time and isn't what you were asked to do "
    "now (a recurring problem to fix properly, a cleanup, documentation to write), you can "
    "suggest it with propose_goal. It goes to the user's backlog for approval; don't start "
    "it yourself.")


# --------------------------------------------------------------------------- waiting hooks

def mark_waiting(thread_id: str | None, kind: str, detail: str = "") -> None:
    """A turn in a goal's conversation is waiting on an approval or a desktop hand-off.
    Shown on the board; the runner sets it back when nothing is waiting any more."""
    g = goal_for_thread(thread_id)
    if g is None or g.status != WorkStatus.in_progress:
        return
    try:
        set_status(g.id, WorkStatus.waiting, actor="runner", why=detail,
                   waiting_on={"kind": kind, "detail": detail}, progress=False)
    except Exception:
        log.exception("couldn't mark goal %s waiting", g.id)


def clear_waiting(thread_id: str | None, kind: str) -> None:
    g = goal_for_thread(thread_id)
    if g is None or g.status != WorkStatus.waiting or (g.waiting_on or {}).get("kind") != kind:
        return
    try:
        set_status(g.id, WorkStatus.in_progress, actor="runner", force=True, progress=False)
    except Exception:
        log.exception("couldn't resume goal %s", g.id)


def stopped(thread_id: str) -> None:
    """You pressed Stop in a goal's conversation: block the goal so the runner doesn't
    start it again. Resume it from the board."""
    g = goal_for_thread(thread_id)
    if g is None or g.status not in ACTIVE:
        return
    set_status(g.id, WorkStatus.blocked, actor="you", why="stopped by you",
               waiting_on={"kind": "stopped", "detail": "Stopped by you."}, progress=False)


# --------------------------------------------------------------------------- the runner

def _active_jobs(thread_id: str) -> list[Job]:
    with db.session() as s:
        return list(s.exec(select(Job).where(
            Job.origin_thread_id == thread_id,
            col(Job.status).in_([JobStatus.queued, JobStatus.running]))).all())


def _unreported(thread_id: str, job_ids: list[str]) -> bool:
    """True while a background task we're waiting on hasn't finished, or has finished but
    its report turn isn't queued yet (the report is what tells the agent the result)."""
    if not job_ids:
        return False
    with db.session() as s:
        tasks = s.exec(select(Job).where(col(Job.id).in_(job_ids))).all()
        reports = {(j.meta or {}).get("report_for") for j in s.exec(select(Job).where(
            Job.origin_thread_id == thread_id, Job.kind == JobKind.turn,
            Job.created_at >= min(t.created_at for t in tasks) if tasks else _now())).all()}
    for t in tasks:
        if t.status in (JobStatus.queued, JobStatus.running):
            return True
        if t.background and t.error != "Stopped by you." and t.id not in reports:
            return True
    return False


def _requests_used(g: WorkItem) -> int:
    if not g.thread_id:
        return 0
    with db.session() as s:
        q = select(func.coalesce(func.sum(UsageRecord.requests), 0)).where(
            UsageRecord.thread_id == g.thread_id)
        if g.started_at:
            q = q.where(UsageRecord.at >= g.started_at)
        return int(s.exec(q).one() or 0)


def _user_spoke_since(thread_id: str, since: datetime) -> bool:
    with db.session() as s:
        return s.exec(select(Message.id).where(Message.thread_id == thread_id,
                                               Message.role == Role.user,
                                               Message.created_at > since)).first() is not None


async def _notify(rt, kind: str, g: WorkItem, **extra: Any) -> None:
    from . import subagents

    with db.session() as s:
        agent = s.get(AgentDef, g.owner_agent_id) if g.owner_agent_id else None
    try:
        await subagents.notify({"type": kind, "goal_id": g.id, "title": g.title,
                                "agent": agent.name if agent else None, **extra,
                                "link": subagents.link(f"/board?item={g.id}")})
    except Exception:
        log.warning("goal notification failed", exc_info=True)


async def _say(rt, g: WorkItem, text: str, **meta: Any) -> None:
    if g.thread_id:
        await rt.save_message(g.thread_id, Role.event, text, agent_id=g.owner_agent_id,
                              kind="goal", goal_id=g.id, **meta)


async def _block(rt, g: WorkItem, kind: str, why: str) -> None:
    g = set_status(g.id, WorkStatus.blocked, actor="runner", why=why,
                   waiting_on={"kind": kind, "detail": why}, progress=False)
    await _say(rt, g, f"Goal blocked: {why} Resume it from the board when you're ready.",
               status="blocked")
    await _notify(rt, "goal_blocked", g, reason=why)


def pull(agent_id: str) -> list[WorkItem]:
    """Start Ready goals for this agent while it has free WIP slots (highest priority,
    then board order). Never pulls from Backlog."""
    started: list[WorkItem] = []
    with db.session() as s:
        agent = s.get(AgentDef, agent_id)
    if agent is None or not agent.enabled or agent.is_subagent or agent.goal_wip <= 0:
        return started
    while wip_used(agent_id) < agent.goal_wip:
        with db.session() as s:
            nxt = s.exec(select(WorkItem).where(
                WorkItem.owner_agent_id == agent_id, WorkItem.kind == GOAL,
                WorkItem.status == WorkStatus.ready, WorkItem.proposed == False)  # noqa: E712
                .order_by(col(WorkItem.priority).desc(), WorkItem.position,
                          WorkItem.created_at)).first()
        if nxt is None:
            break
        started.append(set_status(nxt.id, WorkStatus.in_progress, actor="runner",
                                  why="pulled from Ready (a WIP slot was free)"))
    return started


async def _queue_turn(rt, g: WorkItem) -> Job:
    with db.session() as s:
        row = s.get(WorkItem, g.id)
        row.turns += 1
        row.turn_revision = row.revision
        s.add(row)
        s.commit()
        s.refresh(row)
    job = await rt.enqueue_turn(row.thread_id, card_prompt(row), from_user=False,
                                event_text=f"Goal turn {row.turns}: {row.title}",
                                kind="goal", goal_id=row.id)
    with db.session() as s:
        row = s.get(WorkItem, g.id)
        row.last_job_id = job.id
        s.add(row)
        s.commit()
    return job


async def advance(rt, g: WorkItem) -> str:
    """One look at one active goal. Returns what happened (for tests and logs)."""
    if g.thread_id is None:
        with db.session() as s:
            row = s.get(WorkItem, g.id)
            _ensure_thread(s, row)
            s.add(row)
            s.commit()
            s.refresh(row)
            g = row
    active = _active_jobs(g.thread_id)
    team_jobs = [j for j in active if (j.meta or {}).get("goal_action")]
    jobs = [j for j in active if not (j.meta or {}).get("goal_action")]
    w = g.waiting_on or {}
    now = _now()

    if g.status == WorkStatus.waiting:
        kind = w.get("kind")
        if kind == "time":
            until = datetime.fromisoformat(w["until"]) if w.get("until") else now
            if aware(until) > now:
                return "waiting: time"
        elif kind == "user":
            since = datetime.fromisoformat(w["since"]) if w.get("since") else now
            if not _user_spoke_since(g.thread_id, since):
                return "waiting: user"
        elif kind == "team":
            if team_jobs and not lead_has_work(g):
                return "waiting: team"
        elif jobs:
            return f"waiting: {kind}"
        elif kind == "delegation" and _unreported(g.thread_id, w.get("job_ids") or []):
            return "waiting: delegation (report on its way)"
        # A delegate's result or your answer counts as progress; the clock running out doesn't.
        g = set_status(g.id, WorkStatus.in_progress, actor="runner", force=True,
                       progress=kind in ("delegation", "user", "team"),
                       why={"time": "time to check back", "user": "you answered",
                            "delegation": "the delegated work finished",
                            "team": "a team result came in"}.get(kind, ""))
        if kind == "user":
            jobs = _active_jobs(g.thread_id)  # your reply is a turn of its own

    if jobs:
        if all(j.kind == JobKind.task and j.background for j in jobs):
            set_status(g.id, WorkStatus.waiting, actor="runner", progress=False,
                       why="a delegated task is running in the background",
                       waiting_on={"kind": "delegation", "job_ids": [j.id for j in jobs]})
            return "waiting: delegation"
        return "busy"
    if team_jobs and not lead_has_work(g):
        set_status(g.id, WorkStatus.waiting, actor="runner", progress=False,
                   why=f"{len(team_jobs)} sub-agent(s) on its team are working",
                   waiting_on={"kind": "team", "job_ids": [j.id for j in team_jobs]})
        return "waiting: team"
    # (Team members still working while the lead has work of its own: the lead gets turns.)

    # A background task that finished a moment ago: wait for its report turn first. Team
    # sub-agents have no report turn: their results go straight onto the board.
    with db.session() as s:
        recent = [j.id for j in s.exec(select(Job).where(
            Job.origin_thread_id == g.thread_id, Job.kind == JobKind.task,
            Job.background == True,  # noqa: E712
            Job.finished_at >= now - timedelta(minutes=2))).all()
            if not (j.meta or {}).get("goal_action")]
    if recent and _unreported(g.thread_id, recent):
        return "busy: report on its way"

    # Nothing running: account for the last goal turn, check the guards, queue the next.
    lim = limits_of(g)
    if g.last_job_id:
        with db.session() as s:
            last = s.get(Job, g.last_job_id)
        if last is not None and last.status in (JobStatus.done, JobStatus.failed):
            moved = g.revision != g.turn_revision
            with db.session() as s:
                row = s.get(WorkItem, g.id)
                row.stalled_turns = 0 if moved else row.stalled_turns + 1
                row.turn_revision = row.revision
                row.last_job_id = None
                s.add(row)
                if last.status == JobStatus.failed:
                    _log(s, g.id, "error", f"The goal turn failed: {(last.error or '')[:300]}",
                         "runner")
                s.commit()
                s.refresh(row)
                g = row
            if g.status != WorkStatus.in_progress:
                return "moved"  # the turn claimed done, waited, or was stopped
    if g.stalled_turns >= lim["stall_turns"]:
        await _block(rt, g, "stalled", f"No progress on the board in {g.stalled_turns} goal "
                     "turns in a row.")
        return "blocked: stalled"
    if g.turns >= lim["max_turns"]:
        await _block(rt, g, "limit", f"It reached its limit of {lim['max_turns']} goal turns.")
        return "blocked: turns"
    if g.started_at and now - aware(g.started_at) > timedelta(hours=lim["max_hours"]):
        await _block(rt, g, "limit", f"It has been running longer than its limit of "
                     f"{lim['max_hours']} hours.")
        return "blocked: hours"
    used = _requests_used(g)
    if used >= lim["max_requests"]:
        await _block(rt, g, "limit", f"It used {used} model requests (limit "
                     f"{lim['max_requests']}).")
        return "blocked: requests"
    await _queue_turn(rt, g)
    return "queued"


async def tick(rt) -> dict[str, str]:
    """Pull Ready goals into free WIP slots, then move every active goal along once."""
    out: dict[str, str] = {}
    with db.session() as s:
        agents = s.exec(select(AgentDef.id).where(AgentDef.enabled == True,  # noqa: E712
                                                  AgentDef.is_subagent == False,  # noqa: E712
                                                  AgentDef.goal_wip > 0)).all()
    for agent_id in agents:
        try:
            for g in pull(agent_id):
                out[g.id] = "started"
        except Exception:
            log.exception("pulling goals for agent %s failed", agent_id)
    with db.session() as s:
        goals = s.exec(select(WorkItem).where(WorkItem.kind == GOAL,
                                              col(WorkItem.status).in_(ACTIVE))).all()
    for g in goals:
        # One look per goal at a time, however many API processes run the runner.
        key = f"ahq:goal:{g.id}:tick"
        if not await rt.broker.r.set(key, "1", nx=True, ex=60):
            continue
        try:
            with db.session() as s:
                agent = s.get(AgentDef, g.owner_agent_id)
            if agent is None or not agent.enabled:
                out[g.id] = "agent disabled"
                continue
            out[g.id] = await advance(rt, get(g.id))
        except Exception:
            log.exception("goal %s: runner step failed", g.id)
            out[g.id] = "error"
        finally:
            await rt.broker.r.delete(key)
    return out


async def runner_loop(rt, tick_seconds: float | None = None) -> None:
    every = tick_seconds or settings.goals_tick
    log.info("goal runner running (checks every %ss)", every)
    while True:
        try:
            await tick(rt)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("goal runner check failed")
        await asyncio.sleep(every)


# --------------------------------------------------------------------------- agent tools

def _mine(ctx: RunContext) -> tuple[WorkItem | None, AgentDef | None, dict | None]:
    from .runtime import load_agent

    me = load_agent(ctx.deps.agent_id)
    g = goal_for_thread(ctx.deps.thread_id)
    if g is None or g.owner_agent_id != me.id or ctx.deps.depth != 0:
        return None, me, {"error": "this conversation has no goal of yours"}
    if g.status not in ACTIVE:
        return None, me, {"error": f"the goal is {COLUMNS[g.status]}; there's nothing to do on it now"}
    return g, me, None


def goal_toolset(rt, team: bool = False) -> FunctionToolset:
    """Tools for the agent working a goal (only in that goal's conversation). With a team,
    also assign_action."""

    def _err(e: Exception) -> dict:
        return {"error": str(e)}

    def board_view(ctx: RunContext) -> dict:
        """Your goal's card: objective, done criteria, next actions with their IDs, and the
        recent log."""
        g, _, err = _mine(ctx)
        return err or card(g)

    def add_action(ctx: RunContext, title: str, details: str = "",
                   after: list[str] | None = None) -> dict:
        """Add a next action to your goal (to do). Make it a concrete, visible step.

        Args:
            title: The step, e.g. "List VLANs on the core switch (crs-326)".
            details: Optional notes: how, what to check, what to bring back.
            after: IDs of actions that must be done before this one can start (it needs
                their results, or would clash with them). Leave empty if it can start now.
        """
        g, me, err = _mine(ctx)
        if err:
            return err
        try:
            a = new_action(g.id, title, details, actor=me.name, after=after)
        except WorkError as e:
            return _err(e)
        return {"ok": True, "action_id": a.id,
                "hint": "start_action it when it's next" if not a.depends_on
                else f"it can start once {', '.join(a.depends_on)} are done"}

    def start_action(ctx: RunContext, action_id: str) -> dict:
        """Start an action. Only one action can be in progress at a time."""
        g, me, err = _mine(ctx)
        if err:
            return err
        try:
            a = set_action(g.id, action_id, WorkStatus.in_progress, actor=me.name)
        except WorkError as e:
            return _err(e)
        return {"ok": True, "in_progress": a.title}

    def complete_action(ctx: RunContext, action_id: str, result: str) -> dict:
        """Finish an action, saying what you found or did (it's kept on the board).

        Args:
            action_id: The action's ID.
            result: What came of it: findings, what you changed, links, numbers.
        """
        g, me, err = _mine(ctx)
        if err:
            return err
        if not (result or "").strip():
            return {"error": "say what came of it in result"}
        try:
            set_action(g.id, action_id, WorkStatus.done, actor=me.name, result=result)
        except WorkError as e:
            return _err(e)
        left = [a for a in actions(g.id) if a.status in OPEN_ACTION]
        return {"ok": True, "open_actions": len(left),
                "next": "start the next action" if left else
                "no actions left: claim_done if the done criteria are met, else add the next action"}

    def block_action(ctx: RunContext, action_id: str, reason: str) -> dict:
        """Mark an action blocked (it can't go on as it is), with the reason."""
        g, me, err = _mine(ctx)
        if err:
            return err
        try:
            set_action(g.id, action_id, WorkStatus.blocked, actor=me.name, result=reason)
        except WorkError as e:
            return _err(e)
        return {"ok": True, "hint": "work around it with another action, or wait_for the user"}

    def drop_action(ctx: RunContext, action_id: str, reason: str) -> dict:
        """Drop an action that turned out not to be needed, with the reason."""
        g, me, err = _mine(ctx)
        if err:
            return err
        try:
            set_action(g.id, action_id, WorkStatus.dropped, actor=me.name, result=reason)
        except WorkError as e:
            return _err(e)
        return {"ok": True}

    def log_note(ctx: RunContext, text: str) -> dict:
        """Write a note on the goal's log (a finding, a dead end, something to remember).
        Notes don't count as progress."""
        g, me, err = _mine(ctx)
        if err:
            return err
        if not (text or "").strip():
            return {"error": "the note is empty"}
        record(g.id, "note", text.strip()[:2000], me.name)
        return {"ok": True}

    async def wait_for(ctx: RunContext, kind: str, reason: str, minutes: int = 60) -> dict:
        """Pause the goal until something happens. The goal runner resumes it.

        Args:
            kind: "user" (you need the user: a decision, access, credentials; they answer in
                this conversation) or "time" (check back after `minutes`).
            reason: What you're waiting for. For "user", ask the question here.
            minutes: For "time": how long to wait (5 to 10080).
        """
        g, me, err = _mine(ctx)
        if err:
            return err
        if kind not in ("user", "time"):
            return {"error": "kind must be \"user\" or \"time\""}
        if not (reason or "").strip():
            return {"error": "say what you're waiting for in reason"}
        waiting: dict[str, Any] = {"kind": kind, "detail": reason.strip()}
        if kind == "time":
            minutes = max(5, min(int(minutes or 60), 10080))
            waiting["until"] = (_now() + timedelta(minutes=minutes)).isoformat()
        g = set_status(g.id, WorkStatus.waiting, actor=me.name, why=reason.strip(),
                       waiting_on=waiting)
        if kind == "user":
            await rt.save_message(g.thread_id, Role.event, f"{me.name} needs you: {reason.strip()}",
                                  agent_id=me.id, kind="goal", goal_id=g.id, status="question")
            await _notify(rt, "goal_question", g, question=reason.strip())
            return {"ok": True, "summary": "Waiting for the user. Ask your question in your reply "
                    "too, then end the turn. You'll be woken when they answer here."}
        return {"ok": True, "summary": f"The goal waits {minutes} minutes. End the turn now."}

    async def claim_done(ctx: RunContext, evidence: str) -> dict:
        """Say the goal is done: its done criteria are met. The user confirms it or sends it
        back with a note.

        Args:
            evidence: How each done criterion is met: what you checked and what it showed.
        """
        g, me, err = _mine(ctx)
        if err:
            return err
        if not (evidence or "").strip():
            return {"error": "give the evidence that the done criteria are met"}
        acts_now = actions(g.id)
        team_busy = [a for a in acts_now if on_team(a)]
        if team_busy:
            return {"error": "your team is still working: " + ", ".join(
                f"{a.title} ({a.id})" for a in team_busy) + ". Wait for their results."}
        busy = [a for a in acts_now if a.status == WorkStatus.in_progress]
        if busy:
            return {"error": f"finish or drop the action in progress first: {busy[0].title} ({busy[0].id})"}
        with db.session() as s:
            row = s.get(WorkItem, g.id)
            row.result = evidence.strip()
            s.add(row)
            s.commit()
        g = set_status(g.id, WorkStatus.review, actor=me.name, why="claims done")
        await rt.save_message(g.thread_id, Role.event, f"{me.name} says the goal is done.",
                              agent_id=me.id, kind="goal", goal_id=g.id, status="review",
                              evidence=evidence.strip())
        await _notify(rt, "goal_review", g, evidence=evidence.strip()[:1000])
        return {"ok": True, "summary": "Sent to the user to confirm. Summarise the outcome in "
                "your reply and end the turn."}

    async def assign_action(ctx: RunContext, action_id: str, role: str,
                            why_not_existing_agents: str, instructions: str = "",
                            model: str | None = None, tools: list[str] | None = None) -> dict:
        """Hand a startable next action to a new sub-agent on your team. It works the action
        in parallel while you carry on with yours, and its result lands on the action by
        itself (done, or blocked for you to re-plan).

        Args:
            action_id: A to-do action with nothing unfinished in its after list.
            role: Short role name for the sub-agent, e.g. "VLAN auditor".
            why_not_existing_agents: Why none of the existing agents should do this action
                (required; a sub-agent that duplicates one of them is refused: delegate to
                that agent instead).
            instructions: How to do it, what to check, what to bring back.
            model: One of the sub-agent models in your instructions (a cloud one if the local
                GPU is busy). Default: the first.
            tools: Tool server names to give it (default: all you're allowed to give).
        """
        g, me, err = _mine(ctx)
        if err:
            return err
        try:
            a = reserve_for_team(g.id, action_id, actor=me.name)
        except WorkError as e:
            return _err(e)
        try:
            got = await rt.spawn(
                ctx.deps, me, role=role, task=team_task(g, a, me, instructions),
                why_not_existing_agents=why_not_existing_agents,
                model=model, tools=tools, background=True,
                extra_meta={"goal_action": a.id, "goal_id": g.id},
                # The duplicate check looks at the work itself, as for any sub-agent: not at
                # the goal and team context the briefing adds.
                check_text=" ".join(x for x in (a.title, a.objective, instructions) if x).strip())
        except Exception:
            release_team(g.id, a.id)
            raise
        if got["status"] != "queued":
            release_team(g.id, a.id)
            return got
        assigned(g.id, a.id, got["sub"], got["job"].id, actor=me.name)
        return {"ok": True, "action_id": a.id, "subagent": got["sub"].name,
                "summary": f"{got['sub'].name} is working on it in parallel. Its result will be "
                "on the board. Carry on with your own action, or end the turn."}

    tools = [board_view, add_action, start_action, complete_action, block_action,
             drop_action, log_note, wait_for, claim_done]
    if team:
        tools.append(assign_action)
    return FunctionToolset(tools, id="goal")


def propose_toolset(rt) -> FunctionToolset:
    from .runtime import load_agent

    async def propose_goal(ctx: RunContext, title: str, objective: str, done_criteria: str,
                           reason: str, agent_name: str = "") -> dict:
        """Suggest a standing goal for the user's backlog. Nothing starts until they approve
        it and move it to Ready.

        Args:
            title: Short name, e.g. "Document every VLAN in the vault".
            objective: The outcome wanted, in a few sentences.
            done_criteria: How anyone can tell it's finished (checkable).
            reason: Why it's worth doing, and what you saw that led to it.
            agent_name: Who should own it (yourself if empty).
        """
        me = load_agent(ctx.deps.agent_id)
        owner = me
        if (agent_name or "").strip() and agent_name.strip().lower() != me.name.lower():
            with db.session() as s:
                owner = s.exec(select(AgentDef).where(
                    func.lower(AgentDef.name) == agent_name.strip().lower(),
                    AgentDef.is_subagent == False, AgentDef.enabled == True)).first()  # noqa: E712
            if owner is None:
                return {"error": f"there's no agent called {agent_name}"}
        with db.session() as s:
            mine = [w for w in s.exec(select(WorkItem).where(
                WorkItem.kind == GOAL, WorkItem.proposed == True,  # noqa: E712
                WorkItem.status == WorkStatus.backlog)).all()
                if (w.source or {}).get("agent_id") == me.id]
        if len(mine) >= MAX_PROPOSALS_PER_AGENT:
            return {"error": f"you already have {len(mine)} goal proposals waiting for the user"}
        try:
            g = create_goal(title=title, agent_id=owner.id, objective=objective,
                            done_criteria=done_criteria, proposed=True, proposal_reason=reason,
                            source={"kind": "agent", "agent_id": me.id, "agent": me.name,
                                    "thread_id": ctx.deps.thread_id}, actor=me.name)
        except WorkError as e:
            return {"error": str(e)}
        await rt.save_message(ctx.deps.thread_id, Role.event,
                              f"{me.name} proposes a goal: {g.title}", agent_id=me.id,
                              kind="goal_proposal", goal_id=g.id, status="proposed",
                              owner=owner.name, reason=g.proposal_reason)
        await _notify(rt, "goal_proposal", g, reason=g.proposal_reason, proposed_by=me.name)
        return {"ok": True, "goal_id": g.id,
                "summary": "It's in the user's backlog, waiting for approval. Mention it in your reply."}

    return FunctionToolset([propose_goal], id="goal_propose")


def takes_goals(agent: AgentDef) -> bool:
    return not agent.is_subagent and (agent.goal_wip or 0) > 0
