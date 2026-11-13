"""Agents know today's date, and Stop ends a conversation's work (turn and delegations)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic_ai import ModelResponse, TextPart

from app import clock
from app.config import reload_settings
from app.models import JobKind, JobStatus, Role

from .conftest import SCRIPTS, _instructions, boss, worker_fn

# --------------------------------------------------------------------------- the date

SEEN: dict[str, str] = {}


def _seeing(name, inner):
    def fn(messages, info):
        SEEN[name] = _instructions(messages)
        return inner(messages, info)
    return fn


SCRIPTS["dated-boss"] = _seeing("boss", boss(False))
SCRIPTS["dated-worker"] = _seeing("worker", worker_fn)


def test_clock_describes_the_moment_in_words():
    at = datetime(2026, 11, 13, 11, 24, tzinfo=ZoneInfo("America/New_York"))
    assert clock.describe(at) == "Friday, November 13, 2026, 11:24 AM EST"
    assert clock.describe(at.replace(hour=0, minute=5)) == "Friday, November 13, 2026, 12:05 AM EST"
    text = clock.instructions(at)
    assert "today's date is 2026-11-13" in text and "(America/New_York)" in text


def test_unknown_time_zone_falls_back_to_utc():
    assert clock.zone("Mars/Olympus_Mons").utcoffset(None).total_seconds() == 0


async def test_every_agent_is_told_the_current_date(hq, monkeypatch):
    monkeypatch.setenv("AHQ_TIMEZONE", "America/New_York")
    reload_settings()
    SEEN.clear()
    await hq.start_worker()
    w = await hq.agent(name="Scarlett", description="Does research", model="fake:dated-worker")
    b = await hq.agent(name="Duke", description="Lead", model="fake:dated-boss", delegates=[w["id"]])
    t = await hq.thread(b["id"])
    await hq.say(t["id"], "what's on this weekend?")
    await hq.wait_agent_replies(t["id"], 1)
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    # The agent you talk to, and the one it delegates to, both get it, as the last block
    # (it changes every minute, so it must not break prompt caching of what comes before).
    for who in ("boss", "worker"):
        assert f"today's date is {today}" in SEEN[who], who
        assert SEEN[who].rstrip().endswith("give the actual date."), who
        assert "(America/New_York)" in SEEN[who]

# --------------------------------------------------------------------------- Stop

STARTED = {"calls": 0}


async def hangs_first(messages, info):
    """A delegate stuck on its first job (a long web search); later jobs finish."""
    STARTED["calls"] += 1
    if STARTED["calls"] == 1:
        await asyncio.sleep(3600)
    return worker_fn(messages, info)


SCRIPTS["hangs-first"] = hangs_first
SCRIPTS["chatty"] = lambda m, i: ModelResponse(parts=[TextPart("hello again")])


async def _wait(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


async def test_stop_ends_the_turn_and_its_delegated_task(hq, monkeypatch):
    monkeypatch.setenv("AHQ_STOP_POLL", "0.1")
    reload_settings()
    STARTED["calls"] = 0
    worker = await hq.start_worker()
    w = await hq.agent(name="Scarlett", description="Does research", model="fake:hangs-first")
    b = await hq.agent(name="Duke", description="Lead", model="fake:boss", delegates=[w["id"]])
    t = await hq.thread(b["id"])
    await hq.say(t["id"], "find things to do this weekend")
    await _wait(lambda: STARTED["calls"] == 1)  # Scarlett is stuck; Duke is waiting on her

    r = await hq.http.post(f"/api/threads/{t['id']}/stop")
    assert r.status_code == 200 and r.json()["stopped"] == 2
    await _wait(lambda: all(j.status == JobStatus.failed for j in hq.jobs()))
    turn, task = hq.jobs()
    assert (turn.kind, task.kind) == (JobKind.turn, JobKind.task)
    assert turn.error == task.error == "Stopped by you."
    assert task.attempts == 1 and turn.attempts == 1  # acked, not left for another worker

    msgs = hq.messages(t["id"])
    assert not [m for m in msgs if m.role == Role.agent]  # no reply, no report turn
    deleg = next(m for m in msgs if (m.meta or {}).get("kind") == "delegation_result")
    assert deleg.meta["status"] == "stopped"
    assert [m.content for m in msgs if (m.meta or {}).get("kind") == "stopped"] == ["Stopped by you."]

    # The worker is free and the conversation isn't locked: the next message runs.
    await hq.say(t["id"], "ok, try again")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Report: Worker finished"]
    await _wait(lambda: not worker._jobs)  # it finishes the new job and holds nothing else
    # Nothing comes back from the dead after the worker's claim window either.
    await asyncio.sleep(0.5)
    assert [j.attempts for j in hq.jobs()][:2] == [1, 1]


async def test_stop_before_a_worker_picks_it_up(hq):
    b = await hq.agent(name="Duke", description="Lead", model="fake:chatty")
    t = await hq.thread(b["id"])
    await hq.say(t["id"], "hello")
    r = await hq.http.post(f"/api/threads/{t['id']}/stop")
    assert r.json()["stopped"] == 1
    (job,) = hq.jobs()
    assert job.status == JobStatus.failed and job.attempts == 0
    await hq.start_worker()  # gets the queued entry, sees it was stopped, never runs it
    await asyncio.sleep(1)
    (job,) = hq.jobs()
    assert job.attempts == 0 and job.status == JobStatus.failed
    assert not [m for m in hq.messages(t["id"]) if m.role == Role.agent]


async def test_stop_with_nothing_running_is_harmless(hq):
    b = await hq.agent(name="Duke", description="Lead", model="fake:chatty")
    t = await hq.thread(b["id"])
    r = await hq.http.post(f"/api/threads/{t['id']}/stop")
    assert r.status_code == 200 and r.json()["stopped"] == 0
    assert hq.messages(t["id"]) == []
    assert (await hq.http.post("/api/threads/nope/stop")).status_code == 404
