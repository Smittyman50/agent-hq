"""Skills (Settings → Skills) and agents' proposals for new or better ones."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import select

from .. import db, skills
from ..models import AgentDef, Skill, SkillProposal, SkillVersion

router = APIRouter(tags=["skills"])


def _names() -> dict[str, str]:
    with db.session() as s:
        return {a.id: a.name for a in s.exec(select(AgentDef)).all()}


def _out(k: Skill, st: dict | None = None, names: dict | None = None) -> dict[str, Any]:
    g = (st or {}).get(k.id) or {"uses": 0, "done": 0, "partial": 0, "failed": 0, "pending": 0,
                                 "up": 0, "down": 0, "last_used_at": None}
    finished = g["done"] + g["partial"] + g["failed"]
    return {**k.model_dump(), "agent": (names or {}).get(k.agent_id or "", ""), "stats": g,
            "success_rate": round(g["done"] / finished, 2) if finished else None}


@router.get("/api/skills")
def list_skills(include_retired: bool = False):
    with db.session() as s:
        q = select(Skill).order_by(Skill.name)
        if not include_retired:
            q = q.where(Skill.status == "active")
        rows = s.exec(q).all()
    st, names = skills.stats([k.id for k in rows]), _names()
    return [_out(k, st, names) for k in rows]


@router.get("/api/skills/{skill_id}")
def get_skill(skill_id: str):
    with db.session() as s:
        k = s.get(Skill, skill_id)
        if not k:
            raise HTTPException(404)
        versions = s.exec(select(SkillVersion).where(SkillVersion.skill_id == skill_id)
                          .order_by(SkillVersion.version.desc())).all()
    return {**_out(k, skills.stats([k.id]), _names()),
            "versions": [v.model_dump() for v in versions]}


class SkillIn(BaseModel):
    name: str
    title: str
    when_to_use: str
    body: str
    tools: list[str] = []
    script: str | None = None
    scope: str = "team"
    agent_id: str | None = None
    note: str = ""  # why it changed (kept in the version history)


def _check(body: SkillIn) -> None:
    why = skills.check_fields(body.name, body.title, body.when_to_use, body.body)
    if why:
        raise HTTPException(422, why[0].upper() + why[1:] + ".")
    if body.scope not in ("team", "agent"):
        raise HTTPException(422, "scope is team or agent")
    if body.scope == "agent":
        with db.session() as s:
            if not body.agent_id or not s.get(AgentDef, body.agent_id):
                raise HTTPException(422, "choose the agent this skill belongs to")


@router.post("/api/skills", status_code=201)
def create_skill(body: SkillIn, request: Request):
    _check(body)
    if skills.find(body.name):
        raise HTTPException(409, f"a skill named {body.name} already exists")
    user = getattr(request.state, "user", None)
    k = skills.create(name=body.name, title=body.title, when_to_use=body.when_to_use, body=body.body,
                      tools=body.tools, script=body.script, scope=body.scope, agent_id=body.agent_id,
                      created_by=user.username if user else "you", note=body.note or "created")
    return get_skill(k.id)


@router.put("/api/skills/{skill_id}")
def update_skill(skill_id: str, body: SkillIn, request: Request):
    _check(body)
    with db.session() as s:
        k = s.get(Skill, skill_id)
        if not k:
            raise HTTPException(404)
        other = s.exec(select(Skill).where(Skill.name == body.name, Skill.id != skill_id)).first()
        if other:
            raise HTTPException(409, f"a skill named {body.name} already exists")
        k.name, k.scope = body.name, body.scope
        k.agent_id = body.agent_id if body.scope == "agent" else None
        s.add(k)
        s.commit()
    user = getattr(request.state, "user", None)
    skills.revise(skill_id, title=body.title, when_to_use=body.when_to_use, body=body.body,
                  tools=body.tools, script=body.script, created_by=user.username if user else "you",
                  note=body.note or "edited")
    return get_skill(skill_id)


class StatusIn(BaseModel):
    status: str


@router.post("/api/skills/{skill_id}/status")
def set_status(skill_id: str, body: StatusIn):
    if body.status not in ("active", "retired"):
        raise HTTPException(422, "status is active or retired")
    with db.session() as s:
        k = s.get(Skill, skill_id)
        if not k:
            raise HTTPException(404)
        k.status = body.status
        s.add(k)
        s.commit()
    return get_skill(skill_id)


class RollbackIn(BaseModel):
    version: int


@router.post("/api/skills/{skill_id}/rollback")
def rollback(skill_id: str, body: RollbackIn, request: Request):
    user = getattr(request.state, "user", None)
    try:
        skills.rollback(skill_id, body.version, by=user.username if user else "you")
    except LookupError:
        raise HTTPException(404, "no such version") from None
    return get_skill(skill_id)


# ------------------------------------------------------------------ proposals


def _proposal_out(p: SkillProposal) -> dict[str, Any]:
    current = None
    if p.skill_id:
        with db.session() as s:
            current = s.get(Skill, p.skill_id)
    out = p.model_dump()
    if current is not None and p.status == "pending":
        out["current"] = {"version": current.version, "title": current.title,
                          "when_to_use": current.when_to_use, "body": current.body}
        out["diff"] = skills.diff(f"# {current.title}\nWhen: {current.when_to_use}\n\n{current.body}",
                                  f"# {p.title}\nWhen: {p.when_to_use}\n\n{p.body}")
        out["stale"] = p.base_version is not None and current.version != p.base_version
    return out


@router.get("/api/skill-proposals")
def list_proposals(status: str | None = None, limit: int = 100):
    q = select(SkillProposal).order_by(SkillProposal.created_at.desc()).limit(max(1, min(limit, 500)))
    if status:
        q = q.where(SkillProposal.status == status)
    with db.session() as s:
        rows = s.exec(q).all()
    return [_proposal_out(p) for p in rows]


@router.get("/api/skill-proposals/{proposal_id}")
def get_proposal(proposal_id: str):
    with db.session() as s:
        p = s.get(SkillProposal, proposal_id)
    if not p:
        raise HTTPException(404)
    return _proposal_out(p)


class ProposalDecision(BaseModel):
    approve: bool
    note: str = ""
    # Your edits before approving (any left out stay as proposed).
    name: str | None = None
    title: str | None = None
    when_to_use: str | None = None
    body: str | None = None
    scope: str | None = None


@router.post("/api/skill-proposals/{proposal_id}/decide")
async def decide(proposal_id: str, body: ProposalDecision, request: Request):
    user = getattr(request.state, "user", None)
    edits = body.model_dump(exclude={"approve", "note"}, exclude_none=True)
    if edits.get("scope") not in (None, "team", "agent"):
        raise HTTPException(422, "scope is team or agent")
    try:
        p = await request.app.state.runtime.decide_skill_proposal(
            proposal_id, body.approve, note=body.note, edits=edits,
            by=user.username if user else "you")
    except LookupError:
        raise HTTPException(404) from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return _proposal_out(p)
