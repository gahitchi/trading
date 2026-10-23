"""Normalize the pinned Nautilus IB adapter's timestamps at the live boundary.

2.0.0rc7 (55c0e8205314), crates/adapters/interactive_brokers/src/data/convert.rs:
convert_bar uses bar_close_from_open. Daily IB dates become 23:59:59.999999999 UTC;
intraday bars become open + interval, including the shortened last hourly bar.
The lab uses New York session closes. Simulated clients already provide that convention.
"""

from datetime import UTC, datetime

from nautilus_trader.model import Bar

from lab.agents.schedule import NY


def normalize_ib_bar(bar, session=None):
    if session is None:
        day = datetime.fromtimestamp(bar.ts_event // 1_000_000_000, UTC).date()
        close = datetime(day.year, day.month, day.day, 16, tzinfo=NY)
    else:
        raw = datetime.fromtimestamp(bar.ts_event / 1e9, NY)
        close = min(raw, session.close_at(raw))
    ts = int(close.timestamp() * 1e9)
    return Bar(bar.bar_type, bar.open, bar.high, bar.low, bar.close, bar.volume, ts, max(ts, bar.ts_init))
