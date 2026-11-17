"""The board: standing goals and their next actions (see app/workitems.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import col, select

from .. import db, subagents, workitems as wi
from ..models import AgentDef, Role, SubagentMode, WorkItem, WorkStatus

router = APIRouter(tags=["board"])


def _iso(dt: datetime | None) -> str | None:
    return wi.aware(dt).isoformat().replace("+00:00", "Z") if dt else None


def _goal_out(g: WorkItem, agents: dict[str, AgentDef], acts: list[WorkItem] | None = None) -> dict:
    acts = wi.actions(g.id) if acts is None else acts
    agent = agents.get(g.owner_agent_id or "")
    size = wi.team_size(agent)
    current = next((a for a in acts if a.status == WorkStatus.in_progress and not a.job_id), None)
    return {
        "id": g.id, "kind": g.kind, "title": g.title, "objective": g.objective,
        "done_criteria": g.done_criteria, "status": g.status.value, "column": wi.COLUMNS[g.status],
        "owner_agent_id": g.owner_agent_id, "agent_name": agent.name if agent else None,
        "priority": g.priority, "position": g.position, "thread_id": g.thread_id,
        "waiting_on": g.waiting_on, "source": g.source, "proposed": g.proposed,
        "proposal_reason": g.proposal_reason, "needs_you": wi.needs_you(g),
        "limits": wi.limits_of(g), "custom_limits": g.limits or {},
        "turns": g.turns, "stalled_turns": g.stalled_turns,
        "result": g.result, "review_note": g.review_note,
        "actions_total": len([a for a in acts if a.status != WorkStatus.dropped]),
        "actions_done": len([a for a in acts if a.status == WorkStatus.done]),
        "current_action": current.title if current else None,
        "team_size": size, "team_working": sum(1 for a in acts if wi.on_team(a)),
        # Who the lead can put on the team: its own agents, and sub-agents if it's allowed them.
        "team_agents": [x.name for x in wi.team_agents(agent)] if size > 1 else [],
        "team_sub_agents": size > 1 and agent is not None
        and subagents.mode_of(agent) != SubagentMode.off,
        "created_at": _iso(g.created_at), "updated_at": _iso(g.updated_at),
        "started_at": _iso(g.started_at), "finished_at": _iso(g.finished_at),
    }


def _action_out(a: WorkItem, acts: list[WorkItem], names: dict[str, AgentDef]) -> dict:
    waiting = wi.unmet(a, acts) if a.status == WorkStatus.backlog else []
    return {"id": a.id, "title": a.title, "details": a.objective, "status": a.status.value,
            "label": wi.LABELS[a.status], "result": a.result, "position": a.position,
            "after": list(a.depends_on or []), "waiting_for": [d.id for d in waiting],
            "startable": wi.startable(a, acts), "on_team": wi.on_team(a),
            "assignee": (m.name if (m := names.get(a.assignee_agent_id)) else "a sub-agent")
            if a.assignee_agent_id else None,
            # One of the lead's own agents (delegate_action) or a sub-agent (assign_action).
            "assignee_kind": ("agent" if (m := names.get(a.assignee_agent_id)) and not m.is_subagent
                              else "sub-agent") if a.assignee_agent_id else None,
            "started_at": _iso(a.started_at), "finished_at": _iso(a.finished_at)}


def _names(acts: list[WorkItem]) -> dict[str, AgentDef]:
    ids = [a.assignee_agent_id for a in acts if a.assignee_agent_id]
    if not ids:
        return {}
    with db.session() as s:
        return {x.id: x for x in s.exec(select(AgentDef).where(col(AgentDef.id).in_(ids))).all()}


def _agents() -> dict[str, AgentDef]:
    with db.session() as s:
        return {a.id: a for a in s.exec(select(AgentDef).where(AgentDef.is_subagent == False)).all()}  # noqa: E712


def _goal(goal_id: str) -> WorkItem:
    g = wi.get(goal_id)
    if g is None or g.kind != wi.GOAL:
        raise HTTPException(404, "no such goal")
    return g


def _detail(g: WorkItem) -> dict:
    acts = wi.actions(g.id)
    names = _names(acts)
    return {**_goal_out(g, _agents(), acts), "actions": [_action_out(a, acts, names) for a in acts],
            "events": [{"id": e.id, "at": _iso(e.at), "actor": e.actor, "kind": e.kind,
                        "text": e.text, "action_id": e.action_id} for e in wi.events(g.id)],
            "requests_used": wi._requests_used(g), "default_limits": wi.DEFAULT_LIMITS}


def _bad(e: Exception) -> HTTPException:
    if isinstance(e, LookupError):
        return HTTPException(404, str(e))
    return HTTPException(409 if isinstance(e, wi.WorkError) and "at its WIP limit" in str(e) else 422, str(e))


@router.get("/api/board")
def board(agent_id: str | None = None, done_days: int = 14):
    """Every goal on the board (done and dropped ones from the last `done_days` days),
    with each agent's WIP limit and how much of it is used."""
    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(done_days, 365)))
    q = select(WorkItem).where(WorkItem.kind == wi.GOAL)
    if agent_id:
        q = q.where(WorkItem.owner_agent_id == agent_id)
    with db.session() as s:
        rows = [g for g in s.exec(q.order_by(col(WorkItem.priority).desc(), WorkItem.position,
                                             WorkItem.created_at)).all()
                if g.status not in (WorkStatus.done, WorkStatus.dropped)
                or (g.finished_at and wi.aware(g.finished_at) >= since)]
        acts = s.exec(select(WorkItem).where(col(WorkItem.parent_id).in_([g.id for g in rows]))).all() \
            if rows else []
    by_goal: dict[str, list[WorkItem]] = {}
    for a in acts:
        by_goal.setdefault(a.parent_id, []).append(a)
    agents = _agents()
    lanes = [{"id": a.id, "name": a.name, "goal_wip": a.goal_wip, "wip_used": wi.wip_used(a.id),
              "enabled": a.enabled}
             for a in sorted(agents.values(), key=lambda a: a.name.lower())
             if a.goal_wip > 0 or any(g.owner_agent_id == a.id for g in rows)]
    return {"columns": [{"status": st.value, "label": wi.COLUMNS[st]} for st in (
                WorkStatus.backlog, WorkStatus.ready, WorkStatus.in_progress, WorkStatus.waiting,
                WorkStatus.review, WorkStatus.done)],
            "agents": lanes,
            "goals": [_goal_out(g, agents, by_goal.get(g.id, [])) for g in rows]}


@router.get("/api/work-items/attention")
def attention():
    """Goals that need you, newest first: done claims to confirm, proposals, goals a
    guard blocked, and questions."""
    with db.session() as s:
        rows = s.exec(select(WorkItem).where(WorkItem.kind == wi.GOAL)
                      .order_by(col(WorkItem.updated_at).desc())).all()
    agents = _agents()
    return [_goal_out(g, agents) for g in rows if wi.needs_you(g)]


class GoalIn(BaseModel):
    title: str
    agent_id: str
    objective: str = ""
    done_criteria: str = ""
    status: WorkStatus = WorkStatus.backlog
    priority: int = 0
    limits: dict[str, Any] | None = None
    # "Make this a goal" from a chat message: where it came from.
    from_thread_id: str | None = None
    from_message_id: str | None = None


@router.post("/api/work-items", status_code=201)
def create(body: GoalIn):
    source: dict[str, Any] = {"kind": "user"}
    if body.from_thread_id:
        source.update(thread_id=body.from_thread_id, message_id=body.from_message_id)
    try:
        g = wi.create_goal(title=body.title, agent_id=body.agent_id, objective=body.objective,
                           done_criteria=body.done_criteria, status=body.status,
                           priority=body.priority, limits=body.limits, source=source)
    except wi.WorkError as e:
        raise _bad(e) from None
    return _detail(g)


@router.get("/api/work-items/{goal_id}")
def detail(goal_id: str):
    return _detail(_goal(goal_id))


class GoalPatch(BaseModel):
    title: str | None = None
    objective: str | None = None
    done_criteria: str | None = None
    priority: int | None = None
    limits: dict[str, Any] | None = None
    owner_agent_id: str | None = None


@router.patch("/api/work-items/{goal_id}")
def update(goal_id: str, body: GoalPatch):
    _goal(goal_id)
    try:
        g = wi.update_goal(goal_id, body.model_dump(exclude_none=True))
    except (wi.WorkError, LookupError) as e:
        raise _bad(e) from None
    return _detail(g)


class MoveIn(BaseModel):
    status: WorkStatus
    position: float | None = None


@router.post("/api/work-items/{goal_id}/move")
async def move(goal_id: str, body: MoveIn, request: Request):
    before = _goal(goal_id)
    try:
        g = wi.move(goal_id, body.status, position=body.position)
    except (wi.WorkError, LookupError) as e:
        raise _bad(e) from None
    if g.thread_id and before.status != g.status:
        rt = request.app.state.runtime
        if g.status == WorkStatus.in_progress and before.status in (WorkStatus.blocked, WorkStatus.waiting):
            await rt.save_message(g.thread_id, Role.event, f"You resumed the goal: {g.title}",
                                  agent_id=g.owner_agent_id, kind="goal", goal_id=g.id, status="resumed")
        elif g.status == WorkStatus.dropped:
            if before.status in wi.ACTIVE:
                await rt.stop_thread(g.thread_id)
            await rt.save_message(g.thread_id, Role.event, f"You dropped the goal: {g.title}",
                                  agent_id=g.owner_agent_id, kind="goal", goal_id=g.id, status="dropped")
    return _detail(wi.get(goal_id))


class DecisionIn(BaseModel):
    approve: bool
    note: str = ""
    ready: bool = False  # proposals: approve straight into Ready


@router.post("/api/work-items/{goal_id}/review")
async def review(goal_id: str, body: DecisionIn, request: Request):
    _goal(goal_id)
    try:
        g = wi.decide_review(goal_id, body.approve, body.note)
    except (wi.WorkError, LookupError) as e:
        raise _bad(e) from None
    if g.thread_id:
        note = f" Your note: {body.note.strip()}" if body.note.strip() else ""
        text = (f"You confirmed the goal is done.{note}" if body.approve
                else f"You sent the goal back.{note}")
        await request.app.state.runtime.save_message(
            g.thread_id, Role.event, text, agent_id=g.owner_agent_id, kind="goal", goal_id=g.id,
            status="done" if body.approve else "sent_back")
    return _detail(g)


@router.post("/api/work-items/{goal_id}/proposal")
async def proposal(goal_id: str, body: DecisionIn, request: Request):
    _goal(goal_id)
    try:
        g = wi.decide_proposal(goal_id, body.approve, ready=body.ready, note=body.note)
    except (wi.WorkError, LookupError) as e:
        raise _bad(e) from None
    thread_id = (g.source or {}).get("thread_id")
    if thread_id:
        with db.session() as s:
            from ..models import Message

            msgs = s.exec(select(Message).where(Message.thread_id == thread_id)).all()
        rt = request.app.state.runtime
        for m in msgs:
            if (m.meta or {}).get("kind") == "goal_proposal" and (m.meta or {}).get("goal_id") == g.id:
                await rt.update_message(m.id, status="approved" if body.approve else "rejected",
                                        note=body.note.strip())
    return _detail(g)


class ActionIn(BaseModel):
    title: str
    details: str = ""
    after: list[str] = []


@router.post("/api/work-items/{goal_id}/actions", status_code=201)
def add_action(goal_id: str, body: ActionIn):
    _goal(goal_id)
    try:
        wi.new_action(goal_id, body.title, body.details, actor="you", after=body.after)
    except wi.WorkError as e:
        raise _bad(e) from None
    return _detail(wi.get(goal_id))


class ActionPatch(BaseModel):
    status: WorkStatus
    result: str = ""


@router.patch("/api/work-items/{goal_id}/actions/{action_id}")
def update_action(goal_id: str, action_id: str, body: ActionPatch):
    _goal(goal_id)
    if body.status not in (WorkStatus.backlog, WorkStatus.in_progress, WorkStatus.done,
                           WorkStatus.blocked, WorkStatus.dropped):
        raise HTTPException(422, "an action is to do, in progress, done, blocked or dropped")
    try:
        wi.set_action(goal_id, action_id, body.status, actor="you", result=body.result)
    except wi.WorkError as e:
        raise _bad(e) from None
    return _detail(wi.get(goal_id))


class NoteIn(BaseModel):
    text: str


@router.post("/api/work-items/{goal_id}/notes", status_code=201)
def add_note(goal_id: str, body: NoteIn):
    _goal(goal_id)
    if not body.text.strip():
        raise HTTPException(422, "the note is empty")
    wi.record(goal_id, "note", body.text.strip()[:4000], "you")
    return _detail(wi.get(goal_id))
