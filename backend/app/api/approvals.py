"""Tool calls waiting for your approval, and your decisions on them."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import select

from .. import approvals, db, subagents
from ..models import Thread, ToolApproval

router = APIRouter(tags=["approvals"])


def _out(r: ToolApproval) -> dict:
    with db.session() as s:
        t = s.get(Thread, r.thread_id)
    return {**r.model_dump(), "thread_title": t.title if t else ""}


@router.get("/api/tool-approvals")
def list_approvals(status: str | None = None, limit: int = 100):
    approvals.sweep()
    q = select(ToolApproval).order_by(ToolApproval.created_at.desc()).limit(max(1, min(limit, 500)))
    if status:
        q = q.where(ToolApproval.status == status)
    with db.session() as s:
        rows = s.exec(q).all()
    return [_out(r) for r in rows]


@router.get("/api/tool-approvals/{approval_id}")
def get_approval(approval_id: str):
    with db.session() as s:
        r = s.get(ToolApproval, approval_id)
    if not r:
        raise HTTPException(404)
    return _out(r)


class Decision(BaseModel):
    approve: bool
    note: str = ""
    # On approval: don't ask again for this tool, agent and conversation for this long.
    remember_minutes: int = 0


@router.post("/api/tool-approvals/{approval_id}/decide")
async def decide(approval_id: str, body: Decision, request: Request):
    if not 0 <= body.remember_minutes <= 24 * 60:
        raise HTTPException(422, "remember for at most 24 hours")
    try:
        r = await request.app.state.runtime.decide_approval(
            approval_id, body.approve, note=body.note, remember_minutes=body.remember_minutes)
    except LookupError:
        raise HTTPException(404) from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return _out(r)


@router.get("/api/inbox/count")
def inbox_count() -> dict[str, int]:
    """What's waiting for you: sub-agent requests and tool approvals (the sidebar badge)."""
    requests = subagents.pending_requests_total()
    tool_calls = approvals.pending_count()
    return {"requests": requests, "approvals": tool_calls, "total": requests + tool_calls}
