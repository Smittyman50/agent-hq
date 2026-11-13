"""Short titles for conversations, so the sidebar says what each chat is about.

A chat started from the UI is called "Chat with <agent>" until its first reply; then the
summary model (Settings → Conversations, if you picked one) or the agent's own model names
it in a few words. If the model fails, the first thing you said is used instead.
Scheduled and event chats keep the names they were given.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from sqlmodel import select

from . import db
from .models import AgentDef, Message, Role, Thread

log = logging.getLogger("agent_hq.titles")

MAX_CHARS = 60
RULES = """Name this conversation for a sidebar list. Reply with the title only: 3 to 7
words, no quotes, no trailing punctuation, no preamble. Name the subject (e.g. Wedding
venues near Roanoke, May 2028), not the fact that it's a conversation."""

_tasks: set[asyncio.Task] = set()


def needs_title(t: Thread) -> bool:
    return t.deleted_at is None and (t.title.startswith("Chat with ") or t.title == "New conversation")


def clean(raw: str) -> str:
    """The model's answer as a sidebar title: one line, no quotes or labels, ≤ MAX_CHARS."""
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S | re.I)
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = re.sub(r"^(title|conversation title)\s*:\s*", "", line, flags=re.I)
    line = line.strip(" *#`\"'“”‘’").rstrip(" .!?:;")
    line = re.sub(r"\s+", " ", line)
    return _shorten(line)


def fallback(first_user: str) -> str:
    """Your first message, cut to fit, for when no model could name the chat."""
    text = re.sub(r"\s+", " ", first_user or "").strip()
    return _shorten(text) or "Chat"


def _shorten(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    cut = text[:MAX_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut or text[:MAX_CHARS]) + "…"


def _opening(thread_id: str) -> tuple[str, str]:
    """The first thing you said and the agent's first reply."""
    with db.session() as s:
        msgs = s.exec(select(Message).where(Message.thread_id == thread_id,
                                            Message.role.in_([Role.user, Role.agent]))
                      .order_by(Message.created_at).limit(6)).all()
    user = next((m.content for m in msgs if m.role == Role.user and m.content.strip()), "")
    agent = next((m.content for m in msgs if m.role == Role.agent and m.content.strip()), "")
    return user, agent


async def _ask_model(agent: AgentDef, user: str, reply: str) -> str:
    from pydantic_ai import Agent

    from .context import get_settings
    from .llm import build_agent_model, build_model_for

    cfg = get_settings()
    model = (build_model_for(cfg.get("provider_id"), cfg["model"], "the summary model")
             if cfg.get("model") else build_agent_model(agent))
    namer = Agent(model, instructions=RULES, name=f"{agent.name} (naming the chat)")
    prompt = f"The user said:\n{user[:1500]}"
    if reply:
        prompt += f"\n\n{agent.name} replied:\n{reply[:1500]}"
    result = await asyncio.wait_for(namer.run(prompt), timeout=90)
    return clean(str(result.output))


async def title_thread(thread_id: str, broker=None) -> str | None:
    """Name the chat if it still has its default name. Returns the new title, if any."""
    with db.session() as s:
        t = s.get(Thread, thread_id)
        agent = s.get(AgentDef, t.agent_id) if t else None
    if t is None or agent is None or not needs_title(t):
        return None
    user, reply = _opening(thread_id)
    if not user:
        return None
    try:
        title = await _ask_model(agent, user, reply)
    except Exception as e:  # noqa: BLE001  (a title is never worth failing over)
        log.warning("couldn't name chat %s with a model: %s", thread_id, e)
        title = ""
    title = title or fallback(user)
    with db.session() as s:
        t = s.get(Thread, thread_id)
        if t is None or not needs_title(t):  # renamed or deleted meanwhile
            return None
        t.title = title
        s.add(t)
        s.commit()
        s.refresh(t)
        data = json.loads(t.model_dump_json())
    if broker is not None:
        await broker.publish(thread_id, "thread_updated", thread=data)
    return title


def title_soon(thread_id: str, broker=None) -> None:
    """Name the chat in the background (the turn that triggered it doesn't wait)."""
    task = asyncio.create_task(title_thread(thread_id, broker))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def backfill(broker=None, delay: float = 20) -> None:
    """Once at start-up: name chats from before titles existed (one at a time)."""
    await asyncio.sleep(delay)
    with db.session() as s:
        ids = [t.id for t in s.exec(select(Thread).where(Thread.deleted_at.is_(None))
                                    .order_by(Thread.created_at.desc())).all() if needs_title(t)]
    for tid in ids:
        try:
            await title_thread(tid, broker)
        except Exception:  # noqa: BLE001
            log.exception("naming chat %s failed", tid)
