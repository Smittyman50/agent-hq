"""The learning loop: your ratings of replies, reflection settings and the reflection log."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import select

from .. import db, learning
from ..models import Message, Reflection, Role, SkillProposal

router = APIRouter(tags=["learning"])

_running: set[asyncio.Task] = set()  # "Run now" reflections in flight


@router.get("/api/learning/settings")
def get_settings():
    return learning.get_settings()


@router.put("/api/learning/settings")
def put_settings(body: dict[str, Any]):
    try:
        return learning.save_settings(body)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e)) from None


def _out(r: Reflection, proposals: dict[str, SkillProposal]) -> dict[str, Any]:
    out = r.model_dump()
    out["proposals"] = [{"id": p.id, "name": p.name, "title": p.title, "status": p.status,
                         "revision": bool(p.skill_id and p.base_version)}
                        for p in (proposals.get(i) for i in r.proposal_ids) if p]
    return out


@router.get("/api/learning/reflections")
def list_reflections(limit: int = 50, agent_id: str | None = None, status: str | None = None):
    q = select(Reflection).order_by(Reflection.created_at.desc()).limit(max(1, min(limit, 500)))
    if agent_id:
        q = q.where(Reflection.agent_id == agent_id)
    if status:
        q = q.where(Reflection.status == status)
    with db.session() as s:
        rows = list(s.exec(q).all())
        ids = [i for r in rows for i in r.proposal_ids]
        props = {p.id: p for p in s.exec(select(SkillProposal).where(SkillProposal.id.in_(ids))).all()} \
            if ids else {}
    return {"reflections": [_out(r, props) for r in rows], "ran_today": learning.ran_today(),
            "max_per_day": learning.get_settings()["max_per_day"]}


@router.post("/api/learning/reflections/{reflection_id}/run", status_code=202)
async def run_reflection(reflection_id: str, request: Request):
    """Run a waiting, skipped or failed reflection now (ignores the wait and the daily cap)."""
    with db.session() as s:
        r = s.get(Reflection, reflection_id)
    if r is None:
        raise HTTPException(404)
    if r.status in ("running", "done"):
        raise HTTPException(409, f"this reflection is already {r.status}")
    rt = request.app.state.runtime

    async def go():
        try:
            await learning.run_now(rt, reflection_id)
        except Exception:
            learning.log.exception("reflection %s failed", reflection_id)

    t = asyncio.create_task(go())
    _running.add(t)
    t.add_done_callback(_running.discard)
    return {"status": "started"}


@router.delete("/api/learning/reflections/{reflection_id}")
def cancel_reflection(reflection_id: str):
    """Drop a waiting reflection."""
    with db.session() as s:
        r = s.get(Reflection, reflection_id)
        if r is None:
            raise HTTPException(404)
        if r.status != "waiting":
            raise HTTPException(409, f"only waiting reflections can be cancelled (this one is {r.status})")
        r.status, r.summary = "skipped", "Cancelled by you."
        s.add(r)
        s.commit()
    return {"ok": True}


class FeedbackIn(BaseModel):
    rating: str | None  # up | down | None (clear)
    comment: str = ""


@router.post("/api/messages/{message_id}/feedback")
async def give_feedback(message_id: str, body: FeedbackIn, request: Request):
    """Rate an agent's reply. 👎 with a note (what it should have done) is the most useful
    thing you can give an agent to learn from."""
    if body.rating not in (None, "up", "down"):
        raise HTTPException(422, "rating is up, down or null")
    with db.session() as s:
        m = s.get(Message, message_id)
    if m is None:
        raise HTTPException(404)
    if m.role != Role.agent:
        raise HTTPException(422, "only agent replies can be rated")
    learning.set_feedback(m, body.rating, body.comment)
    fb = {"rating": body.rating, "comment": body.comment.strip()[:2000]} if body.rating else None
    updated = await request.app.state.runtime.update_message(message_id, feedback=fb)
    return {"id": message_id, "feedback": (updated.meta or {}).get("feedback") if updated else fb}
