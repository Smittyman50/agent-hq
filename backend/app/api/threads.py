"""Conversations, messages, jobs and the live event stream."""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import attachments
from ..config import settings
from ..db import get_session
from ..models import AgentDef, Job, Message, Thread

router = APIRouter(tags=["threads"])


class ThreadIn(BaseModel):
    agent_id: str
    title: str | None = None


class MessageIn(BaseModel):
    content: str = ""
    # Files uploaded with POST /api/threads/{id}/attachments, sent with this message.
    attachment_ids: list[str] = []


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
    """Queues a turn for the thread's agent; the reply arrives over the WebSocket."""
    if not s.get(Thread, thread_id):
        raise HTTPException(404)
    if not body.content.strip() and not body.attachment_ids:
        raise HTTPException(422, "say something or attach a file")
    if len(body.attachment_ids) > settings.attachment_max_files:
        raise HTTPException(422, f"at most {settings.attachment_max_files} files on one message")
    try:
        files = attachments.in_thread(thread_id, body.attachment_ids)
    except attachments.AttachmentError as e:
        raise HTTPException(422, str(e)) from None
    if any(a.message_id is not None or a.source != "user" for a in files):
        raise HTTPException(409, "a file was already sent; attach it again")
    job = await request.app.state.runtime.enqueue_turn(thread_id, body.content,
                                                       attachments=[a.id for a in files])
    return {"accepted": True, "job_id": job.id, "pool": job.pool}


@router.post("/api/threads/{thread_id}/stop")
async def stop_thread(thread_id: str, request: Request, s: Session = Depends(get_session)):
    """Stop the conversation's running turn and everything it delegated (Stop in the UI)."""
    if not s.get(Thread, thread_id):
        raise HTTPException(404)
    return await request.app.state.runtime.stop_thread(thread_id)


@router.get("/api/threads/{thread_id}/jobs", response_model=list[Job])
def list_jobs(thread_id: str, s: Session = Depends(get_session)):
    return s.exec(select(Job).where(Job.origin_thread_id == thread_id)
                  .order_by(Job.created_at.desc())).all()


@router.websocket("/ws/threads/{thread_id}")
async def thread_events(ws: WebSocket, thread_id: str):
    await ws.accept()
    broker = ws.app.state.broker
    events = broker.events(thread_id)
    try:
        async for event in events:
            await ws.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(events.aclose(), 2)
