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
import re
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
        return f"{_days_label(s.weekdays or [])} at {_clock(s.hour, s.minute)}"
    if s.frequency == "monthly":
        return f"Monthly on the {_ordinal(s.day_of_month)} at {_clock(s.hour, s.minute)}"
    return describe_cron(s.cron) or f"Cron: {s.cron}"


def _days_label(weekdays: list[int]) -> str:
    days = sorted(set(weekdays))
    if days == [1, 2, 3, 4, 5]:
        return "Weekdays"
    if days == [0, 6]:
        return "Weekends"
    if len(days) == 7:
        return "Every day"
    names = [DAY_NAMES[d] + "s" for d in days]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _ints(field: str, lo: int, hi: int) -> list[int] | None:
    """'1,3-5' -> [1, 3, 4, 5]; None if it isn't a plain list of numbers and ranges."""
    out: set[int] = set()
    for part in field.split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            return None
        a, b = int(m[1]), int(m[2] or m[1])
        if not lo <= a <= b <= hi:
            return None
        out.update(range(a, b + 1))
    return sorted(out)


_NTH_NAMES = {1: "First", 2: "Second", 3: "Third", 4: "Fourth", 5: "Fifth"}


def describe_cron(expr: str) -> str | None:
    """Plain English for the cron shapes plain-language schedules produce ('First Monday of
    the month at 9:00 AM', 'Every 15 minutes from 8:00 AM to 5:45 PM on weekdays'), or None
    for anything else."""
    parts = (expr or "").split()
    if len(parts) != 5 or parts[3] != "*":
        return None
    mi, ho, dom, _, dow = parts
    days = None
    if dow not in ("*", "?"):
        days = _ints(dow.replace("7", "0"), 0, 6)
        if days is None and not re.fullmatch(r"\d#[1-5]|L\d", dow):
            return None
    on_days = ""
    if days and len(days) < 7:
        label = _days_label(days)
        on_days = " on " + (label.lower() if label in ("Weekdays", "Weekends") else label)

    # Several times an hour or every N hours, maybe inside a window of hours.
    if dom == "*" and days is not None or dom == "*" and dow in ("*", "?"):
        sub_hourly = mi == "*" or re.fullmatch(r"\*/\d+", mi) is not None
        if sub_hourly and ho.isdigit():
            ho = f"{ho}-{ho}"  # every N minutes within one hour
        m = re.fullmatch(r"(\*|\d+-\d+)(?:/(\d+))?", ho)
        if m and (sub_hourly or mi.isdigit()):
            step = int(m[2] or 1)
            first, last = (0, 23) if m[1] == "*" else map(int, m[1].split("-"))
            last = first + (last - first) // step * step
            window = "" if m[1] == "*" else None
            if mi == "*" or mi.startswith("*/"):
                if step != 1:
                    return None
                n = 1 if mi == "*" else int(mi[2:])
                if not 1 <= n < 60 or 60 % n:
                    return None
                every = "Every minute" if n == 1 else f"Every {n} minutes"
                if window is None:
                    window = f" from {_clock(first, 0)} to {_clock(last, 60 - n)}"
                return every + window + on_days
            minute = int(mi)
            every = "Every hour" if step == 1 else f"Every {step} hours"
            if window is None:
                return f"{every} from {_clock(first, minute)} to {_clock(last, minute)}{on_days}"
            return f"{every} at :{minute:02d}{on_days}"

    # Once or a few times on the days it runs.
    if not mi.isdigit():
        return None
    hours = _ints(ho, 0, 23)
    if hours is None:
        return None
    at = "at " + _join([_clock(h, int(mi)) for h in hours])
    if dom == "*":
        if m := re.fullmatch(r"(\d)#([1-5])", dow):
            return f"{_NTH_NAMES[int(m[2])]} {DAY_NAMES[int(m[1]) % 7]} of the month {at}"
        if m := re.fullmatch(r"L(\d)", dow):
            return f"Last {DAY_NAMES[int(m[1]) % 7]} of the month {at}"
        return f"{_days_label(days or list(range(7)))} {at}"
    if dow not in ("*", "?"):
        return None
    if dom == "L":
        return f"Last day of the month {at}"
    dates = _ints(dom, 1, 31)
    if not dates:
        return None
    return f"Monthly on the {_join([_ordinal(d) for d in dates])} {at}"


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
        if thread is not None and thread.agent_id == row.agent_id and thread.deleted_at is None:
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
                                from_user=False, agent_id=s.agent_id, event_text=note, kind="schedule",
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
            from . import subagents

            subagents.sweep()  # retire expired sub-agents, lapse old requests and grants
            from . import approvals

            approvals.sweep()  # tool calls nobody decided in time
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler check failed")
        await asyncio.sleep(tick)
