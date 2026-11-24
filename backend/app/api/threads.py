"""Conversations, messages, jobs and the live event stream."""

from __future__ import annotations

import asyncio
import contextlib
import re

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from .. import attachments, huddles, nextactions, scratchpad
from ..config import settings
from ..db import get_session
from ..models import AgentDef, Job, Message, Role, Thread

router = APIRouter(tags=["threads"])


class ThreadIn(BaseModel):
    agent_id: str
    title: str | None = None


class MemberIn(BaseModel):
    agent_id: str


class MessageIn(BaseModel):
    content: str = ""
    # Files uploaded with POST /api/threads/{id}/attachments, sent with this message.
    attachment_ids: list[str] = []


@router.get("/api/threads", response_model=list[Thread])
def list_threads(agent_id: str | None = None, s: Session = Depends(get_session)):
    q = (select(Thread).where(Thread.deleted_at.is_(None))
         .order_by(func.coalesce(Thread.updated_at, Thread.created_at).desc()))
    rows = s.exec(q).all()
    if agent_id:  # conversations it started, and huddles it's in
        rows = [t for t in rows if t.agent_id == agent_id or huddles.is_member(t, agent_id)]
    return rows


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


@router.delete("/api/threads/{thread_id}", status_code=204)
async def delete_thread(thread_id: str, request: Request, s: Session = Depends(get_session)):
    """Delete a chat from the sidebar. Anything still running in it is stopped first."""
    t = s.get(Thread, thread_id)
    if not t or t.deleted_at is not None:
        raise HTTPException(404)
    await request.app.state.runtime.stop_thread(thread_id)
    t.deleted_at = datetime.now(timezone.utc)
    s.add(t)
    s.commit()


@router.get("/api/threads/{thread_id}/messages", response_model=list[Message])
def list_messages(thread_id: str, s: Session = Depends(get_session)):
    return s.exec(select(Message).where(Message.thread_id == thread_id)
                  .order_by(Message.created_at)).all()


@router.post("/api/threads/{thread_id}/messages", status_code=202)
async def post_message(thread_id: str, body: MessageIn, request: Request,
                       s: Session = Depends(get_session)):
    """Queues a turn for the thread's agent; the reply arrives over the WebSocket."""
    t = s.get(Thread, thread_id)
    if not t or t.deleted_at is not None:
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
    jobs = await request.app.state.runtime.enqueue_turns(thread_id, body.content,
                                                         attachments=[a.id for a in files])
    job = jobs[0]
    return {"accepted": True, "job_id": job.id, "pool": job.pool,
            "agent_id": job.assignee_agent_id, "thread": _fresh(thread_id)}


def _fresh(thread_id: str) -> dict:
    """The conversation as it is now (members may have changed), for the UI."""
    from .. import db

    with db.session() as s:
        t = s.get(Thread, thread_id)
        return t.model_dump(mode="json") if t else {}


def _live(s: Session, thread_id: str) -> Thread:
    t = s.get(Thread, thread_id)
    if not t or t.deleted_at is not None:
        raise HTTPException(404)
    return t


# ------------------------------------------------------------------ huddles


@router.post("/api/threads/{thread_id}/members", response_model=Thread)
async def add_member(thread_id: str, body: MemberIn, request: Request,
                     s: Session = Depends(get_session)):
    """Add an agent to the conversation; with a second agent it becomes a huddle."""
    _live(s, thread_id)
    try:
        t, joined = huddles.add_member(thread_id, body.agent_id)
    except huddles.HuddleError as e:
        raise HTTPException(422, str(e)) from None
    if joined:
        await request.app.state.runtime.member_joined(t, body.agent_id)
    return t


@router.delete("/api/threads/{thread_id}/members/{agent_id}", response_model=Thread)
async def remove_member(thread_id: str, agent_id: str, request: Request,
                        s: Session = Depends(get_session)):
    """Take an agent out of the conversation (it stays a huddle). Its work there stays."""
    _live(s, thread_id)
    try:
        t = huddles.remove_member(thread_id, agent_id)
    except huddles.HuddleError as e:
        raise HTTPException(422, str(e)) from None
    rt = request.app.state.runtime
    agent = s.get(AgentDef, agent_id)
    name = agent.name if agent else "An agent"
    await rt.save_message(thread_id, Role.event, f"{name} left the huddle.", kind="huddle_leave",
                          agent_id=agent_id)
    await rt.broker.publish(thread_id, "huddle", members=huddles.members(t), lead=huddles.lead(t))
    return t


@router.put("/api/threads/{thread_id}/lead", response_model=Thread)
async def set_lead(thread_id: str, body: MemberIn, request: Request,
                   s: Session = Depends(get_session)):
    """Choose who answers messages that mention nobody."""
    _live(s, thread_id)
    try:
        t = huddles.set_lead(thread_id, body.agent_id)
    except huddles.HuddleError as e:
        raise HTTPException(422, str(e)) from None
    rt = request.app.state.runtime
    agent = s.get(AgentDef, body.agent_id)
    await rt.save_message(thread_id, Role.event, f"{agent.name} leads the huddle now.",
                          kind="huddle_lead", agent_id=agent.id)
    await rt.broker.publish(thread_id, "huddle", members=huddles.members(t), lead=huddles.lead(t))
    return t


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
async def thread_events(ws: WebSocket, thread_id: str, after: str | None = None):
    """Live events of a conversation. A reconnecting page passes the last "eid" it saw
    as ?after= to get what it missed; a fresh page gets recent activity replayed."""
    await ws.accept()
    broker = ws.app.state.broker
    if after is not None and not re.fullmatch(r"\d+-\d+", after):
        after = None
    events = broker.events(thread_id, after=after)
    try:
        async for event in events:
            await ws.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(events.aclose(), 2)


# ------------------------------------------------------------------ next actions


@router.get("/api/threads/{thread_id}/plans")
async def thread_plans(thread_id: str, request: Request, s: Session = Depends(get_session)):
    """The next actions of this conversation's recent requests, while they're kept
    (app/nextactions.py), oldest first."""
    if not s.get(Thread, thread_id):
        raise HTTPException(404)
    return await nextactions.for_thread(request.app.state.runtime.broker.r, thread_id)


# ------------------------------------------------------------------ scratch pads


@router.get("/api/jobs/{job_id}/notes")
async def job_notes(job_id: str, request: Request, s: Session = Depends(get_session)):
    """A job's scratch-pad notes while they're kept (app/scratchpad.py)."""
    job = s.get(Job, job_id)
    if not job:
        raise HTTPException(404)
    r = request.app.state.runtime.broker.r
    info = await scratchpad.info(r, job_id)
    if info is None:
        return {"job_id": job_id, "count": 0, "notes": [], "expires_in": None, "gone": True}
    return {"job_id": job_id, "count": info["count"], "agent_id": info["agent"],
            "notes": [{"n": e["n"], "at": e["at"], "text": e["text"]}
                      for e in await scratchpad.notes(r, job_id)],
            "expires_in": await scratchpad.expires_in(r, job_id), "gone": False}


@router.post("/api/jobs/{job_id}/notes/keep")
async def keep_notes(job_id: str, request: Request, s: Session = Depends(get_session)):
    """Save a job's notes as a Markdown file in its conversation, on its delegation card.
    The only way a pad reaches the database."""
    job = s.get(Job, job_id)
    if not job:
        raise HTTPException(404)
    rt = request.app.state.runtime
    entries = await scratchpad.notes(rt.broker.r, job_id)
    if not entries:
        raise HTTPException(410, "these notes have expired, or there were none")
    agent = s.get(AgentDef, job.assignee_agent_id)
    name = agent.name if agent else "agent"
    task = (job.meta or {}).get("prompt") or job.task
    text = scratchpad.markdown(name, task if job.kind == "task" else "", entries)
    # A delegated task's card shows the file; a turn's file is listed with the conversation's.
    message_id = None
    if job.kind == "task":
        message_id = (job.meta or {}).get("message_id") or next(
            (m.id for m in s.exec(select(Message).where(
                Message.thread_id == job.origin_thread_id, Message.role == Role.event)).all()
             if (m.meta or {}).get("job_id") == job_id
             and (m.meta or {}).get("kind") == "delegation_result"), None)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "agent"
    try:
        a = attachments.store(job.origin_thread_id, f"{slug}-notes-{job_id}.md",
                              text.encode("utf-8"), source="agent", agent_id=job.assignee_agent_id,
                              job_id=job_id, message_id=message_id)
    except attachments.AttachmentError as e:
        raise HTTPException(422, str(e)) from None
    if message_id:
        m = s.get(Message, message_id)
        have = list((m.meta or {}).get("attachments") or []) if m else []
        await rt.update_message(message_id, attachments=have + [attachments.summary(a)])
    return attachments.summary(a)
