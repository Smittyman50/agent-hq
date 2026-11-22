"""Skills (Settings → Skills), agents' proposals for new or better ones, Agent Skills
(SKILL.md) import and export, and upkeep (staleness, the skill review, archiving)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlmodel import select

from .. import db, skill_upkeep, skills, skills_io
from ..models import AgentDef, Skill, SkillProposal, SkillReview, SkillVersion

router = APIRouter(tags=["skills"])


def _names() -> dict[str, str]:
    with db.session() as s:
        return {a.id: a.name for a in s.exec(select(AgentDef)).all()}


def _out(k: Skill, st: dict | None = None, names: dict | None = None,
         cfg: dict | None = None) -> dict[str, Any]:
    g = (st or {}).get(k.id) or {"uses": 0, "done": 0, "partial": 0, "failed": 0, "pending": 0,
                                 "up": 0, "down": 0, "last_used_at": None}
    finished = g["done"] + g["partial"] + g["failed"]
    out = {**k.model_dump(exclude={"files"}), "status": skills.status_of(k),
           "files": skills.file_listing(k), "frontmatter": k.frontmatter or {},
           "agent": (names or {}).get(k.agent_id or "", ""), "stats": g,
           "success_rate": round(g["done"] / finished, 2) if finished else None}
    out["stale"] = skill_upkeep.flags(k, g, cfg) if out["status"] == "active" else []
    return out


@router.get("/api/skills")
def list_skills(status: str = "active", include_retired: bool = False, stale: bool = False):
    """status: active | archived | all (include_retired=true is the old spelling of all)."""
    if include_retired:
        status = "all"
    if status not in ("active", "archived", "all"):
        raise HTTPException(422, "status is active, archived or all")
    with db.session() as s:
        q = select(Skill).order_by(Skill.name)
        if status == "active":
            q = q.where(Skill.status == "active")
        elif status == "archived":
            q = q.where(Skill.status.in_(skills.ARCHIVED))
        rows = s.exec(q).all()
    st, names, cfg = skills.stats([k.id for k in rows]), _names(), skill_upkeep.get_settings()
    out = [_out(k, st, names, cfg) for k in rows]
    return [k for k in out if k["stale"]] if stale else out


# ------------------------------------------------------------------ export


def _download(data: bytes | str, filename: str, media: str) -> Response:
    return Response(content=data, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/api/skills/export")
def export_all(status: str = "active", ids: str = ""):
    """Skills as a zip of Agent Skills folders (all active ones, or those in `ids`)."""
    with db.session() as s:
        q = select(Skill).order_by(Skill.name)
        if ids:
            q = q.where(Skill.id.in_([i for i in ids.split(",") if i]))
        elif status == "active":
            q = q.where(Skill.status == "active")
        elif status == "archived":
            q = q.where(Skill.status.in_(skills.ARCHIVED))
        rows = list(s.exec(q).all())
    if not rows:
        raise HTTPException(404, "no skills to export")
    return _download(skills_io.write_zip(rows), "agent-hq-skills.zip", "application/zip")


@router.get("/api/skills/{skill_id}/export")
def export_skill(skill_id: str, format: str = ""):
    """One skill: its SKILL.md, or a zip of its folder when it has files (format=md|zip)."""
    with db.session() as s:
        k = s.get(Skill, skill_id)
    if not k:
        raise HTTPException(404)
    fmt = format or ("zip" if k.files else "md")
    if fmt == "zip":
        return _download(skills_io.write_zip([k]), f"{skills_io.folder_name(k)}.zip", "application/zip")
    if fmt != "md":
        raise HTTPException(422, "format is md or zip")
    return _download(skills_io.render(k), "SKILL.md", "text/markdown; charset=utf-8")


@router.get("/api/skills/{skill_id}/files")
def skill_file(skill_id: str, path: str):
    with db.session() as s:
        k = s.get(Skill, skill_id)
    if not k:
        raise HTTPException(404)
    got = skills.read_file(k, path, limit=1_000_000)
    if "error" in got:
        raise HTTPException(404, got["error"])
    return got


# ------------------------------------------------------------------ import


def _who(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return user.username if user else "you"


def _scope(scope: str, agent_id: str | None) -> None:
    if scope not in ("team", "agent"):
        raise HTTPException(422, "scope is team or agent")
    if scope == "agent":
        with db.session() as s:
            if not agent_id or not s.get(AgentDef, agent_id):
                raise HTTPException(422, "choose the agent the imported skills belong to")


def _import_out(found: list, problems: list[str], results: list[dict]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"found": len(found), "results": results, "problems": problems, "counts": counts}


@router.post("/api/skills/import")
async def import_files(request: Request, files: list[UploadFile] = File(...),
                       paths: str = Form(""), scope: str = Form("team"), agent_id: str = Form("")):
    """Upload a SKILL.md, a skill's folder (send each file with its relative path in
    `paths`, a JSON list in the same order), or zips. Each skill found becomes a proposal."""
    _scope(scope, agent_id or None)
    try:
        rel = json.loads(paths) if paths else []
    except ValueError:
        raise HTTPException(422, "paths is a JSON list of relative paths") from None
    got = []
    total = 0
    for i, f in enumerate(files):
        data = await f.read()
        total += len(data)
        if total > skills_io.MAX_UNPACKED:
            raise HTTPException(413, f"upload more than {skills_io.MAX_UNPACKED // 1_000_000} MB at a time")
        got.append(((rel[i] if i < len(rel) and rel[i] else f.filename) or "SKILL.md", data))
    found, problems = skills_io.from_files(got)
    names = [n for n, _ in got]
    if len(names) == 1:
        origin = names[0]
    else:
        tops = {n.replace("\\", "/").split("/")[0] for n in names}
        origin = (next(iter(tops)) + "/") if len(tops) == 1 else f"{len(names)} uploaded files"
    results = await skills_io.propose(found, origin=origin, scope=scope, agent_id=agent_id or None,
                                      by=_who(request))
    return _import_out(found, problems, results)


class ImportText(BaseModel):
    text: str
    scope: str = "team"
    agent_id: str | None = None
    origin: str = "pasted SKILL.md"


@router.post("/api/skills/import/text")
async def import_text(body: ImportText, request: Request):
    _scope(body.scope, body.agent_id)
    try:
        found = [skills_io.parse(body.text)]
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    results = await skills_io.propose(found, origin=body.origin or "pasted SKILL.md", scope=body.scope,
                                      agent_id=body.agent_id, by=_who(request))
    return _import_out(found, [], results)


class ImportGit(BaseModel):
    url: str
    branch: str = ""
    path: str = ""
    username: str = ""
    token: str = ""  # used for this clone only; not kept
    scope: str = "team"
    agent_id: str | None = None


@router.post("/api/skills/import/git")
async def import_git(body: ImportGit, request: Request):
    """Every SKILL.md in a repository (or under a path in it). Links to a folder work too."""
    _scope(body.scope, body.agent_id)
    try:
        found, problems, origin = await skills_io.from_git(body.url, branch=body.branch, path=body.path,
                                                           username=body.username, token=body.token)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(422, f"Couldn't read the repository: {e}") from None
    results = await skills_io.propose(found, origin=origin, scope=body.scope, agent_id=body.agent_id,
                                      by=_who(request))
    return _import_out(found, problems, results)


# ------------------------------------------------------------------ upkeep


@router.get("/api/skills/upkeep")
def upkeep_settings():
    return {"settings": skill_upkeep.get_settings(), **skill_upkeep.status()}


@router.put("/api/skills/upkeep")
def save_upkeep(values: dict[str, Any]):
    try:
        cfg = skill_upkeep.save_settings(values)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e)) from None
    return {"settings": cfg, **skill_upkeep.status()}


@router.get("/api/skills/reviews")
def list_reviews(limit: int = 20):
    with db.session() as s:
        rows = s.exec(select(SkillReview).order_by(SkillReview.started_at.desc())
                      .limit(max(1, min(limit, 100)))).all()
        ids = [i for r in rows for i in (r.proposal_ids or [])]
        props = {p.id: p for p in s.exec(select(SkillProposal).where(SkillProposal.id.in_(ids))).all()} if ids else {}
    return [{**r.model_dump(), "proposals": [
        {"id": i, "name": props[i].name, "action": props[i].action, "status": props[i].status}
        for i in (r.proposal_ids or []) if i in props]} for r in rows]


@router.post("/api/skills/reviews", status_code=202)
async def run_review(request: Request):
    with db.session() as s:
        running = s.exec(select(SkillReview).where(SkillReview.status == "running")).first()
    if running is not None:
        raise HTTPException(409, "a skill review is already running")
    r = skill_upkeep.start(request.app.state.runtime, trigger="manual")
    return r.model_dump()


@router.get("/api/skills/{skill_id}")
def get_skill(skill_id: str):
    with db.session() as s:
        k = s.get(Skill, skill_id)
        if not k:
            raise HTTPException(404)
        versions = s.exec(select(SkillVersion).where(SkillVersion.skill_id == skill_id)
                          .order_by(SkillVersion.version.desc())).all()
    return {**_out(k, skills.stats([k.id]), _names()),
            "versions": [{**v.model_dump(exclude={"files"}),
                          "files": [f.get("path", "") for f in (v.files or [])]} for v in versions]}


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
    reason: str = ""


@router.post("/api/skills/{skill_id}/status")
def set_status(skill_id: str, body: StatusIn, request: Request):
    """active (restore) or archived ("retired" is the old word for archived)."""
    if body.status not in ("active", "archived", "retired"):
        raise HTTPException(422, "status is active or archived")
    try:
        if body.status == "active":
            skills.restore(skill_id)
        else:
            skills.archive(skill_id, reason=body.reason, by=_who(request))
    except LookupError:
        raise HTTPException(404) from None
    return get_skill(skill_id)


class ArchiveIn(BaseModel):
    reason: str = ""


@router.post("/api/skills/{skill_id}/archive")
def archive_skill(skill_id: str, request: Request, body: ArchiveIn | None = None):
    return set_status(skill_id, StatusIn(status="archived", reason=(body.reason if body else "")), request)


@router.post("/api/skills/{skill_id}/restore")
def restore_skill(skill_id: str, request: Request):
    return set_status(skill_id, StatusIn(status="active"), request)


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
    out = {**p.model_dump(exclude={"files"}), "files": [f.get("path", "") for f in (p.files or [])]}
    if p.merge_ids:
        with db.session() as s:
            out["merging"] = [{"id": k.id, "name": k.name, "title": k.title}
                              for k in s.exec(select(Skill).where(Skill.id.in_(p.merge_ids))).all()]
    if current is not None and p.status in ("pending", "testing") and p.action != "archive":
        out["current"] = {"version": current.version, "title": current.title,
                          "when_to_use": current.when_to_use, "body": current.body}
        out["diff"] = skills.diff(_as_text(current.title, current.when_to_use, current.body, current.files),
                                  _as_text(p.title, p.when_to_use, p.body,
                                           p.files if p.source == "import" else current.files))
        out["stale"] = p.base_version is not None and current.version != p.base_version
    if current is not None and p.action == "archive":
        out["current"] = {"version": current.version, "title": current.title,
                          "when_to_use": current.when_to_use, "body": current.body}
    return out


def _as_text(title: str, when: str, body: str, files: list[dict] | None) -> str:
    text = f"# {title}\nWhen: {when}\n\n{body}"
    if files:
        text += "\n\nFiles:\n" + "\n".join(f"- {f.get('path', '')} ({len(f.get('content', ''))} chars)"
                                          for f in files)
    return text


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
