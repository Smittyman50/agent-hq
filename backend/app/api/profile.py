"""Your profile (Settings → Profile) and agents' suggestions for it (Requests → Profile updates)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import select

from .. import db, profile
from ..models import ProfileProposal

router = APIRouter(tags=["profile"])


def _out(p: dict[str, Any]) -> dict[str, Any]:
    return {**p, "words": profile.words(p["text"])}


@router.get("/api/profile")
def get_profile():
    return _out(profile.get())


@router.put("/api/profile")
def put_profile(body: dict[str, Any]):
    try:
        return _out(profile.save(body))
    except (TypeError, ValueError) as e:
        raise HTTPException(422, str(e)) from None


@router.get("/api/profile-proposals")
def list_proposals(status: str | None = None, limit: int = 100):
    q = select(ProfileProposal).order_by(ProfileProposal.created_at.desc()).limit(max(1, min(limit, 500)))
    if status:
        q = q.where(ProfileProposal.status == status)
    with db.session() as s:
        return list(s.exec(q).all())


@router.get("/api/profile-proposals/{proposal_id}")
def get_proposal(proposal_id: str):
    with db.session() as s:
        p = s.get(ProfileProposal, proposal_id)
    if p is None:
        raise HTTPException(404)
    return p


class DecideIn(BaseModel):
    approve: bool
    note: str = ""
    text: str | None = None  # your edited wording


@router.post("/api/profile-proposals/{proposal_id}/decide")
async def decide(proposal_id: str, body: DecideIn, request: Request):
    try:
        return await request.app.state.runtime.decide_profile_proposal(
            proposal_id, body.approve, note=body.note, text=body.text)
    except LookupError:
        raise HTTPException(404) from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
