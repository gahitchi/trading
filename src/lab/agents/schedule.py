"""When the live agents act: session close, signal time and rebalance time, in New York time.

Production uses the defaults (16:00 close, weekdays only). The live-node harness passes a
compressed schedule (times a few seconds apart, every day a session) so that a whole
trading day runs through the real node in seconds, on the real wall clock.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def parse_clock(value: str) -> time:
    """'HH:MM' or 'HH:MM:SS' (24 h) -> time. Raises ValueError on anything else."""
    parts = str(value).split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise ValueError(f"time {value!r} must be HH:MM or HH:MM:SS")
    hh, mm, ss = (int(p) for p in (*parts, "0")[:3])
    return time(hh, mm, ss)


def is_session_day(day: datetime, weekdays_only: bool) -> bool:
    return not weekdays_only or day.weekday() < 5


def next_at(after: datetime, at: str | time, *, weekdays_only: bool = True) -> datetime:
    """The first moment strictly after `after` (tz-aware, New York) at clock time `at`, on a session day."""
    t = at if isinstance(at, time) else parse_clock(at)
    nxt = after.astimezone(NY).replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
    if nxt <= after:
        nxt += timedelta(days=1)
    while not is_session_day(nxt, weekdays_only):
        nxt += timedelta(days=1)
    return nxt


def now_ny(clock) -> datetime:
    return datetime.fromtimestamp(clock.timestamp_ns() / 1e9, NY)


# A live daily bar older than this many calendar days is not actionable: a weekend plus a holiday Monday is 4.
MAX_BAR_AGE_DAYS = 4.0
FUTURE_TOLERANCE = timedelta(minutes=1)


def market_now(clock, offset_days: float = 0.0) -> datetime:
    """ "Now" for judging whether market data is current. Production: the wall clock. The live-node harness
    replays history on shifted dates and passes the shift, so freshness is checked against the replayed day
    instead of trusting future-dated bars (G007-F2). Timers always use the real clock."""
    return now_ny(clock) + timedelta(days=offset_days)


def bar_freshness(ts_ns: int, now: datetime, max_age_days: float = MAX_BAR_AGE_DAYS) -> str | None:
    """None if a bar stamped ts_ns is usable as the latest data at `now`; else why not."""
    t = datetime.fromtimestamp(ts_ns / 1e9, NY)
    if t > now + FUTURE_TOLERANCE:
        return f"bar dated {t:%Y-%m-%d %H:%M} is in the future (now {now:%Y-%m-%d %H:%M})"
    age = (now - t).total_seconds() / 86_400
    if age > max_age_days:
        return f"latest bar {t:%Y-%m-%d} is {age:.1f} days old (limit {max_age_days:g})"
    return None
