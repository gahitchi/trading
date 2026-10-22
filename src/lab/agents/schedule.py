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
