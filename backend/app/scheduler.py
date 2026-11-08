"""Scheduled agent tasks.

A Schedule gives one agent a task on a cadence (every N hours, daily, weekly, monthly, or
any cron expression), in a chosen time zone. When it's due, the API process queues a turn
for that agent in the schedule's own conversation, exactly like a message you'd type, and
the agent's work (including any delegation) shows up there.

- Due schedules are checked every SCHEDULER_TICK seconds by the API process. A Valkey lock
  per schedule and due time makes each run fire once, even with several API processes.
- If Agent HQ was down when a run was due, it runs once when it comes back (not once per
  missed slot), and the conversation says it ran late.
- With "skip if still running", a run is skipped while the previous one is unfinished.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlmodel import select

from . import db
from .models import AgentDef, Job, JobStatus, Schedule, Thread

log = logging.getLogger(__name__)

SCHEDULER_TICK = 20.0
FREQUENCIES = ("hourly", "daily", "weekly", "monthly", "cron")
DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
MAX_RUNS_KEPT = 20


class ScheduleError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    """UTC with its time zone attached (naive values are taken as UTC)."""
    return dt.replace(tzinfo=timezone.utc) if dt.utcoffset() is None else dt.astimezone(timezone.utc)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ScheduleError(f"unknown time zone '{name}'") from e


# --------------------------------------------------------------------------- cadence

def build_cron(frequency: str, *, minute: int = 0, hour: int = 9, interval_hours: int = 1,
               weekdays: list[int] | None = None, day_of_month: int = 1, cron: str = "") -> str:
    """The cron expression for a friendly cadence (or the custom one, checked)."""
    if frequency not in FREQUENCIES:
        raise ScheduleError(f"frequency must be one of {', '.join(FREQUENCIES)}")
    if frequency == "cron":
        expr = " ".join((cron or "").split())
        if not expr or not croniter.is_valid(expr) or len(expr.split()) != 5:
            raise ScheduleError("that isn't a valid cron expression (5 fields: minute hour day month weekday)")
        return expr
    if not 0 <= minute <= 59:
        raise ScheduleError("minute must be 0-59")
    if frequency == "hourly":
        if not 1 <= interval_hours <= 23:
            raise ScheduleError("hourly interval must be 1-23 hours")
        return f"{minute} {'*' if interval_hours == 1 else f'*/{interval_hours}'} * * *"
    if not 0 <= hour <= 23:
        raise ScheduleError("hour must be 0-23")
    if frequency == "daily":
        return f"{minute} {hour} * * *"
    if frequency == "weekly":
        days = sorted({int(d) for d in (weekdays or [])})
        if not days or any(not 0 <= d <= 6 for d in days):
            raise ScheduleError("choose at least one day of the week")
        return f"{minute} {hour} * * {','.join(map(str, days))}"
    if not 1 <= day_of_month <= 31:
        raise ScheduleError("day of month must be 1-31")
    # Days that some months lack (29-31) would silently skip those months; "L" isn't
    # portable cron, so they're capped at 28 by the editor and refused here.
    if day_of_month > 28:
        raise ScheduleError("day of month must be 1-28 so it happens every month")
    return f"{minute} {hour} {day_of_month} * *"


def _clock(hour: int, minute: int) -> str:
    h = hour % 12 or 12
    return f"{h}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def describe(s: Schedule) -> str:
    """'Every day at 9:00 AM', 'Every 4 hours at :15', 'Mondays and Thursdays at 7:30 AM', ..."""
    if s.frequency == "hourly":
        every = "Every hour" if s.interval_hours == 1 else f"Every {s.interval_hours} hours"
        return f"{every} at :{s.minute:02d}"
    if s.frequency == "daily":
        return f"Every day at {_clock(s.hour, s.minute)}"
    if s.frequency == "weekly":
        days = sorted(set(s.weekdays or []))
        if days == [1, 2, 3, 4, 5]:
            label = "Weekdays"
        elif days == [0, 6]:
            label = "Weekends"
        elif len(days) == 7:
            label = "Every day"
        else:
            names = [DAY_NAMES[d] + "s" for d in days]
            label = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        return f"{label} at {_clock(s.hour, s.minute)}"
    if s.frequency == "monthly":
        return f"Monthly on the {_ordinal(s.day_of_month)} at {_clock(s.hour, s.minute)}"
    return f"Cron: {s.cron}"


def next_runs(cron: str, tz: str, after: datetime | None = None, count: int = 1) -> list[datetime]:
    """The next `count` run times after `after`, in UTC."""
    local = aware(after or utcnow()).astimezone(zone(tz))
    it = croniter(cron, local)
    return [it.get_next(datetime).astimezone(timezone.utc) for _ in range(count)]


def apply(s: Schedule, data: dict[str, Any]) -> Schedule:
    """Set a schedule's fields from editor input, rebuild its cron and next run."""
    for k in ("name", "agent_id", "task", "frequency", "interval_hours", "minute", "hour", "weekdays",
              "day_of_month", "timezone", "enabled", "skip_if_running"):
        if k in data and data[k] is not None:
            setattr(s, k, data[k])
    s.name = (s.name or "").strip()
    if not s.name:
        raise ScheduleError("give the schedule a name")
    if not (s.task or "").strip():
        raise ScheduleError("describe the task the agent should do")
    zone(s.timezone)
    s.cron = build_cron(s.frequency, minute=s.minute, hour=s.hour, interval_hours=s.interval_hours,
                        weekdays=s.weekdays, day_of_month=s.day_of_month, cron=data.get("cron", s.cron))
    s.next_run_at = next_runs(s.cron, s.timezone)[0] if s.enabled else None
    return s


# --------------------------------------------------------------------------- running

def ensure_thread(s: Schedule) -> str:
    """The schedule's conversation (a new one if it has none, or its agent changed)."""
    with db.session() as ses:
        row = ses.get(Schedule, s.id)
        thread = ses.get(Thread, row.thread_id) if row.thread_id else None
        if thread is not None and thread.agent_id == row.agent_id:
            return thread.id
        thread = Thread(agent_id=row.agent_id, title=f"Scheduled: {row.name}")
        ses.add(thread)
        ses.commit()
        row.thread_id = thread.id
        ses.add(row)
        ses.commit()
        return thread.id


def _previous_unfinished(s: Schedule) -> bool:
    last = next((r for r in s.runs or [] if r.get("job_id")), None)
    if not last:
        return False
    with db.session() as ses:
        job = ses.get(Job, last["job_id"])
    return job is not None and job.status in (JobStatus.queued, JobStatus.running)


def _record(schedule_id: str, entry: dict, *, advance_from: datetime | None = None) -> None:
    with db.session() as ses:
        row = ses.get(Schedule, schedule_id)
        row.runs = [entry, *(row.runs or [])][:MAX_RUNS_KEPT]
        if not entry.get("skipped"):
            row.last_run_at = utcnow()
        if advance_from is not None:
            row.next_run_at = next_runs(row.cron, row.timezone, advance_from)[0] if row.enabled else None
        ses.add(row)
        ses.commit()


def _prompt(s: Schedule, due: datetime | None) -> str:
    when = aware(due or utcnow()).astimezone(zone(s.timezone))
    return (f"[Scheduled task \"{s.name}\" ({describe(s)}), run for {when:%A %Y-%m-%d %H:%M} "
            f"{s.timezone}]\n\n{s.task.strip()}\n\n"
            "This was started by a schedule, not typed by Mike just now. Do the task and reply "
            "with the results in this conversation.")


async def fire(rt, s: Schedule, *, trigger: str = "schedule", due: datetime | None = None,
               advance: bool = False) -> dict:
    """Run a schedule now. Returns the run entry recorded on it."""
    now = utcnow()
    with db.session() as ses:
        agent = ses.get(AgentDef, s.agent_id)
    if agent is None or not agent.enabled:
        entry = {"at": now.isoformat(), "trigger": trigger, "skipped": True,
                 "reason": "its agent is missing or disabled"}
        _record(s.id, entry, advance_from=now if advance else None)
        return entry
    if trigger == "schedule" and s.skip_if_running and _previous_unfinished(s):
        entry = {"at": now.isoformat(), "trigger": trigger, "skipped": True,
                 "reason": "the previous run was still going"}
        _record(s.id, entry, advance_from=now if advance else None)
        log.info("schedule %s skipped: previous run unfinished", s.name)
        return entry
    thread_id = ensure_thread(s)
    late = due is not None and (now - aware(due)) > timedelta(minutes=2)
    note = f"Scheduled run: {s.name}"
    if trigger == "manual":
        note = f"Run now: {s.name}"
    elif late:
        note += f" (was due {aware(due).astimezone(zone(s.timezone)):%a %H:%M}; " \
                "Agent HQ was offline then)"
    job = await rt.enqueue_turn(thread_id, _prompt(s, due if trigger == "schedule" else None),
                                from_user=False, event_text=note, kind="schedule",
                                schedule_id=s.id, schedule=s.name, trigger=trigger)
    entry = {"at": now.isoformat(), "job_id": job.id, "trigger": trigger, "thread_id": thread_id}
    if late:
        entry["late"] = True
    _record(s.id, entry, advance_from=now if advance else None)
    return entry


async def run_due(rt, now: datetime | None = None) -> list[str]:
    """Fire every enabled schedule whose time has come. Returns the schedule IDs fired."""
    now = now or utcnow()
    with db.session() as ses:
        due = ses.exec(select(Schedule).where(Schedule.enabled == True,  # noqa: E712
                                              Schedule.next_run_at != None,  # noqa: E711
                                              Schedule.next_run_at <= now)).all()
    fired = []
    for s in due:
        # One run per schedule and due time, however many API processes are checking.
        key = f"ahq:schedule:{s.id}:{aware(s.next_run_at).isoformat()}"
        if not await rt.broker.r.set(key, "1", nx=True, ex=7 * 86400):
            continue
        try:
            await fire(rt, s, trigger="schedule", due=s.next_run_at, advance=True)
            fired.append(s.id)
        except Exception:
            log.exception("schedule %s failed to start", s.name)
            _record(s.id, {"at": utcnow().isoformat(), "trigger": "schedule", "skipped": True,
                           "reason": "it failed to start (see the server log)"}, advance_from=now)
    return fired


async def scheduler_loop(rt, tick: float = SCHEDULER_TICK) -> None:
    log.info("scheduler running (checks every %ss)", tick)
    while True:
        try:
            await run_due(rt)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler check failed")
        await asyncio.sleep(tick)
