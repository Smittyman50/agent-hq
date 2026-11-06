"""Health, live workers and queue depth."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlmodel import select

from .. import db
from ..models import AgentDef

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/workers")
async def workers(request: Request):
    return await request.app.state.broker.workers()


@router.get("/queues")
async def queues(request: Request):
    """Pending (claimed but unacknowledged) and waiting entries per pool in use."""
    broker = request.app.state.broker
    with db.session() as s:
        pools = sorted({a.pool for a in s.exec(select(AgentDef)).all()} | {"default"})
    out = []
    for p in pools:
        length = await broker.r.xlen(broker.stream(p))
        out.append({"pool": p, "entries": length, "in_progress": await broker.pending_count(p)})
    return out


@router.get("/status")
async def status(request: Request):
    app = request.app
    bridge = getattr(app.state, "mqtt", None)
    ok = True
    try:
        await app.state.broker.r.ping()
    except Exception:
        ok = False
    return {"valkey": ok,
            "mqtt": None if bridge is None else bridge.connected.is_set(),
            "embedded_worker": getattr(app.state, "worker", None) is not None}
