"""Plain-language schedules: "every weekday at 7am" -> the Scheduler's fields.

A small rule-based parser, not a model: it's instant, works offline, and gives the same
answer every time. It understands the common shapes:

- intervals: hourly, every 2 hours, every 15 minutes, every half hour (with "at :15",
  "15 past", "on the hour"; optionally "between 9am and 5pm" and on chosen days)
- daily: daily at 7am, every day at noon, every evening at 6, at 9am and 5pm
- days of the week: weekdays, weekends, Mondays and Thursdays, mon-fri, Tuesday to Friday
- monthly: on the 15th of every month, monthly, the last day of the month,
  the first Monday of the month, the last Friday of every month
- a cron expression, as is

parse() returns the same fields the editor sends (frequency, interval_hours, minute, hour,
weekdays, day_of_month, cron). Shapes the friendly fields can't hold (first Monday, a time
window, several times a day) come back as a cron expression. Anything it doesn't follow
raises ScheduleError naming the words it didn't understand, rather than guessing.
"""

from __future__ import annotations

import re
from typing import Any

from croniter import croniter

from .scheduler import ScheduleError, _ordinal

EXAMPLES = ("every weekday at 7am, daily at 18:30, every 2 hours, Mondays and Thursdays at 9, "
            "first Monday of the month at 9am, every 15 minutes between 8am and 6pm")

_DAY = (r"(?:sun(?:day)?|mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|"
        r"fri(?:day)?|sat(?:urday)?)s?")
_DAY_INDEX = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_NTH = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
        "fifth": 5, "5th": 5, "last": "L"}
_NUMBER = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
           "eight": 8, "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "other": 2}
_MONTH_TAIL = r"(?:\s+(?:day\s+)?(?:of|in)\s+(?:the|every|each|a)?\s*month)?"
_TIME = r"(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midday|midnight)"
# Words that may be left over once everything meaningful has been taken out.
_FILLER = {"every", "each", "on", "the", "at", "of", "in", "a", "an", "and", "also", "plus", "run",
           "it", "please", "o'clock", "oclock", "clock", "o", "morning", "afternoon", "evening",
           "night", "tonight", "day", "days", "time", "times", "starting", "from"}


def _day(token: str) -> int:
    return _DAY_INDEX[token[:3]]


def _clock(text: str, evening: bool = False) -> tuple[int, int]:
    """'7am', '7:30 pm', '19:00', 'noon', '7' (24-hour unless it's evening) -> (hour, minute)."""
    t = text.strip()
    if t in ("noon", "midday"):
        return 12, 0
    if t == "midnight":
        return 0, 0
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        raise ScheduleError(f"couldn't read the time '{text}'")
    hour, minute, half = int(m[1]), int(m[2] or 0), m[3]
    if minute > 59:
        raise ScheduleError(f"'{text}' isn't a time")
    if half:
        if not 1 <= hour <= 12:
            raise ScheduleError(f"'{text}' isn't a time")
        hour = hour % 12 + (12 if half == "pm" else 0)
    elif hour > 23:
        raise ScheduleError(f"'{text}' isn't a time")
    elif evening and 1 <= hour < 12:
        hour += 12
    return hour, minute


def _number(word: str | None) -> int:
    if not word:
        return 1
    return int(word) if word.isdigit() else _NUMBER[word]


class _Text:
    """The phrase, with each understood part cut out as it's found."""

    def __init__(self, text: str):
        self.s = " " + text + " "

    def take(self, pattern: str) -> list[re.Match]:
        found = list(re.finditer(pattern, self.s))
        for m in reversed(found):
            self.s = self.s[:m.start()] + " " + self.s[m.end():]
        return found

    def has(self, pattern: str) -> bool:
        return re.search(pattern, self.s) is not None

    def leftover(self) -> list[str]:
        words = re.findall(r"[a-z0-9:'#]+", self.s)
        return [w for w in words if w not in _FILLER]


def _normalise(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"\b([ap])\.m\.?", r"\1m", s)
    s = re.sub(r"[.!?;]+$", "", s)
    s = s.replace("&", " and ").replace(",", " , ").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s)


def _as_cron(text: str) -> str | None:
    expr = " ".join(text.split())
    if len(expr.split()) == 5 and re.fullmatch(r"[\d*/,\-#LW? a-z]+", expr, re.I) \
            and any(c.isdigit() or c == "*" for c in expr) and croniter.is_valid(expr):
        return expr
    return None


def parse(text: str, *, hour: int = 9, minute: int = 0) -> dict[str, Any]:
    """The schedule fields for a plain-language cadence. `hour`/`minute` are used when the
    phrase needs a time of day and doesn't give one (the editor passes its current time)."""
    if not (text or "").strip():
        raise ScheduleError("describe when it should run, e.g. " + EXAMPLES)
    raw = " ".join(text.split())
    if (expr := _as_cron(raw)) is not None:
        return _fields("cron", cron=expr)

    t = _Text(_normalise(raw))
    evening = t.has(r"\b(?:afternoon|evening|night|tonight|pm)\b") and not t.has(r"\bam\b")

    if t.has(rf"\b(?:every\s+(?:other|\d+|two|three|four|five|six)\s+(?:days?|weeks?|months?)|"
             rf"every\s+other\s+{_DAY}|bi-?weekly|bi-?monthly|fortnight(?:ly)?)\b"):
        raise ScheduleError("schedules can't count days or weeks between runs; pick days "
                            "instead, e.g. 'Mondays and Thursdays at 9' or 'the 1st and 15th'")

    # A window: "between 9am and 5pm", "from 8 to 6".
    window = None
    for m in t.take(rf"\b(?:between|from)\s+({_TIME})\s*(?:and|to|until|till|through|-)\s*({_TIME})\b"):
        (h1, _), (h2, m2) = _clock(m[1]), _clock(m[2])
        if h2 <= h1 and not re.search(r"am|pm|:|noon|mid", m[2]):
            h2 += 12  # "9 to 5"
        if not 0 <= h1 < h2 <= 23:
            raise ScheduleError("a time window has to start and end on the same day, e.g. 'between 8am and 6pm'")
        window = (h1, h2, m2)

    # Monthly shapes.
    last_day = bool(t.take(r"\blast\s+day" + _MONTH_TAIL + r"\b"))
    nth = None
    for m in t.take(rf"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|last)\s+({_DAY})\b" + _MONTH_TAIL):
        if nth is not None:
            raise ScheduleError("one 'first Monday'-style day per schedule, please")
        nth = (_NTH[m[1]], _day(m[2]))
    if nth and nth[0] == 5:
        raise ScheduleError("not every month has a fifth one of those; try 'the last' instead")
    month_days = [int(m[1]) for m in t.take(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b" + _MONTH_TAIL)]
    month_days += [int(m[1]) for m in t.take(r"\bday\s+(\d{1,2})\b" + _MONTH_TAIL)]
    monthly = bool(t.take(r"\b(?:monthly|every\s+month|each\s+month|a\s+month|month)\b"))
    monthly = monthly or bool(month_days) or last_day or nth is not None
    for d in month_days:
        if not 1 <= d <= 31:
            raise ScheduleError(f"there's no day {d} in a month")
        if d > 28:
            raise ScheduleError(f"not every month has a {_ordinal(d)}; use 'the last day of the month' instead")

    # Intervals.
    interval = None  # ("minutes" | "hours", n)
    for m in t.take(r"\bevery\s+(?:(\d+|a|an|one|two|three|four|five|six|eight|ten|twelve|fifteen|"
                    r"twenty|thirty|other)\s+)?(minutes?|mins?|hours?|hrs?)\b"):
        unit = "minutes" if m[2].startswith("min") else "hours"
        interval = (unit, _number(m[1]))
    if t.take(r"\bevery\s+half\s+(?:an\s+)?hour\b|\bhalf[- ]hourly\b"):
        interval = ("minutes", 30)
    if t.take(r"\bevery\s+quarter\s+(?:of\s+an\s+)?hour\b"):
        interval = ("minutes", 15)
    if t.take(r"\bhourly\b"):
        interval = interval or ("hours", 1)

    # Minutes past the hour (for interval schedules).
    past = None
    for pattern, value in ((r"\bquarter\s+past(?:\s+the\s+hour)?\b", 15),
                           (r"\bhalf\s+past(?:\s+the\s+hour)?\b", 30),
                           (r"\bquarter\s+to(?:\s+the\s+hour)?\b", 45),
                           (r"\bon\s+the\s+half[- ]hour\b", 30),
                           (r"\bon\s+the\s+hour\b", 0),
                           (r"\btop\s+of\s+the\s+hour\b", 0)):
        if t.take(pattern):
            past = value
    for m in t.take(r"\b(\d{1,2})\s+(?:minutes?\s+|mins?\s+)?(?:past|after)(?:\s+the\s+hour)?\b"):
        past = int(m[1])
    for m in t.take(r"(?:\bat\s+)?(?<![\d:]):(\d{2})\b"):
        past = int(m[1])
    if past is not None and not 0 <= past <= 59:
        raise ScheduleError("minutes past the hour must be 0-59")

    # Times of day.
    times: list[tuple[int, int]] = []
    for m in t.take(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b"):
        times.append(_clock(m[1]))
    for m in t.take(r"\b(noon|midday|midnight)\b"):
        times.append(_clock(m[1]))
    for m in t.take(r"\b(\d{1,2}:\d{2})\b"):
        times.append(_clock(m[1], evening))
    for m in t.take(r"\b(?:at|and)\s+(\d{1,2})(?:\s*o'?\s*clock)?\b(?!\s*(?:minutes?|mins?|hours?|past|after))"):
        times.append(_clock(m[1], evening))
    times = sorted(set(times))

    # Days of the week.
    days: set[int] = set()
    if t.take(r"\b(?:week\s?days?|business\s+days?|work\s?days?|working\s+days?)\b"):
        days |= {1, 2, 3, 4, 5}
    if t.take(r"\bweek\s?ends?\b"):
        days |= {0, 6}
    for m in t.take(rf"\b({_DAY})\s*(?:-|to|through|thru|until|till)\s*({_DAY})\b"):
        a, b = _day(m[1]), _day(m[2])
        d = a
        while True:
            days.add(d)
            if d == b:
                break
            d = (d + 1) % 7
    for m in t.take(rf"\b({_DAY})\b"):
        days.add(_day(m[1]))
    weekly = bool(t.take(r"\b(?:weekly|every\s+week|each\s+week|a\s+week|week)\b"))
    daily = bool(t.take(r"\b(?:daily|nightly|every\s+day|each\s+day|every\s+(?:morning|afternoon|evening|night)|"
                        r"each\s+(?:morning|afternoon|evening|night)|a\s+day)\b"))
    if t.take(r"\b(?:once|twice)\b"):
        daily = daily or not (weekly or monthly)

    unknown = [w for w in t.leftover() if w != ","]
    if unknown:
        raise ScheduleError(f"I didn't follow '{' '.join(unknown)}'. Try something like: {EXAMPLES}")

    # ----------------------------------------------------------------- assemble
    dow = ",".join(map(str, sorted(days))) if days and len(days) < 7 else "*"

    if interval:
        unit, n = interval
        if monthly:
            raise ScheduleError("an every-N-minutes or hours schedule can't also be monthly")
        if times:
            raise ScheduleError("for an every-N schedule, give the minute ('at :15', '15 past') "
                                "or a window ('between 8am and 6pm') instead of a time")
        hours = f"{window[0]}-{window[1]}" if window else "*"
        if window and unit == "minutes" and n < 60 and window[2] == 0:
            # "every 30 minutes until 6pm" stops at 5:30, not 6:30.
            hours = f"{window[0]}-{window[1] - 1}" if window[1] - 1 > window[0] else str(window[0])
        if unit == "minutes":
            if n >= 60 and n % 60 == 0:
                unit, n = "hours", n // 60
            elif not 1 <= n < 60 or 60 % n:
                raise ScheduleError("every N minutes needs N to divide the hour evenly "
                                    "(5, 10, 15, 20 or 30); for longer gaps use hours")
            else:
                if past is not None:
                    raise ScheduleError("every N minutes already runs several times an hour; "
                                        "leave out the minute")
                return _fields("cron", cron=f"{'*' if n == 1 else f'*/{n}'} {hours} * * {dow}")
        if not 1 <= n <= 23:
            raise ScheduleError("every N hours needs N from 1 to 23; for once a day say 'daily at …'")
        mm = past if past is not None else 0
        if not window and dow == "*":
            return _fields("hourly", interval_hours=n, minute=mm)
        step = "" if n == 1 else f"/{n}"
        return _fields("cron", cron=f"{mm} {hours if window else '*'}{step} * * {dow}")

    if window:
        raise ScheduleError("a window like 'between 9am and 5pm' goes with 'every N minutes' "
                            "or 'every N hours'")
    if past is not None:
        raise ScheduleError("'15 past' and ':15' are for hourly schedules; for a daily one give "
                            "a time like '7:15am'")
    if not times:
        times = [(hour, minute)]
    minutes = {m for _, m in times}
    if len(minutes) > 1:
        raise ScheduleError("several times in one schedule need the same minutes past the hour "
                            "(9:00 and 17:00 is fine, 9:00 and 17:30 isn't); make two schedules")
    mm = times[0][1]
    hh = ",".join(str(h) for h, _ in times)
    one = len(times) == 1
    h0 = times[0][0]

    if monthly:
        if days:
            raise ScheduleError("pick either days of the week or a day of the month, not both")
        kinds = sum((bool(month_days), last_day, nth is not None))
        if kinds > 1:
            raise ScheduleError("pick one kind of monthly day: a date, the last day, or e.g. the first Monday")
        if nth:
            n, d = nth
            return _fields("cron", cron=f"{mm} {hh} * * {f'L{d}' if n == 'L' else f'{d}#{n}'}")
        if last_day:
            return _fields("cron", cron=f"{mm} {hh} L * *")
        dates = sorted(set(month_days or [1]))
        if one and len(dates) == 1:
            return _fields("monthly", day_of_month=dates[0], hour=h0, minute=mm)
        return _fields("cron", cron=f"{mm} {hh} {','.join(map(str, dates))} * *")

    if weekly and not days:
        days = {1}
        dow = "1"
    if days and len(days) < 7:
        if one:
            return _fields("weekly", weekdays=sorted(days), hour=h0, minute=mm)
        return _fields("cron", cron=f"{mm} {hh} * * {dow}")
    if one:
        return _fields("daily", hour=h0, minute=mm)
    return _fields("cron", cron=f"{mm} {hh} * * *")


def _fields(frequency: str, *, interval_hours: int = 1, minute: int = 0, hour: int = 9,
            weekdays: list[int] | None = None, day_of_month: int = 1, cron: str = "") -> dict[str, Any]:
    return {"frequency": frequency, "interval_hours": interval_hours, "minute": minute, "hour": hour,
            "weekdays": weekdays or [1], "day_of_month": day_of_month, "cron": cron}
