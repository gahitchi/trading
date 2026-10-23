"""When the live agents act: session close, signal time and rebalance time, in New York time.

Production uses the defaults (16:00 close, weekdays only). The live-node harness passes a
compressed schedule (times a few seconds apart, every day a session) so that a whole
trading day runs through the real node in seconds, on the real wall clock.
"""

from __future__ import annotations

from dataclasses import dataclass
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


# ----------------------------------------------------------------------------- quick speeds


@dataclass(frozen=True)
class Session:
    """The regular session and bar grid of a quick trading speed, on the market clock.

    Production: 09:30-16:00 New York, a market minute is 60 s, bars every `interval_min`. The live-node
    harness compresses a session (a shorter session, a fraction of a second per market minute) so that a
    whole intraday day runs through the real node in seconds; every agent asks this object, never the clock
    times directly, so the same code runs in both.

    delay_min: how late the broker's bars are (IBKR paper without a subscription: 15). The live cycle per
    bar: the bar closes at E, is in the delayed data at E + delay, is requested `fetch_lag_s` later, ranked
    `eval_s` after the request and decided on `decide_s` after it.
    """

    interval_min: int
    delay_min: float = 0.0
    open: time = time(9, 30)
    minutes: float = 390.0
    seconds_per_minute: float = 60.0
    fetch_lag_s: float = 20.0
    eval_s: float = 10.0
    decide_s: float = 25.0
    weekdays_only: bool = True

    def _m(self, minutes: float) -> timedelta:
        return timedelta(seconds=minutes * self.seconds_per_minute)

    def open_at(self, day: datetime) -> datetime:
        d = day.astimezone(NY)
        return d.replace(hour=self.open.hour, minute=self.open.minute, second=self.open.second, microsecond=0)

    def close_at(self, day: datetime) -> datetime:
        return self.open_at(day) + self._m(self.minutes)

    def minute_of(self, t: datetime) -> float:
        """Market minutes since the session's open (390 at a production close)."""
        return (t - self.open_at(t)).total_seconds() / self.seconds_per_minute

    def bar_ends(self, day: datetime) -> list[datetime]:
        """Close times of the session's bars (the last bar is cut at the close)."""
        o, n, out = self.open_at(day), self.interval_min, []
        m = n
        while m < self.minutes:
            out.append(o + self._m(m))
            m += n
        out.append(o + self._m(self.minutes))
        return out

    def phase(self, now: datetime) -> str:
        """'open', 'closing' (at most one interval left: orders still fill on the session's last bar) or
        'closed'."""
        if not is_session_day(now, self.weekdays_only):
            return "closed"
        o, c = self.open_at(now), self.close_at(now)
        if now < o or now >= c:
            return "closed"
        return "closing" if c - now <= self._m(self.interval_min) else "open"

    def delay(self) -> timedelta:
        return self._m(self.delay_min)

    def fetch_times(self, day: datetime) -> list[datetime]:
        lag = timedelta(seconds=self.fetch_lag_s)
        return [e + self.delay() + lag for e in self.bar_ends(day)]

    def next_fetch(self, after: datetime) -> datetime:
        """The first live fetch strictly after `after` (on this or a later session day)."""
        day = after.astimezone(NY)
        for _ in range(10):
            if is_session_day(day, self.weekdays_only):
                for t in self.fetch_times(day):
                    if t > after:
                        return t
            day = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        raise RuntimeError("no session day within 10 days")

    def max_bar_age(self) -> timedelta:
        """The latest bar may be at most one interval plus the data delay old, plus the request lag."""
        lag = timedelta(seconds=self.fetch_lag_s + self.decide_s)
        return self._m(self.interval_min + 2) + self.delay() + lag
