"""What time it is, for agents.

A model has no clock: left alone it assumes "now" is roughly when its training data ends,
and quietly turns "tomorrow" into a date months or years off. So every agent's instructions
end with the current date and time in your time zone (AHQ_TIMEZONE, see config.py).

It goes at the end of the instructions, not the start: the line changes every minute, and
providers that cache prompts reuse everything before the first difference.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import settings

log = logging.getLogger("agent_hq.clock")
_warned: set[str] = set()


def zone(name: str | None = None) -> tzinfo:
    """Your time zone; UTC (with one warning) if the name is not a known zone."""
    name = (name if name is not None else settings.timezone).strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name not in _warned:
            _warned.add(name)
            log.warning("unknown time zone %r (AHQ_TIMEZONE); agents get UTC", name)
        return timezone.utc


def now(tz: tzinfo | None = None) -> datetime:
    return datetime.now(tz or zone())


def describe(at: datetime) -> str:
    """e.g. 'Friday, November 13, 2026, 11:24 AM EST'."""
    hour = at.hour % 12 or 12
    abbr = at.tzname() or ""
    return (f"{at:%A, %B} {at.day}, {at.year}, {hour}:{at:%M} {'AM' if at.hour < 12 else 'PM'}"
            + (f" {abbr}" if abbr else "")).strip()


def instructions(at: datetime | None = None) -> str:
    """The block that goes at the end of every agent's instructions."""
    at = at or now()
    key = getattr(at.tzinfo, "key", None) or at.tzname() or "UTC"
    return (f"## Current date and time\n"
            f"It is now {describe(at)} ({key}); today's date is {at:%Y-%m-%d}. This is the "
            "real current time, later than your training data: use it for every relative date "
            "(today, tomorrow, this weekend, last week) and never assume a date from memory. "
            "When you hand work to another agent or write a date down, give the actual date.")
