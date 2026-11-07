"""Long-term memory: browse, search, add, edit, retire/restore, settings, maintenance."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import func, select

from .. import db, memory
from ..models import AgentDef, Memory, MemoryKind, MemoryScope, ModelProvider

router = APIRouter(prefix="/api/memory", tags=["memory"])
log = logging.getLogger("agent_hq.memory")


class MemoryOut(BaseModel):
    id: str
    text: str
    kind: MemoryKind
    scope: MemoryScope
    agent_id: str | None
    thread_id: str | None
    source: str
    source_job_id: str | None
    created_by: str | None
    invalidated_at: datetime | None
    invalidated_reason: str | None
    superseded_by: str | None
    last_confirmed: datetime
    times_seen: int
    embedded: bool
    created_at: datetime
    score: float | None = None


def out(m: Memory, score: float | None = None) -> MemoryOut:
    return MemoryOut(**m.model_dump(exclude={"embedding", "embedding_model", "meta", "valid_from",
                                             "updated_at"}),
                     embedded=bool(m.embedding_model), score=score)


class MemoryIn(BaseModel):
    text: str
    kind: MemoryKind = MemoryKind.fact
    scope: MemoryScope = MemoryScope.team
    agent_id: str | None = None
    supersedes: str | None = None


class RetireIn(BaseModel):
    reason: str = "retired by user"


@router.get("", response_model=list[MemoryOut])
async def list_memories(query: str = "", scope: MemoryScope | None = None, agent_id: str | None = None,
                        include_retired: bool = False, limit: int = 100):
    if query.strip():
        hits = await memory.search(query, k=limit, include_retired=include_retired, everything=True,
                                   min_score=0.05)
        rows = [(h.memory, h.score) for h in hits]
    else:
        with db.session() as s:
            q = select(Memory)
            if not include_retired:
                q = q.where(Memory.invalidated_at == None)  # noqa: E711
            rows = [(m, None) for m in s.exec(q.order_by(Memory.last_confirmed.desc()).limit(limit * 3)).all()]
    if scope:
        rows = [r for r in rows if r[0].scope == scope]
    if agent_id:
        rows = [r for r in rows if r[0].agent_id == agent_id or r[0].created_by == agent_id]
    return [out(m, sc) for m, sc in rows[:limit]]


@router.post("", response_model=MemoryOut, status_code=201)
async def create_memory(body: MemoryIn):
    if body.scope == MemoryScope.agent and not body.agent_id:
        raise HTTPException(422, "agent-scoped memories need an agent")
    if body.scope == MemoryScope.thread:
        raise HTTPException(422, "conversation memories are created by agents")
    try:
        m = await memory.add(body.text, kind=body.kind, scope=body.scope,
                             agent_id=body.agent_id if body.scope == MemoryScope.agent else None,
                             source="user", supersedes=body.supersedes)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return out(m)


@router.get("/settings")
def get_memory_settings():
    return memory.get_settings()


@router.put("/settings")
async def put_memory_settings(body: dict[str, Any], request: Request):
    for key in ("embedding_provider_id", "tidy_provider_id"):
        if body.get(key):
            with db.session() as s:
                if not s.get(ModelProvider, body[key]):
                    raise HTTPException(422, "model provider not found")
    before = memory.get_settings()
    saved = memory.save_settings(body)
    if (saved["embedding_model"], saved["embedding_provider_id"]) != \
            (before["embedding_model"], before["embedding_provider_id"]) and saved["embedding_model"]:
        _background(request, memory.reembed())  # new embedding model: re-embed everything
    return saved


@router.put("/{memory_id}", response_model=MemoryOut)
async def edit_memory(memory_id: str, body: MemoryIn):
    m = memory.get(memory_id)
    if not m:
        raise HTTPException(404)
    changed = body.text.strip() != m.text
    m = memory.update(memory_id, text=body.text.strip(), kind=body.kind, scope=body.scope,
                      agent_id=body.agent_id if body.scope == MemoryScope.agent else None)
    if changed:
        got = await memory.embed_texts([m.text])
        if got:
            memory._store_vector(m.id, got[0][0], got[1])
    return out(memory.get(memory_id))


@router.post("/{memory_id}/retire", response_model=MemoryOut)
def retire_memory(memory_id: str, body: RetireIn):
    try:
        return out(memory.retire(memory_id, body.reason))
    except LookupError as e:
        raise HTTPException(404) from e


@router.post("/{memory_id}/restore", response_model=MemoryOut)
def restore_memory(memory_id: str):
    try:
        return out(memory.restore(memory_id))
    except LookupError as e:
        raise HTTPException(404) from e


@router.post("/{memory_id}/confirm", response_model=MemoryOut)
def confirm_memory(memory_id: str):
    try:
        return out(memory.confirm(memory_id))
    except LookupError as e:
        raise HTTPException(404) from e


@router.delete("/{memory_id}", status_code=204)
def delete_memory(memory_id: str):
    with db.session() as s:
        m = s.get(Memory, memory_id)
        if not m:
            raise HTTPException(404)
        s.delete(m)
        s.commit()


@router.get("/stats")
def stats():
    cfg = memory.get_settings()
    with db.session() as s:
        total = s.exec(select(func.count()).select_from(Memory)).one()
        live = s.exec(select(func.count()).select_from(Memory).where(Memory.invalidated_at == None)).one()  # noqa: E711
        current = s.exec(select(func.count()).select_from(Memory)
                         .where(Memory.embedding_model == (cfg["embedding_model"] or None))).one()
    return {"total": total, "active": live, "retired": total - live,
            "embedded_with_current_model": current, "pgvector": memory.pgvector_enabled()}


@router.post("/reembed")
async def reembed():
    try:
        return await memory.reembed()
    except Exception as e:
        raise HTTPException(502, f"embedding failed: {e}") from e


@router.post("/tidy")
async def tidy(use_model: bool = True):
    return await memory.tidy(use_model=use_model)


_tasks: set[asyncio.Task] = set()


def _background(request: Request, coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def nightly_tidy_loop(broker) -> None:
    """Run the memory tidy-up once a night at the configured hour (local time)."""
    while True:
        try:
            cfg = memory.get_settings()
            now = datetime.now()
            if cfg["tidy_nightly"] and now.hour == int(cfg["tidy_hour"]):
                if await broker.r.set(f"ahq:memory:tidy:{now:%Y-%m-%d}", "1", nx=True, ex=2 * 86400):
                    report = await memory.tidy()
                    log.info("nightly memory tidy-up: %s", report)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("nightly memory tidy-up failed")
        await asyncio.sleep(600)
