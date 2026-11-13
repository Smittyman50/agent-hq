"""A page that comes back to a conversation mid-run sees what it missed.

Live events go to a per-conversation stream. Opening the conversation replays recent
activity (not messages or status, which come from the database and /jobs), and a
reconnect resumes after the last event the page saw, so nothing is lost or repeated.
"""

from __future__ import annotations

import asyncio

from app import broker as broker_mod


async def _take(events, n: int, timeout: float = 5) -> list[dict]:
    out: list[dict] = []
    async def go():
        async for ev in events:
            if ev["type"] != "ping":
                out.append(ev)
            if len(out) == n:
                return
    await asyncio.wait_for(go(), timeout)
    return out


async def test_opening_replays_recent_activity_then_goes_live(hq):
    b = hq.broker
    await b.publish("t1", "delegation_started", from_agent="Duke", to_agent="Scarlett", task="x")
    await b.publish("t1", "status", state="thinking")
    await b.publish("t1", "message", message={"id": "m1"})
    await b.publish("t1", "tool_call", agent="Scarlett", tool="search_web", args={}, depth=1)
    await b.publish("other", "tool_call", agent="Snake Eyes", tool="nope", args={})

    events = b.events("t1")
    try:
        replayed = await _take(events, 2)
        assert [e["type"] for e in replayed] == ["delegation_started", "tool_call"]
        assert all(e["replay"] and e["eid"] for e in replayed)
        assert replayed[1]["tool"] == "search_web" and replayed[1]["depth"] == 1

        # Published after the backlog was read: arrives live, once, unmarked.
        await b.publish("t1", "tool_result", agent="Scarlett", tool="search_web", preview="ok")
        await b.publish("t1", "message", message={"id": "m2"})
        live = await _take(events, 2)
        assert [e["type"] for e in live] == ["tool_result", "message"]
        assert not any(e.get("replay") for e in live)
    finally:
        await events.aclose()


async def test_reconnect_resumes_after_last_event_seen(hq):
    b = hq.broker
    for i in range(3):
        await b.publish("t2", "tool_call", agent="Scarlett", tool=f"step{i}", args={})
    events = b.events("t2")
    try:
        seen = await _take(events, 3)
    finally:
        await events.aclose()

    # While the page was disconnected:
    await b.publish("t2", "message", message={"id": "m9"})
    await b.publish("t2", "tool_call", agent="Scarlett", tool="step3", args={})

    events = b.events("t2", after=seen[1]["eid"])
    try:
        resumed = await _take(events, 3)
    finally:
        await events.aclose()
    # Everything after step1, messages included, not marked as replay.
    assert [e.get("tool") or e["type"] for e in resumed] == ["step2", "message", "step3"]
    assert not any(e.get("replay") for e in resumed)


async def test_log_is_capped_and_expires(hq, monkeypatch):
    monkeypatch.setattr(broker_mod, "EVENTLOG_MAX", 20)
    b = hq.broker
    for i in range(300):
        await b.publish("t3", "tool_call", agent="Scarlett", tool=f"s{i}", args={})
    key = b.eventlog("t3")
    assert await b.r.xlen(key) < 300  # approximate trim keeps it near the cap
    assert 0 < await b.r.ttl(key) <= broker_mod.EVENTLOG_TTL

    events = b.events("t3", replay=5)
    try:
        replayed = await _take(events, 5)
    finally:
        await events.aclose()
    assert [e["tool"] for e in replayed] == [f"s{i}" for i in range(295, 300)]


async def test_websocket_replays_on_open_and_ignores_bad_after(hq):
    from types import SimpleNamespace

    from starlette.websockets import WebSocketDisconnect

    from app.api.threads import thread_events

    await hq.broker.publish("t4", "tool_call", agent="Scarlett", tool="fetch_page", args={})
    sent: list[dict] = []

    class FakeSocket:
        app = SimpleNamespace(state=SimpleNamespace(broker=hq.broker))
        async def accept(self): pass
        async def send_json(self, ev):
            sent.append(ev)
            raise WebSocketDisconnect()  # the page goes away after one event

    # A malformed ?after= is treated as a fresh open, so the backlog is replayed.
    await asyncio.wait_for(thread_events(FakeSocket(), "t4", after="junk"), 5)
    assert sent[0]["type"] == "tool_call" and sent[0]["tool"] == "fetch_page" and sent[0]["replay"]
