"""Live activity of every agent (the Activity page): what's running now, tokens, model
requests and tool calls as they happen. The counting is in app/activity.py."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from sqlmodel import select

from .. import activity, db
from .. import workitems as wi
from ..models import AgentDef, WorkStatus

router = APIRouter(prefix="/api/activity", tags=["activity"])

SERIES = ("requests", "input", "output", "tools")


def _goal_for(thread_id: str) -> dict[str, Any] | None:
    g = wi.goal_for_thread(thread_id) if thread_id else None
    if g is None:
        return None
    acts = wi.actions(g.id)
    current = next((a for a in acts if a.status == WorkStatus.in_progress), None)
    return {"id": g.id, "title": g.title,
            "actions_total": len([a for a in acts if a.status != WorkStatus.dropped]),
            "actions_done": len([a for a in acts if a.status == WorkStatus.done]),
            "current_action": current.title if current else None, "turns": g.turns}


@router.get("")
async def live(request: Request, window: int = 60) -> dict[str, Any]:
    """window: minutes (15, 60, 180, 1440 or 10080). Sub-agents get their own rows, marked
    with the agent that made them."""
    broker = request.app.state.broker
    snap = await activity.snapshot(broker.r, window)
    with db.session() as s:
        defs = {a.id: a for a in s.exec(select(AgentDef)).all()}
    seen = {a["agent_id"] for a in snap["agents"]}
    for a in snap["agents"]:
        d = defs.get(a["agent_id"])
        a["name"] = d.name if d else "(deleted agent)"
        a["is_subagent"] = bool(d and d.is_subagent)
        a["parent_agent_id"] = d.parent_agent_id if d else None
    # Every enabled agent gets a row, busy or not, so you can see who is idle.
    for d in defs.values():
        if d.enabled and not d.is_subagent and d.id not in seen:
            snap["agents"].append({
                "agent_id": d.id, "name": d.name, "is_subagent": False, "parent_agent_id": None,
                "totals": {m: 0 for m in activity.METRICS},
                "series": {m: [0] * len(snap["buckets"]) for m in SERIES}, "top_tools": []})
    goals: dict[str, Any] = {}
    for j in snap["jobs"]:
        tid = j.get("thread_id") or ""
        if tid not in goals:
            goals[tid] = _goal_for(tid)
        j["goal"] = goals[tid]
    running = {j["agent_id"] for j in snap["jobs"]}
    snap["agents"].sort(key=lambda a: (a["agent_id"] not in running,
                                       -(a["totals"]["input"] + a["totals"]["output"]),
                                       a["name"].lower()))
    snap["workers"] = await broker.workers()
    return snap
