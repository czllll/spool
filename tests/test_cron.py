"""Cron parsing and next-time computation, including DOM/DOW and daylight-saving edges."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from spool.cron import Cron, CronError, describe

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def ts(*args, tz=UTC) -> float:
    return datetime(*args, tzinfo=tz).timestamp()


def at(t: float, tz=UTC) -> datetime:
    return datetime.fromtimestamp(t, tz)


@pytest.mark.parametrize("expr", ["61 * * * *", "* * *", "*/0 * * * *", "5-1 * * * *", "* * * foo *", "1,,2 * * * *"])
def test_bad_expressions_are_rejected(expr):
    with pytest.raises(CronError):
        Cron.parse(expr)


def test_basic_schedules():
    assert at(Cron.parse("0 9 * * *").next_after(ts(2026, 10, 11, 8, 59, 30))) == datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
    assert at(Cron.parse("*/15 * * * *").next_after(ts(2026, 10, 11, 10, 7))) == datetime(2026, 10, 11, 10, 15, tzinfo=UTC)
    # strictly after: a time exactly on the schedule moves to the next one
    assert at(Cron.parse("0 9 * * *").next_after(ts(2026, 10, 11, 9, 0))) == datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    # names, ranges, steps, weekday 7 = Sunday
    weekdays = Cron.parse("30 8 * jan-dec mon-fri")
    assert at(weekdays.next_after(ts(2026, 10, 10, 12, 0))).weekday() == 0  # Sat noon -> Monday
    assert Cron.parse("0 0 * * 7").weekdays == Cron.parse("0 0 * * sun").weekdays == frozenset({0})
    assert Cron.parse("5/20 * * * *").minutes == frozenset({5, 25, 45})
    assert Cron.parse("@hourly").minutes == frozenset({0})


def test_day_of_month_or_day_of_week_when_both_are_restricted():
    cron = Cron.parse("0 9 1 * mon")  # the 1st of the month, and every Monday
    first = cron.next_after(ts(2026, 10, 1, 10, 0))  # Thu Oct 1, after 09:00
    assert at(first) == datetime(2026, 10, 5, 9, 0, tzinfo=UTC)  # the next Monday
    # with only one restricted, the other is ignored
    assert at(Cron.parse("0 9 1 * *").next_after(ts(2026, 10, 1, 10, 0))) == datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def test_impossible_schedule_is_reported():
    with pytest.raises(CronError, match="never fires"):
        Cron.parse("0 0 30 2 *").next_after(ts(2026, 1, 1))


def test_wall_time_skipped_by_spring_forward_does_not_fire():
    cron = Cron.parse("30 2 * * *")
    first = cron.next_after(ts(2026, 3, 7, 3, 0, tz=NY), NY)
    assert at(first, NY).replace(tzinfo=None) == datetime(2026, 3, 9, 2, 30)  # 2026-03-08 02:30 does not exist


def test_repeated_wall_time_fires_once():
    cron = Cron.parse("30 1 * * *")
    first = cron.next_after(ts(2026, 10, 31, 12, 0, tz=NY), NY)
    second = cron.next_after(first, NY)
    assert at(first, UTC) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)   # 01:30 EDT, the first occurrence
    assert at(second, UTC) == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)  # not 01:30 EST an hour later


def test_times_between_keeps_the_most_recent():
    cron = Cron.parse("0 * * * *")
    times = cron.times_between(ts(2026, 10, 11, 0, 0), ts(2026, 10, 11, 5, 30))
    assert [at(t).hour for t in times] == [1, 2, 3, 4, 5]
    assert [at(t).hour for t in cron.times_between(ts(2026, 10, 11, 0, 0), ts(2026, 10, 11, 5, 30), limit=2)] == [4, 5]


def test_describe():
    assert describe("0 9 * * *") == "every day at 09:00"
    assert describe("30 8 * * 1-5") == "weekdays at 08:30"
    assert describe("*/10 * * * *") == "every 10 minutes"
    assert describe("0 9 1 * mon") == "0 9 1 * mon"
