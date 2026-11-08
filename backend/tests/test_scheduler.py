"""Scheduled agent tasks: cadences and time zones (including a DST change), the API, firing
due schedules into their own conversation, exactly-once firing, catching up after downtime,
skipping while a run is still going, run-now, and disabled agents."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pydantic_ai.messages import ModelResponse, TextPart

from app import db, scheduler
from app.models import Role, Schedule

from .conftest import SCRIPTS, _last_prompt

NY = "America/New_York"


def did(messages, info):
    return ModelResponse(parts=[TextPart("Did it: " + _last_prompt(messages).split("\n\n")[1])])


SCRIPTS["did"] = did


# --------------------------------------------------------------------------- cadence

@pytest.mark.parametrize("freq,kw,cron,summary", [
    ("hourly", {"minute": 15}, "15 * * * *", "Every hour at :15"),
    ("hourly", {"minute": 0, "interval_hours": 4}, "0 */4 * * *", "Every 4 hours at :00"),
    ("daily", {"hour": 9, "minute": 0}, "0 9 * * *", "Every day at 9:00 AM"),
    ("daily", {"hour": 18, "minute": 30}, "30 18 * * *", "Every day at 6:30 PM"),
    ("weekly", {"hour": 7, "minute": 30, "weekdays": [4, 1]}, "30 7 * * 1,4", "Mondays and Thursdays at 7:30 AM"),
    ("weekly", {"hour": 8, "minute": 0, "weekdays": [1, 2, 3, 4, 5]}, "0 8 * * 1,2,3,4,5", "Weekdays at 8:00 AM"),
    ("monthly", {"hour": 0, "minute": 5, "day_of_month": 1}, "5 0 1 * *", "Monthly on the 1st at 12:05 AM"),
    ("monthly", {"hour": 12, "minute": 0, "day_of_month": 22}, "0 12 22 * *", "Monthly on the 22nd at 12:00 PM"),
    ("cron", {"cron": " */15  9-17 * *  1-5 "}, "*/15 9-17 * * 1-5", "Cron: */15 9-17 * * 1-5"),
])
def test_cadences(freq, kw, cron, summary):
    s = Schedule(name="x", agent_id="a", task="t", frequency=freq,
                 **{k: v for k, v in kw.items() if k != "cron"})
    s.cron = scheduler.build_cron(freq, **kw)
    assert s.cron == cron and scheduler.describe(s) == summary


@pytest.mark.parametrize("freq,kw,msg", [
    ("cron", {"cron": "every tuesday"}, "valid cron"),
    ("weekly", {"weekdays": []}, "at least one day"),
    ("monthly", {"day_of_month": 31}, "1-28"),
    ("hourly", {"interval_hours": 24}, "1-23"),
    ("daily", {"hour": 24}, "hour"),
    ("yearly", {}, "frequency"),
])
def test_bad_cadences_are_explained(freq, kw, msg):
    with pytest.raises(scheduler.ScheduleError, match=msg):
        scheduler.build_cron(freq, **kw)


def test_times_follow_the_schedules_time_zone_across_dst():
    # 9:00 AM in New York is 14:00 UTC in winter (EST) and 13:00 UTC in summer (EDT).
    utc = timezone.utc
    before = datetime(2026, 3, 6, 20, 0, tzinfo=utc)  # Fri evening; clocks spring forward Sun Mar 8
    runs = scheduler.next_runs("0 9 * * *", NY, before, count=3)
    assert runs == [datetime(2026, 3, 7, 14, 0, tzinfo=utc), datetime(2026, 3, 8, 13, 0, tzinfo=utc),
                    datetime(2026, 3, 9, 13, 0, tzinfo=utc)]
    with pytest.raises(scheduler.ScheduleError, match="time zone"):
        scheduler.next_runs("0 9 * * *", "Mars/Olympus_Mons")


# --------------------------------------------------------------------------- API

async def _schedule(hq, **kw) -> dict:
    if "agent_id" not in kw:
        kw["agent_id"] = (await hq.agent(name="Scarlett", model="fake:did"))["id"]
    body = {"name": "Proxmox releases", "task": "Check the Proxmox VE release feed for new versions.",
            "frequency": "daily", "hour": 8, "minute": 0, "timezone": NY, **kw}
    r = await hq.http.post("/api/schedules", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _set_next(schedule_id: str, when: datetime) -> None:
    with db.session() as s:
        row = s.get(Schedule, schedule_id)
        row.next_run_at = when
        s.add(row)
        s.commit()


def _row(schedule_id: str) -> Schedule:
    with db.session() as s:
        return s.get(Schedule, schedule_id)


async def test_create_preview_and_validation(hq):
    s = await _schedule(hq)
    assert s["summary"] == "Every day at 8:00 AM" and s["cron"] == "0 8 * * *" and s["agent_name"] == "Scarlett"
    next_utc = datetime.fromisoformat(s["next_run_at"].replace("Z", "+00:00"))
    assert next_utc > scheduler.utcnow() and next_utc.minute == 0 and next_utc.hour in (12, 13)  # 8 AM NY
    assert s["runs"] == [] and s["last_status"] is None

    r = await hq.http.post("/api/schedules/preview", json={"frequency": "weekly", "weekdays": [1, 4], "hour": 7,
                                                          "minute": 30, "timezone": NY})
    p = r.json()
    assert p["summary"] == "Mondays and Thursdays at 7:30 AM" and len(p["next"]) == 3

    for bad, msg in [({"frequency": "cron", "cron": "nope"}, "cron"), ({"timezone": "Nowhere/Land"}, "time zone"),
                     ({"task": "  "}, "task"), ({"agent_id": "missing"}, "agent")]:
        r = await hq.http.post("/api/schedules", json={"name": "x", "task": "t", "agent_id": s["agent_id"], **bad})
        assert r.status_code == 422 and msg in r.json()["detail"], r.text

    # Disabling clears the next run; editing the cadence recomputes it.
    r = await hq.http.put(f"/api/schedules/{s['id']}", json={**s, "enabled": False})
    assert r.json()["next_run_at"] is None
    r = await hq.http.put(f"/api/schedules/{s['id']}", json={**s, "enabled": True, "frequency": "hourly",
                                                              "minute": 45})
    assert r.json()["summary"] == "Every hour at :45" and r.json()["next_run_at"].endswith(":45:00Z")


async def test_a_due_schedule_runs_once_in_its_own_conversation(hq):
    await hq.start_worker()
    s = await _schedule(hq)
    due = scheduler.utcnow() - timedelta(seconds=30)
    _set_next(s["id"], due)
    # Two API processes checking at the same moment still start one run.
    fired = await asyncio.gather(scheduler.run_due(hq.runtime), scheduler.run_due(hq.runtime))
    assert sorted(len(f) for f in fired) == [0, 1]
    row = _row(s["id"])
    assert row.next_run_at > scheduler.utcnow()  # moved on to the next 8 AM
    [reply] = await hq.wait_agent_replies(row.thread_id, 1)
    assert reply == "Did it: Check the Proxmox VE release feed for new versions."
    msgs = hq.messages(row.thread_id)
    assert msgs[0].role == Role.event and msgs[0].content == "Scheduled run: Proxmox releases"
    assert msgs[0].meta["kind"] == "schedule"
    # The conversation is named for the schedule, and the API reports the run as done.
    threads = (await hq.http.get("/api/threads")).json()
    assert any(t["id"] == row.thread_id and t["title"] == "Scheduled: Proxmox releases" for t in threads)
    listed = (await hq.http.get("/api/schedules")).json()[0]
    assert listed["last_status"] == "done" and listed["runs"][0]["trigger"] == "schedule"
    # Nothing fires again until the next due time.
    assert await scheduler.run_due(hq.runtime) == []


async def test_after_downtime_it_catches_up_once(hq):
    await hq.start_worker()
    s = await _schedule(hq, frequency="hourly", minute=0)
    _set_next(s["id"], scheduler.utcnow() - timedelta(hours=5))  # five missed hourly slots
    assert len(await scheduler.run_due(hq.runtime)) == 1
    assert await scheduler.run_due(hq.runtime) == []
    row = _row(s["id"])
    await hq.wait_agent_replies(row.thread_id, 1)
    note = hq.messages(row.thread_id)[0].content
    assert note.startswith("Scheduled run: Proxmox releases (was due ") and "offline" in note
    assert row.runs[0]["late"] is True and row.next_run_at > scheduler.utcnow()


async def test_skips_while_the_previous_run_is_still_going(hq):
    s = await _schedule(hq)  # no worker yet: the first run stays queued
    _set_next(s["id"], scheduler.utcnow() - timedelta(seconds=5))
    await scheduler.run_due(hq.runtime)
    _set_next(s["id"], scheduler.utcnow() - timedelta(seconds=1))
    await scheduler.run_due(hq.runtime)
    runs = (await hq.http.get("/api/schedules")).json()[0]["runs"]
    assert [r["status"] for r in runs] == ["skipped", "queued"]
    assert runs[0]["reason"] == "the previous run was still going"
    assert len([j for j in hq.jobs()]) == 1


async def test_run_now_and_a_disabled_agent(hq):
    await hq.start_worker()
    s = await _schedule(hq)
    before = _row(s["id"]).next_run_at
    r = await hq.http.post(f"/api/schedules/{s['id']}/run")
    assert r.status_code == 200 and r.json()["runs"][0]["trigger"] == "manual"
    row = _row(s["id"])
    assert row.next_run_at == before  # run-now doesn't move the schedule
    await hq.wait_agent_replies(row.thread_id, 1)
    assert hq.messages(row.thread_id)[0].content == "Run now: Proxmox releases"

    await hq.http.delete(f"/api/agents/{s['agent_id']}")  # agents are disabled, not deleted
    r = await hq.http.post(f"/api/schedules/{s['id']}/run")
    assert r.status_code == 409 and "disabled" in r.json()["detail"]
    # Deleting the schedule leaves its conversation and results in place.
    assert (await hq.http.delete(f"/api/schedules/{s['id']}")).json() == {"ok": True}
    assert hq.messages(row.thread_id)
