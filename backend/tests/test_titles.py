"""Chats get a short title from their opening exchange; the sidebar shows when they last changed."""

from __future__ import annotations

from pydantic_ai import ModelResponse, TextPart

from app import db, titles
from app.models import AgentDef, Message, Role, Thread

from .conftest import SCRIPTS, worker_fn


def test_clean_makes_a_sidebar_title():
    assert titles.clean('Title: "Wedding venues near Roanoke."') == "Wedding venues near Roanoke"
    assert titles.clean("<think>hmm</think>\n\n**APRS iGate setup**\nextra") == "APRS iGate setup"
    long = titles.clean("word " * 40)
    assert len(long) <= titles.MAX_CHARS + 1 and long.endswith("…")


def test_fallback_uses_your_first_message():
    assert titles.fallback("  find me   3 venues ") == "find me 3 venues"
    assert titles.fallback("") == "Chat"


def test_only_default_names_are_replaced():
    assert titles.needs_title(Thread(agent_id="a", title="Chat with Duke"))
    assert not titles.needs_title(Thread(agent_id="a", title="Scheduled: Morning brief"))
    assert not titles.needs_title(Thread(agent_id="a", title="Events: alerts (Duke)"))


SCRIPTS["namer"] = lambda messages, info: ModelResponse(parts=[TextPart('"May 2028 wedding venues."')])
SCRIPTS["title-worker"] = worker_fn


async def test_first_reply_names_the_chat_and_updates_it(hq):
    await hq.start_worker()
    a = await hq.agent(name="Scarlett", description="Does research", model="fake:title-worker")
    t = await hq.thread(a["id"])
    assert t["title"] == "Chat with Scarlett" and t["updated_at"] is None
    await hq.say(t["id"], "find wedding venues")
    await hq.wait_agent_replies(t["id"], 1)
    with db.session() as s:
        row = s.get(Thread, t["id"])
        assert row.updated_at is not None
        # The fake worker model doesn't answer like a namer, so check the naming step directly.
        row.title = "Chat with Scarlett"
        s.add(row)
        agent = s.get(AgentDef, a["id"])
        agent.model = "fake:namer"
        s.add(agent)
        s.commit()
    assert await titles.title_thread(t["id"]) == "May 2028 wedding venues"
    assert await titles.title_thread(t["id"]) is None  # named once
    listed = (await hq.http.get("/api/threads")).json()
    assert listed[0]["id"] == t["id"] and listed[0]["title"] == "May 2028 wedding venues"
