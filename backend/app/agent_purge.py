"""Permanently deleting an agent, and everything that belongs only to it.

Only a disabled agent can be deleted (disable first, then delete: two deliberate steps).
What goes: its conversations (messages, files, jobs, traces and checkpoints), schedules,
event subscriptions, avatar, its own memories and skills, its proposals, test cases,
feedback, reflections, tool approvals and sub-agent requests, and its sub-agents.
What stays: the usage ledger (cost history), team memories and skills, and knowledge.
Goals it owned go back to your backlog with no owner; actions it was working go back to
Ready. Other agents stop delegating to it.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlmodel import Session, select

from .models import (AgentAvatar, AgentDef, Attachment, EvalCase, EvalResult, Feedback, Job, JobStatus,
                     JobTrace, KnowledgeCollection, KnowledgeProposal, Memory, MemoryScope, Message,
                     ProfileProposal, Reflection, Schedule, Skill, SkillProposal, SkillUse, SkillVersion,
                     SubagentRequest, Subscription, Thread, ToolApproval, WorkItem, WorkItemEvent, WorkStatus,
                     _now)

log = logging.getLogger(__name__)

ACTIVE_GOAL = (WorkStatus.ready, WorkStatus.in_progress, WorkStatus.waiting, WorkStatus.review,
               WorkStatus.blocked)


class PurgeRefused(Exception):
    """The agent can't be deleted right now; the message says why."""


def _ids(s: Session, agent: AgentDef) -> list[str]:
    """The agent and the sub-agents it made."""
    subs = s.exec(select(AgentDef.id).where(AgentDef.parent_agent_id == agent.id,
                                            AgentDef.is_subagent == True)).all()  # noqa: E712
    return [agent.id, *subs]


def _check(s: Session, agent: AgentDef, ids: list[str], tids: list[str]) -> None:
    if agent.is_subagent:
        raise PurgeRefused("sub-agents are cleaned up with the agent that made them")
    if agent.enabled:
        raise PurgeRefused(f"disable {agent.name} first, then delete it")
    busy = s.exec(select(Job.id).where(Job.status.in_([JobStatus.queued, JobStatus.running]),
                                       (Job.assignee_agent_id.in_(ids)) | (Job.origin_thread_id.in_(tids)))
                  ).first()
    if busy:
        raise PurgeRefused(f"{agent.name} still has work queued or running; stop it or wait for it to finish")


def _count(s: Session, model, *where) -> int:
    from sqlalchemy import func

    return s.exec(select(func.count()).select_from(model).where(*where)).one()


def plan(s: Session, agent: AgentDef) -> dict[str, Any]:
    """What deleting this agent would remove or change, for the confirm prompt."""
    ids = _ids(s, agent)
    tids = list(s.exec(select(Thread.id).where(Thread.agent_id.in_(ids))).all())
    blocked = None
    try:
        _check(s, agent, ids, tids)
    except PurgeRefused as e:
        blocked = str(e)
    return {
        "can_delete": blocked is None, "reason": blocked,
        "conversations": len(tids),
        "messages": _count(s, Message, Message.thread_id.in_(tids)),
        "schedules": _count(s, Schedule, Schedule.agent_id.in_(ids)),
        "subscriptions": _count(s, Subscription, Subscription.agent_id.in_(ids)),
        "memories": _count(s, Memory, Memory.agent_id.in_(ids), Memory.scope != MemoryScope.team),
        "skills": _count(s, Skill, Skill.agent_id.in_(ids), Skill.scope == "agent"),
        "test_cases": _count(s, EvalCase, EvalCase.agent_id.in_(ids)),
        "goals_unassigned": _count(s, WorkItem, WorkItem.kind == "goal", WorkItem.owner_agent_id.in_(ids)),
        "subagents": len(ids) - 1,
    }


def _rm(s: Session, model, *where) -> None:
    s.exec(sa_delete(model).where(*where))


def purge(s: Session, agent: AgentDef) -> list[str]:
    """Delete the agent. Returns the deleted conversation IDs (their checkpoints are
    removed separately, see forget_checkpoints). Raises PurgeRefused."""
    ids = _ids(s, agent)
    tids = list(s.exec(select(Thread.id).where(Thread.agent_id.in_(ids))).all())
    _check(s, agent, ids, tids)
    name = agent.name

    # Board: goals go back to your backlog, unowned; actions it worked go back to Ready.
    for g in s.exec(select(WorkItem).where(WorkItem.owner_agent_id.in_(ids))).all():
        g.owner_agent_id = None
        if g.status in ACTIVE_GOAL:
            g.status = WorkStatus.backlog
            g.waiting_on = None
        g.updated_at = _now()
        s.add(g)
        s.add(WorkItemEvent(item_id=g.parent_id or g.id, action_id=g.id if g.parent_id else None,
                            actor="you", kind="owner_deleted",
                            text=f"{name} was deleted; this goal has no owner now"))
    for a in s.exec(select(WorkItem).where(WorkItem.assignee_agent_id.in_(ids))).all():
        a.assignee_agent_id, a.job_id = None, None
        if a.status in (WorkStatus.in_progress, WorkStatus.waiting):
            a.status = WorkStatus.ready
        s.add(a)
    s.exec(sa_update(WorkItem).where(WorkItem.thread_id.in_(tids)).values(thread_id=None))

    # Its conversations and everything in them.
    jids = list(s.exec(select(Job.id).where(Job.origin_thread_id.in_(tids))).all())
    _rm(s, JobTrace, JobTrace.job_id.in_(jids))
    _rm(s, Job, Job.id.in_(jids))
    _rm(s, Attachment, Attachment.thread_id.in_(tids))
    _rm(s, Message, Message.thread_id.in_(tids))
    _rm(s, SubagentRequest, SubagentRequest.agent_id.in_(ids) | SubagentRequest.thread_id.in_(tids))
    _rm(s, ToolApproval, ToolApproval.agent_id.in_(ids) | ToolApproval.thread_id.in_(tids))
    _rm(s, Feedback, Feedback.agent_id.in_(ids) | Feedback.thread_id.in_(tids))
    _rm(s, Reflection, Reflection.agent_id.in_(ids) | Reflection.thread_id.in_(tids))

    # Its triggers.
    _rm(s, Schedule, Schedule.agent_id.in_(ids))
    _rm(s, Subscription, Subscription.agent_id.in_(ids))
    s.exec(sa_update(Schedule).where(Schedule.thread_id.in_(tids)).values(thread_id=None))
    s.exec(sa_update(Subscription).where(Subscription.thread_id.in_(tids)).values(thread_id=None))
    # Notification channels: no longer its default agent, and its rules forget it (a rule
    # that named only this agent is switched off rather than widened to every agent).
    from .models import NotificationChannel, NotificationRoute

    s.exec(sa_update(NotificationChannel).where(NotificationChannel.default_agent_id.in_(ids))
           .values(default_agent_id=None, chat_thread_id=None))
    s.exec(sa_update(NotificationChannel).where(NotificationChannel.chat_thread_id.in_(tids))
           .values(chat_thread_id=None))
    for r in s.exec(select(NotificationRoute)).all():
        if set(r.agent_ids or []) & set(ids):
            r.agent_ids = [a for a in r.agent_ids if a not in ids]
            if not r.agent_ids:  # empty would mean every agent: switch the rule off instead
                r.enabled = False
            s.add(r)

    # What it learned for itself (team memories and skills stay).
    _rm(s, Memory, Memory.agent_id.in_(ids), Memory.scope != MemoryScope.team)
    _rm(s, Memory, Memory.thread_id.in_(tids), Memory.scope == MemoryScope.thread)
    sids = list(s.exec(select(Skill.id).where(Skill.agent_id.in_(ids), Skill.scope == "agent")).all())
    _rm(s, SkillVersion, SkillVersion.skill_id.in_(sids))
    _rm(s, SkillUse, SkillUse.skill_id.in_(sids) | SkillUse.agent_id.in_(ids))
    _rm(s, SkillProposal, SkillProposal.agent_id.in_(ids) | SkillProposal.skill_id.in_(sids))
    _rm(s, Skill, Skill.id.in_(sids))
    _rm(s, KnowledgeProposal, KnowledgeProposal.agent_id.in_(ids))
    _rm(s, ProfileProposal, ProfileProposal.agent_id.in_(ids))
    cids = list(s.exec(select(EvalCase.id).where(EvalCase.agent_id.in_(ids))).all())
    _rm(s, EvalResult, EvalResult.case_id.in_(cids))
    _rm(s, EvalCase, EvalCase.id.in_(cids))

    # Nobody points at it any more.
    for c in s.exec(select(KnowledgeCollection)).all():
        # Older rows can hold NULL here (the columns were added later).
        have, have_p = c.agent_ids or [], c.proposer_agent_ids or []
        keep = [i for i in have if i not in ids]
        keep_p = [i for i in have_p if i not in ids]
        if keep != have or keep_p != have_p:
            c.agent_ids, c.proposer_agent_ids = keep, keep_p
            s.add(c)
    for other in s.exec(select(AgentDef).where(AgentDef.id.not_in(ids))).all():
        if any(d in ids for d in other.delegates or []):
            other.delegates = [d for d in other.delegates or [] if d not in ids]
            s.add(other)
    _rm(s, AgentAvatar, AgentAvatar.agent_id.in_(ids))
    s.flush()
    _rm(s, Thread, Thread.id.in_(tids))
    _rm(s, AgentDef, AgentDef.id.in_(ids))
    s.commit()
    log.info("deleted agent %s (%s) with %d conversations", name, agent.id, len(tids))
    return tids


async def forget_checkpoints(runtime, thread_ids: list[str]) -> None:
    """Drop the model-level history LangGraph keeps for these conversations."""
    if not thread_ids:
        return
    graph = getattr(runtime, "graph", None)
    saver = getattr(graph, "checkpointer", None) if graph is not None else None
    try:
        if saver is not None:
            for t in thread_ids:
                await saver.adelete_thread(t)
            return
        from .runtime import open_checkpointer

        async with open_checkpointer() as saver:
            for t in thread_ids:
                await saver.adelete_thread(t)
    except Exception:  # the rows are gone either way; stale checkpoints are only dead weight
        log.warning("couldn't delete checkpoints for %d conversations", len(thread_ids), exc_info=True)
