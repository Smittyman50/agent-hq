"""Plain-language schedules: phrases -> the editor's fields and cron, their descriptions,
the run times they produce, the errors for what can't be scheduled, and the API."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import schedule_text, scheduler
from app.models import Schedule

NY = "America/New_York"


def _describe(fields: dict) -> tuple[str, str]:
    s = Schedule(name="x", agent_id="a", task="t", **{k: v for k, v in fields.items() if k != "cron"})
    s.cron = scheduler.build_cron(**fields)
    return s.cron, scheduler.describe(s)


@pytest.mark.parametrize("text,frequency,cron,summary", [
    # hourly and intervals
    ("hourly", "hourly", "0 * * * *", "Every hour at :00"),
    ("every hour at :15", "hourly", "15 * * * *", "Every hour at :15"),
    ("Every 4 hours at 15 past", "hourly", "15 */4 * * *", "Every 4 hours at :15"),
    ("every hour on the half hour", "hourly", "30 * * * *", "Every hour at :30"),
    ("every 2 hours", "hourly", "0 */2 * * *", "Every 2 hours at :00"),
    ("every other hour", "hourly", "0 */2 * * *", "Every 2 hours at :00"),
    ("every 120 minutes", "hourly", "0 */2 * * *", "Every 2 hours at :00"),
    ("every 15 minutes", "cron", "*/15 * * * *", "Every 15 minutes"),
    ("every half hour", "cron", "*/30 * * * *", "Every 30 minutes"),
    ("every 30 minutes between 8am and 6pm on weekdays", "cron", "*/30 8-17 * * 1,2,3,4,5",
     "Every 30 minutes from 8:00 AM to 5:30 PM on weekdays"),
    ("every hour from 9 to 5 on weekdays", "cron", "0 9-17 * * 1,2,3,4,5",
     "Every hour from 9:00 AM to 5:00 PM on weekdays"),
    ("every 2 hours on weekends", "cron", "0 */2 * * 0,6", "Every 2 hours at :00 on weekends"),
    # daily
    ("daily", "daily", "0 9 * * *", "Every day at 9:00 AM"),
    ("daily at 7am", "daily", "0 7 * * *", "Every day at 7:00 AM"),
    ("every day at 18:30", "daily", "30 18 * * *", "Every day at 6:30 PM"),
    ("every evening at 6", "daily", "0 18 * * *", "Every day at 6:00 PM"),
    ("every day at noon", "daily", "0 12 * * *", "Every day at 12:00 PM"),
    ("at midnight", "daily", "0 0 * * *", "Every day at 12:00 AM"),
    ("daily at 9am and 5pm", "cron", "0 9,17 * * *", "Every day at 9:00 AM and 5:00 PM"),
    # days of the week
    ("every weekday at 7am", "weekly", "0 7 * * 1,2,3,4,5", "Weekdays at 7:00 AM"),
    ("Weekdays at 7:30 a.m.", "weekly", "30 7 * * 1,2,3,4,5", "Weekdays at 7:30 AM"),
    ("weekends at 10", "weekly", "0 10 * * 0,6", "Weekends at 10:00 AM"),
    ("every monday", "weekly", "0 9 * * 1", "Mondays at 9:00 AM"),
    ("Mondays and Thursdays at 7:30", "weekly", "30 7 * * 1,4", "Mondays and Thursdays at 7:30 AM"),
    ("mon, wed, fri at 9pm", "weekly", "0 21 * * 1,3,5", "Mondays, Wednesdays and Fridays at 9:00 PM"),
    ("mon-fri at 8am", "weekly", "0 8 * * 1,2,3,4,5", "Weekdays at 8:00 AM"),
    ("tuesday through thursday at 8am", "weekly", "0 8 * * 2,3,4", "Tuesdays, Wednesdays and Thursdays at 8:00 AM"),
    ("every tuesday at 7 in the evening", "weekly", "0 19 * * 2", "Tuesdays at 7:00 PM"),
    ("weekly on friday at 5pm", "weekly", "0 17 * * 5", "Fridays at 5:00 PM"),
    ("weekly", "weekly", "0 9 * * 1", "Mondays at 9:00 AM"),
    # monthly
    ("monthly", "monthly", "0 9 1 * *", "Monthly on the 1st at 9:00 AM"),
    ("on the 15th of every month at 9", "monthly", "0 9 15 * *", "Monthly on the 15th at 9:00 AM"),
    ("the 1st and 15th at noon", "cron", "0 12 1,15 * *", "Monthly on the 1st and 15th at 12:00 PM"),
    ("last day of the month at 11pm", "cron", "0 23 L * *", "Last day of the month at 11:00 PM"),
    ("first Monday of the month at 9am", "cron", "0 9 * * 1#1", "First Monday of the month at 9:00 AM"),
    ("the first monday of every month", "cron", "0 9 * * 1#1", "First Monday of the month at 9:00 AM"),
    ("second Tuesday of each month at 10:00", "cron", "0 10 * * 2#2", "Second Tuesday of the month at 10:00 AM"),
    ("last friday of the month at 5pm", "cron", "0 17 * * L5", "Last Friday of the month at 5:00 PM"),
    # cron, as is
    ("*/10 8-18 * * 1-5", "cron", "*/10 8-18 * * 1-5", "Every 10 minutes from 8:00 AM to 6:50 PM on weekdays"),
])
def test_phrases(text, frequency, cron, summary):
    fields = schedule_text.parse(text)
    assert fields["frequency"] == frequency
    assert _describe(fields) == (cron, summary)


def test_a_missing_time_uses_the_one_given():
    fields = schedule_text.parse("every weekday", hour=6, minute=45)
    assert _describe(fields) == ("45 6 * * 1,2,3,4,5", "Weekdays at 6:45 AM")


@pytest.mark.parametrize("text,msg", [
    ("", "describe when"),
    ("every blue moon", "didn't follow 'blue moon'"),
    ("every other day", "can't count days or weeks"),
    ("biweekly on friday", "can't count days or weeks"),
    ("every other Tuesday", "can't count days or weeks"),
    ("every 7 minutes", "divide the hour evenly"),
    ("every 90 minutes", "divide the hour evenly"),
    ("every 30 hours", "1 to 23"),
    ("monthly on the 31st", "31st"),
    ("fifth monday of the month", "fifth"),
    ("at 9:00 and 17:30", "same minutes"),
    ("between 9am and 5pm", "every N minutes"),
    ("every 2 hours at 7am", "give the minute"),
    ("first monday on fridays", "not both"),
    ("at 25:00", "isn't a time"),
])
def test_what_it_cant_schedule_is_explained(text, msg):
    with pytest.raises(scheduler.ScheduleError, match=msg):
        schedule_text.parse(text)


def test_first_monday_and_last_day_run_on_the_right_dates():
    utc = timezone.utc
    after = datetime(2026, 11, 23, 12, 0, tzinfo=utc)
    firsts = scheduler.next_runs("0 9 * * 1#1", NY, after, count=3)
    assert [t.astimezone(scheduler.zone(NY)).date().isoformat() for t in firsts] == \
        ["2026-12-07", "2027-01-04", "2027-02-01"]
    lasts = scheduler.next_runs("0 23 L * *", NY, after, count=3)
    assert [t.astimezone(scheduler.zone(NY)).date().isoformat() for t in lasts] == \
        ["2026-11-30", "2026-12-31", "2027-01-31"]
    fridays = scheduler.next_runs("0 17 * * L5", NY, after, count=2)
    assert [t.astimezone(scheduler.zone(NY)).date().isoformat() for t in fridays] == \
        ["2026-11-27", "2026-12-25"]


async def test_parse_api_previews_five_runs_and_saves(hq):
    r = await hq.http.post("/api/schedules/parse", json={"text": "first Monday of the month at 9am",
                                                         "timezone": NY})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["summary"] == "First Monday of the month at 9:00 AM" and p["cron"] == "0 9 * * 1#1"
    assert len(p["next"]) == 5
    for t in p["next"]:
        local = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(scheduler.zone(NY))
        assert local.weekday() == 0 and local.day <= 7 and (local.hour, local.minute) == (9, 0)

    # The fields go straight into a schedule.
    agent = await hq.agent(name="Scarlett", model="fake:worker")
    body = {"name": "Monthly report", "agent_id": agent["id"], "task": "Write the monthly report.",
            "timezone": NY, **p["fields"]}
    r = await hq.http.post("/api/schedules", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["summary"] == "First Monday of the month at 9:00 AM"
    assert r.json()["next_run_at"] == p["next"][0]

    r = await hq.http.post("/api/schedules/parse", json={"text": "every other week", "timezone": NY})
    assert r.status_code == 422 and "can't count" in r.json()["detail"]
