"""Five-field cron expressions: `minute hour day-of-month month day-of-week`.

Supports `*`, lists, ranges, steps (`*/15`, `9-17/2`), month and weekday names,
weekday 7 as Sunday, and the @hourly/@daily/@weekly/@monthly/@yearly aliases.
Semantics follow Vixie cron: when both day-of-month and day-of-week are
restricted, a day matches if *either* does ("0 9 1 * mon" = the 1st, and every
Monday).

Times are wall-clock times in a time zone. `next_after` skips wall times that
do not exist (the hour lost when clocks go forward) and fires once, at the
first occurrence, for wall times that happen twice (the hour repeated when
clocks go back).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
SEARCH_YEARS = 5


class CronError(ValueError):
    pass


def _value(token: str, lo: int, hi: int, names: list[str] | None, offset: int) -> int:
    t = token.lower()
    if names and t in names:
        return names.index(t) + offset
    try:
        v = int(t)
    except ValueError:
        raise CronError(f"{token!r} is not a number{' or name' if names else ''}") from None
    if not lo <= v <= hi:
        raise CronError(f"{v} is out of range {lo}-{hi}")
    return v


def _field(spec: str, lo: int, hi: int, names: list[str] | None = None, offset: int = 0) -> frozenset[int]:
    values: set[int] = set()
    for part in spec.split(","):
        if not part:
            raise CronError(f"empty list item in {spec!r}")
        rng, _, step_s = part.partition("/")
        step = 1
        if step_s:
            if not step_s.isdigit() or int(step_s) == 0:
                raise CronError(f"bad step in {part!r}")
            step = int(step_s)
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, _, b = rng.partition("-")
            start, end = _value(a, lo, hi, names, offset), _value(b, lo, hi, names, offset)
            if start > end:
                raise CronError(f"range {part!r} runs backwards")
        else:
            start = _value(rng, lo, hi, names, offset)
            end = hi if step_s else start  # "5/15" means 5, 20, 35, 50
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    dom_restricted: bool
    dow_restricted: bool

    @classmethod
    def parse(cls, expr: str) -> "Cron":
        text = ALIASES.get(expr.strip().lower(), expr.strip())
        parts = text.split()
        if len(parts) != 5:
            raise CronError(f"{expr!r}: expected 5 fields (minute hour day month weekday), got {len(parts)}")
        minute, hour, dom, month, dow = parts
        weekdays = frozenset(d % 7 for d in _field(dow, 0, 7, DAYS))
        return cls(
            expr=expr.strip(),
            minutes=_field(minute, 0, 59),
            hours=_field(hour, 0, 23),
            days=_field(dom, 1, 31),
            months=_field(month, 1, 12, MONTHS, offset=1),
            weekdays=weekdays,
            dom_restricted=not dom.startswith("*"),
            dow_restricted=not dow.startswith("*"),
        )

    def day_matches(self, day: datetime) -> bool:
        dom_ok = day.day in self.days
        dow_ok = (day.isoweekday() % 7) in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def next_after(self, ts: float, tz: tzinfo = timezone.utc) -> float:
        """The first matching time strictly after `ts` (a Unix timestamp)."""
        wall = datetime.fromtimestamp(ts, tz).replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
        limit = wall + timedelta(days=366 * SEARCH_YEARS)
        while wall < limit:
            if wall.month not in self.months:
                wall = (wall.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self.day_matches(wall):
                wall = wall.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if wall.hour not in self.hours:
                wall = wall.replace(minute=0) + timedelta(hours=1)
                continue
            if wall.minute not in self.minutes:
                wall += timedelta(minutes=1)
                continue
            candidate = _resolve(wall, tz)
            if candidate is not None and candidate > ts:
                return candidate
            wall += timedelta(minutes=1)
        raise CronError(f"{self.expr!r} never fires (searched {SEARCH_YEARS} years)")

    def times_between(self, after: float, upto: float, tz: tzinfo = timezone.utc, limit: int = 10_000) -> list[float]:
        """Matching times in (after, upto], oldest first, at most `limit` of them (the most recent kept)."""
        out: list[float] = []
        t = after
        while True:
            t = self.next_after(t, tz)
            if t > upto:
                return out
            out.append(t)
            if len(out) > limit:
                out.pop(0)


def _resolve(wall: datetime, tz: tzinfo) -> float | None:
    """Timestamp of a naive wall time in `tz`; None if that wall time does not exist there."""
    first = wall.replace(tzinfo=tz, fold=0)
    back = first.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
    if back != wall:
        return None  # skipped by a forward clock change
    return first.timestamp()


def describe(expr: str) -> str:
    """A short human description for the common shapes; the expression itself otherwise."""
    text = ALIASES.get(expr.strip().lower(), expr.strip())
    parts = text.split()
    if len(parts) != 5:
        return expr
    minute, hour, dom, month, dow = parts
    if minute.isdigit() and hour.isdigit() and dom == "*" and month == "*":
        at = f"{int(hour):02d}:{int(minute):02d}"
        if dow == "*":
            return f"every day at {at}"
        if dow in ("1-5", "mon-fri"):
            return f"weekdays at {at}"
        if dow.isdigit():
            return f"{DAY_NAMES[int(dow) % 7]}s at {at}"
        return f"{dow} at {at}"
    if minute.isdigit() and hour == "*" and dom == "*" and month == "*" and dow == "*":
        return f"every hour at :{int(minute):02d}"
    if minute.startswith("*/") and hour == "*" and dom == "*" and month == "*" and dow == "*":
        return f"every {minute[2:]} minutes"
    return expr
