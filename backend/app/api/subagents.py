"""Sub-agent requests (your approval inbox), team-wide sub-agent caps, and the usage ledger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, func, select

from .. import subagents
from ..db import get_session
from ..models import AgentDef, ModelProvider, SubagentRequest, Thread, UsageRecord

router = APIRouter(tags=["subagents"])


# ------------------------------------------------------------------ requests


def _out(s: Session, r: SubagentRequest) -> dict[str, Any]:
    agent = s.get(AgentDef, r.agent_id)
    thread = s.get(Thread, r.thread_id)
    owner = s.get(AgentDef, thread.agent_id) if thread else None
    return {**r.model_dump(), "agent": agent.name if agent else "?",
            "thread_title": thread.title if thread else "",
            "thread_owner": owner.name if owner else "",
            "limits": subagents.limits_of(agent) if agent else None}


@router.get("/api/subagent-requests")
def list_requests(status: str | None = None, limit: int = 100, s: Session = Depends(get_session)):
    subagents.sweep()
    q = select(SubagentRequest).order_by(SubagentRequest.created_at.desc()).limit(limit)
    if status:
        q = q.where(SubagentRequest.status == status)
    return [_out(s, r) for r in s.exec(q).all()]


@router.get("/api/subagent-requests/pending-count")
def pending_count(s: Session = Depends(get_session)) -> dict[str, int]:
    return {"pending": s.exec(select(func.count()).select_from(SubagentRequest)
                              .where(SubagentRequest.status == "pending")).one()}


@router.get("/api/subagent-requests/{request_id}")
def get_request(request_id: str, s: Session = Depends(get_session)):
    r = s.get(SubagentRequest, request_id)
    if not r:
        raise HTTPException(404)
    return _out(s, r)


class Decision(BaseModel):
    approve: bool
    note: str = ""
    # Optional changes to what was asked for (approve only). Omitted = as requested.
    count: int | None = None
    concurrent: int | None = None
    max_minutes: int | None = None
    max_requests: int | None = None
    hours: float | None = None
    models: list[dict[str, Any]] | None = None
    tool_server_ids: list[str] | None = None


@router.post("/api/subagent-requests/{request_id}/decide")
async def decide(request_id: str, body: Decision, request: Request, s: Session = Depends(get_session)):
    for k, lo, hi in (("count", 1, 20), ("concurrent", 1, 10), ("max_minutes", 1, 240),
                      ("max_requests", 1, 500)):
        v = getattr(body, k)
        if v is not None and not lo <= v <= hi:
            raise HTTPException(422, f"{k} must be between {lo} and {hi}")
    if body.hours is not None and not 0.5 <= body.hours <= 72:
        raise HTTPException(422, "hours must be between 0.5 and 72")
    if body.models is not None:
        for m in body.models:
            if not m.get("model") or (m.get("provider_id") and not s.get(ModelProvider, m["provider_id"])):
                raise HTTPException(422, "choose models from your providers")
    try:
        r = await request.app.state.runtime.decide_request(
            request_id, body.approve, note=body.note,
            changes=body.model_dump(exclude={"approve", "note"}, exclude_none=True))
    except LookupError:
        raise HTTPException(404) from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    s.expire_all()
    return _out(s, s.get(SubagentRequest, r.id))


# ------------------------------------------------------------------ team caps


@router.get("/api/settings/subagents")
def get_caps() -> dict[str, Any]:
    return subagents.get_settings()


@router.put("/api/settings/subagents")
def put_caps(body: dict[str, Any]) -> dict[str, Any]:
    for k in ("max_live", "max_per_day", "max_pending_per_agent", "request_ttl_hours"):
        if k in body and (not isinstance(body[k], (int, float)) or body[k] < 1):
            raise HTTPException(422, f"{k} must be at least 1")
    if "similarity_threshold" in body and not 0.3 <= float(body["similarity_threshold"]) <= 1:
        raise HTTPException(422, "similarity_threshold must be between 0.3 and 1")
    return subagents.save_settings(body)


@router.get("/api/subagents/live")
def live_subagents(s: Session = Depends(get_session)):
    """Sub-agents that exist right now, and today's counts."""
    rows = s.exec(select(AgentDef).where(AgentDef.is_subagent == True,  # noqa: E712
                                         AgentDef.retired == False)).all()  # noqa: E712
    names = {a.id: a.name for a in s.exec(select(AgentDef)).all()}
    today = subagents.start_of_day()
    made_today = s.exec(select(func.count()).select_from(AgentDef).where(
        AgentDef.is_subagent == True, AgentDef.created_at >= today)).one()  # noqa: E712
    return {"live": [{"id": a.id, "name": a.name, "parent": names.get(a.parent_agent_id, "?"),
                      "model": a.model, "expires_at": a.expires_at} for a in rows],
            "today": made_today, "caps": subagents.get_settings()}


# ------------------------------------------------------------------ usage ledger


def _group(rows: list[UsageRecord], key) -> list[dict[str, Any]]:
    out: dict[Any, dict[str, Any]] = {}
    for r in rows:
        k = key(r)
        g = out.setdefault(k, {"runs": 0, "failed": 0, "requests": 0, "input_tokens": 0,
                               "output_tokens": 0, "seconds": 0.0})
        g["runs"] += 1
        g["failed"] += 0 if r.ok else 1
        g["requests"] += r.requests
        g["input_tokens"] += r.input_tokens
        g["output_tokens"] += r.output_tokens
        g["seconds"] += r.seconds
    res = []
    for k, g in out.items():
        g["seconds"] = round(g["seconds"], 1)
        g["avg_seconds"] = round(g["seconds"] / g["runs"], 1)
        g["avg_tokens"] = round((g["input_tokens"] + g["output_tokens"]) / g["runs"])
        res.append({"key": k, **g})
    return sorted(res, key=lambda g: -g["seconds"])


@router.get("/api/usage")
def usage(days: int = 7, s: Session = Depends(get_session)) -> dict[str, Any]:
    """Totals from the ledger: by agent (sub-agents counted under the agent that made
    them), by model, by day, and the sub-agent share."""
    days = max(1, min(days, 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = list(s.exec(select(UsageRecord).where(UsageRecord.at >= since)).all())
    names = {a.id: a.name for a in s.exec(select(AgentDef)).all()}
    by_agent = _group(rows, lambda r: names.get(r.parent_agent_id if r.is_subagent else r.agent_id,
                                                r.agent_name))
    for g in by_agent:
        g["agent"] = g.pop("key")
        g["subagent_runs"] = sum(1 for r in rows if r.is_subagent
                                 and names.get(r.parent_agent_id) == g["agent"])
    by_model = _group(rows, lambda r: (r.provider_name, r.model))
    for g in by_model:
        g["provider"], g["model"] = g.pop("key")
    by_day = _group(rows, lambda r: subagents.aware(r.at).date().isoformat())
    for g in by_day:
        g["day"] = g.pop("key")
    by_day.sort(key=lambda g: g["day"])
    sub_rows = [r for r in rows if r.is_subagent]
    total = _group(rows, lambda r: "all")
    return {"days": days, "total": total[0] if total else None,
            "subagents": (_group(sub_rows, lambda r: "sub")[0] if sub_rows else None),
            "by_agent": by_agent, "by_model": by_model, "by_day": by_day}
