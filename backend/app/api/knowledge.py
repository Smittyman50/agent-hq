"""The knowledge library: collections, sources, documents, test search, and turning a
document (a runbook) into skill proposals."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlmodel import func, select

from .. import db, knowledge
from ..models import AgentDef, KnowledgeChunk, KnowledgeCollection, KnowledgeDoc, KnowledgeSource

router = APIRouter(tags=["knowledge"])

_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _start(src_id: str, full: bool = False) -> bool:
    """Index a source now, in the background (or leave it queued if it's busy)."""
    if not knowledge.claim(src_id):
        return False

    async def go():
        try:
            await knowledge.index_source(src_id, full=full)
        except Exception:
            knowledge.log.exception("indexing %s failed", src_id)

    _spawn(go())
    return True


# --------------------------------------------------------------------------- settings


@router.get("/api/knowledge/settings")
def get_settings():
    return knowledge.get_settings()


@router.put("/api/knowledge/settings")
def put_settings(body: dict[str, Any]):
    try:
        return knowledge.save_settings(body)
    except (TypeError, ValueError) as e:
        raise HTTPException(422, str(e)) from None


# --------------------------------------------------------------------------- collections


class CollectionIn(BaseModel):
    name: str
    description: str = ""
    agent_ids: list[str] = Field(default_factory=list)
    auto_recall: bool = True
    enabled: bool = True


def _collection_out(c: KnowledgeCollection) -> dict[str, Any]:
    with db.session() as s:
        docs = s.exec(select(func.count()).select_from(KnowledgeDoc).where(KnowledgeDoc.collection_id == c.id)).one()
        chunks = s.exec(select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.collection_id == c.id)).one()
        sources = s.exec(select(func.count()).select_from(KnowledgeSource).where(KnowledgeSource.collection_id == c.id)).one()
    return {**c.model_dump(), "docs": docs, "chunks": chunks, "sources": sources}


@router.get("/api/knowledge/collections")
def list_collections():
    with db.session() as s:
        rows = list(s.exec(select(KnowledgeCollection).order_by(KnowledgeCollection.name)).all())
    return [_collection_out(c) for c in rows]


@router.post("/api/knowledge/collections", status_code=201)
def create_collection(body: CollectionIn):
    if not body.name.strip():
        raise HTTPException(422, "a collection needs a name")
    c = KnowledgeCollection(**body.model_dump())
    c.name = c.name.strip()
    with db.session() as s:
        s.add(c)
        s.commit()
        s.refresh(c)
    return _collection_out(c)


@router.put("/api/knowledge/collections/{collection_id}")
def update_collection(collection_id: str, body: CollectionIn):
    with db.session() as s:
        c = s.get(KnowledgeCollection, collection_id)
        if c is None:
            raise HTTPException(404)
        for k, v in body.model_dump().items():
            setattr(c, k, v.strip() if k == "name" else v)
        c.updated_at = knowledge._now()
        s.add(c)
        s.commit()
        s.refresh(c)
    return _collection_out(c)


@router.delete("/api/knowledge/collections/{collection_id}")
def delete_collection(collection_id: str):
    knowledge.delete_collection(collection_id)
    return {"ok": True}


# --------------------------------------------------------------------------- sources


class SourceIn(BaseModel):
    kind: str = "upload"  # upload | url | git
    name: str = ""
    url: str | None = None
    branch: str = ""
    username: str = ""
    token: str | None = None  # None = keep the saved one; "" = remove it
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    obsidian: bool = False
    vault_name: str = ""
    refresh_minutes: int = 0


def _source_out(x: KnowledgeSource) -> dict[str, Any]:
    out = x.model_dump(exclude={"token"})
    out["has_token"] = bool(x.token)
    return out


def _validate(body: SourceIn) -> None:
    if body.kind not in ("upload", "url", "git"):
        raise HTTPException(422, "kind is upload, url or git")
    if body.kind in ("url", "git"):
        url = (body.url or "").strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(422, "the URL must start with http:// or https://")
    if body.refresh_minutes < 0:
        raise HTTPException(422, "refresh_minutes can't be negative")


@router.get("/api/knowledge/collections/{collection_id}/sources")
def list_sources(collection_id: str):
    with db.session() as s:
        rows = s.exec(select(KnowledgeSource).where(KnowledgeSource.collection_id == collection_id)
                      .order_by(KnowledgeSource.created_at)).all()
    return [_source_out(x) for x in rows]


@router.post("/api/knowledge/collections/{collection_id}/sources", status_code=201)
async def create_source(collection_id: str, body: SourceIn):
    _validate(body)
    with db.session() as s:
        if s.get(KnowledgeCollection, collection_id) is None:
            raise HTTPException(404, "no such collection")
    data = body.model_dump()
    data["url"] = (data["url"] or "").strip() or None
    if body.kind == "git" and not data["include"]:
        data["include"] = list(knowledge.DEFAULT_INCLUDE)
    if body.kind == "git" and not data["exclude"]:
        data["exclude"] = list(knowledge.DEFAULT_EXCLUDE)
    if not data["name"] and body.kind != "url":  # a web page is named after its title
        data["name"] = {"upload": "Uploads", "url": "",
                        "git": knowledge._repo_name(data["url"] or "") or "Repository"}[body.kind]
    x = KnowledgeSource(collection_id=collection_id,
                        status="ready" if body.kind == "upload" else "queued", **data)
    with db.session() as s:
        s.add(x)
        s.commit()
        s.refresh(x)
    if x.kind != "upload":
        _start(x.id)
    return _source_out(x)


@router.put("/api/knowledge/sources/{source_id}")
def update_source(source_id: str, body: SourceIn):
    _validate(body)
    with db.session() as s:
        x = s.get(KnowledgeSource, source_id)
        if x is None:
            raise HTTPException(404)
        reclone = x.kind == "git" and ((body.url or "").strip() != (x.url or "") or body.branch != x.branch)
        for k, v in body.model_dump().items():
            if k == "kind" or (k == "token" and v is None):
                continue
            setattr(x, k, (v.strip() or None) if k in ("url", "token") and isinstance(v, str) else v)
        if reclone:
            x.last_commit = ""
        s.add(x)
        s.commit()
        s.refresh(x)
    if reclone:
        import shutil

        shutil.rmtree(knowledge.root() / "git" / source_id, ignore_errors=True)
    return _source_out(x)


@router.delete("/api/knowledge/sources/{source_id}")
def delete_source(source_id: str):
    knowledge.delete_source(source_id)
    return {"ok": True}


@router.post("/api/knowledge/sources/{source_id}/reindex", status_code=202)
async def reindex(source_id: str, full: bool = False):
    """Check for changes now (full=true re-reads and re-embeds everything)."""
    with db.session() as s:
        if s.get(KnowledgeSource, source_id) is None:
            raise HTTPException(404)
    if not _start(source_id, full=full):
        raise HTTPException(409, "it's indexing right now")
    return {"status": "indexing"}


@router.post("/api/knowledge/sources/{source_id}/files")
async def upload_files(source_id: str, files: list[UploadFile] = File(...)):
    with db.session() as s:
        x = s.get(KnowledgeSource, source_id)
    if x is None:
        raise HTTPException(404)
    if x.kind != "upload":
        raise HTTPException(422, "files can only be added to an upload source")
    added, errors = [], []
    loop = asyncio.get_running_loop()
    for f in files:
        data = await f.read()
        try:
            doc = await loop.run_in_executor(None, lambda: knowledge.add_upload(x, f.filename or "file", data))
            added.append({"id": doc.id, "title": doc.title, "path": doc.path})
        except Exception as e:
            errors.append(str(e))
    if added:
        with db.session() as s:
            row = s.get(KnowledgeSource, source_id)
            if row.status != "indexing":
                row.status = "queued"
                s.add(row)
                s.commit()
        _start(source_id)
    return {"added": added, "errors": errors}


# --------------------------------------------------------------------------- documents


def _doc_out(d: KnowledgeDoc, full: bool = False) -> dict[str, Any]:
    out = d.model_dump(exclude={"text"} if not full else set())
    out["indexed"] = d.indexed_hash == d.hash
    return out


@router.get("/api/knowledge/collections/{collection_id}/docs")
def list_docs(collection_id: str, q: str = "", source_id: str | None = None, limit: int = 200, offset: int = 0):
    query = select(KnowledgeDoc).where(KnowledgeDoc.collection_id == collection_id)
    if source_id:
        query = query.where(KnowledgeDoc.source_id == source_id)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(KnowledgeDoc.title.ilike(like) | KnowledgeDoc.path.ilike(like))
    with db.session() as s:
        total = s.exec(select(func.count()).select_from(query.subquery())).one()
        rows = s.exec(query.order_by(KnowledgeDoc.path).offset(max(0, offset)).limit(max(1, min(limit, 1000)))).all()
    return {"total": total, "docs": [_doc_out(d) for d in rows]}


@router.get("/api/knowledge/docs/{doc_id}")
def get_doc(doc_id: str):
    with db.session() as s:
        d = s.get(KnowledgeDoc, doc_id)
        if d is None:
            raise HTTPException(404)
        chunks = s.exec(select(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc_id)
                        .order_by(KnowledgeChunk.ord)).all()
    return {**_doc_out(d, full=True),
            "sections": [{"heading": c.heading, "text": c.text, "embedded": bool(c.embedding_model)}
                         for c in chunks]}


@router.delete("/api/knowledge/docs/{doc_id}")
def delete_doc(doc_id: str):
    with db.session() as s:
        d = s.get(KnowledgeDoc, doc_id)
        if d is None:
            raise HTTPException(404)
        src = s.get(KnowledgeSource, d.source_id)
    if src and src.kind != "upload":
        raise HTTPException(422, "remove it from the repository or page instead; it would come back on the next sync")
    knowledge.remove_docs([doc_id])
    if src:
        docs, chunks = knowledge._counts(src.id)
        knowledge._set(src.id, docs=docs, chunks=chunks)
    return {"ok": True}


@router.get("/api/knowledge/search")
async def test_search(q: str, collection_id: str | None = None, agent_id: str | None = None, limit: int = 8):
    """Try a search: as a given agent would see it, or across one or all collections."""
    agent = None
    if agent_id:
        with db.session() as s:
            agent = s.get(AgentDef, agent_id)
        if agent is None:
            raise HTTPException(404, "no such agent")
    cols = knowledge.readable_collections(agent)
    if collection_id:
        cols = [c for c in cols if c.id == collection_id]
    hits = await knowledge.search(q, collection_ids=[c.id for c in cols], k=max(1, min(limit, 20)))
    cfg = knowledge.get_settings()
    return {"results": [{**h.out(600), "sim": None if h.sim is None else round(h.sim, 3),
                         "keyword": round(h.keyword, 3), "would_recall": h.score >= cfg["min_score"]}
                        for h in hits],
            "min_score": cfg["min_score"]}


# --------------------------------------------------------------------------- runbook → skills


class ProposeIn(BaseModel):
    agent_id: str


@router.post("/api/knowledge/docs/{doc_id}/propose-skills")
async def propose_skills(doc_id: str, body: ProposeIn, request: Request):
    """Read a document (a runbook, a how-to) and propose skills from it, as that agent.
    The proposals follow the agent's learning mode, like any other."""
    from .. import learning

    with db.session() as s:
        d = s.get(KnowledgeDoc, doc_id)
        agent = s.get(AgentDef, body.agent_id)
    if d is None or agent is None:
        raise HTTPException(404)
    try:
        return await learning.skills_from_document(request.app.state.runtime, agent, d)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
