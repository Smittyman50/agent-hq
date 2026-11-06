"""Conversations, messages, jobs and the live event stream."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_session
from ..events import bus
from ..models import AgentDef, Job, Message, Thread

router = APIRouter(tags=["threads"])


class ThreadIn(BaseModel):
    agent_id: str
    title: str | None = None


class MessageIn(BaseModel):
    content: str


@router.get("/api/threads", response_model=list[Thread])
def list_threads(agent_id: str | None = None, s: Session = Depends(get_session)):
    q = select(Thread).order_by(Thread.created_at.desc())
    if agent_id:
        q = q.where(Thread.agent_id == agent_id)
    return s.exec(q).all()


@router.post("/api/threads", response_model=Thread, status_code=201)
def create_thread(body: ThreadIn, s: Session = Depends(get_session)):
    agent = s.get(AgentDef, body.agent_id)
    if not agent or not agent.enabled:
        raise HTTPException(404, "agent not found")
    t = Thread(agent_id=agent.id, title=body.title or f"Chat with {agent.name}")
    s.add(t)
    s.commit()
    s.refresh(t)
    return t


@router.get("/api/threads/{thread_id}/messages", response_model=list[Message])
def list_messages(thread_id: str, s: Session = Depends(get_session)):
    return s.exec(select(Message).where(Message.thread_id == thread_id)
                  .order_by(Message.created_at)).all()


@router.post("/api/threads/{thread_id}/messages", status_code=202)
async def post_message(thread_id: str, body: MessageIn, request: Request,
                       s: Session = Depends(get_session)):
    """Starts the turn and returns at once; the reply arrives over the WebSocket."""
    if not s.get(Thread, thread_id):
        raise HTTPException(404)
    rt = request.app.state.runtime
    rt._spawn(_safe_turn(rt, thread_id, body.content))
    return {"accepted": True}


async def _safe_turn(rt, thread_id: str, content: str) -> None:
    try:
        await rt.run_turn(thread_id, content)
    except Exception:
        pass  # already recorded in the thread as an error event


@router.get("/api/threads/{thread_id}/jobs", response_model=list[Job])
def list_jobs(thread_id: str, s: Session = Depends(get_session)):
    return s.exec(select(Job).where(Job.origin_thread_id == thread_id)
                  .order_by(Job.created_at.desc())).all()


@router.websocket("/ws/threads/{thread_id}")
async def thread_events(ws: WebSocket, thread_id: str):
    await ws.accept()
    q = bus.subscribe(thread_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=25)
            except asyncio.TimeoutError:
                event = {"type": "ping"}
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(thread_id, q)
