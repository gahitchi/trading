"""Strict UTC trade-to-minute aggregation; no fabricated bars or future knowledge."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from lab.markets.domain import Trade

MINUTE_NS = 60_000_000_000


@dataclass(frozen=True, slots=True)
class MinuteBar:
    start_ns: int
    end_ns: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trades: int
    missing_minutes_before: int = 0


class MinuteBuilder:
    """Ingest monotonic exchange timestamps; emit only after a receive-time watermark.

    A watermark certifies that later data cannot be treated as an on-time trade.
    The caller must enforce vendor sequence/backfill separately. Missing minutes
    remain missing and are reported on the next actual completed bar.
    """

    def __init__(self) -> None:
        self._start: int | None = None
        self._first: Decimal | None = None
        self._high: Decimal | None = None
        self._low: Decimal | None = None
        self._last: Decimal | None = None
        self._volume = Decimal("0")
        self._count = 0
        self._last_ts = -1
        self._watermark = -1
        self._last_end: int | None = None
        self._trade_ids: set[str] = set()

    def push(self, trade: Trade) -> tuple[MinuteBar, ...]:
        if trade.trade_id in self._trade_ids:
            raise ValueError("duplicate trade ID; rejecting rather than double-counting")
        if trade.exchange_ns < self._last_ts:
            raise ValueError("out-of-order trade: replay must explicitly sort/repair source")
        if trade.exchange_ns <= self._watermark:
            raise ValueError("trade arrived for an already finalized watermark")
        start = trade.exchange_ns // MINUTE_NS * MINUTE_NS
        out: tuple[MinuteBar, ...] = ()
        if self._start is not None and start != self._start:
            if start < self._start:
                raise ValueError("cannot regress minute")
            out = self.flush(trade.received_ns)
            if self._start is not None:
                raise ValueError("previous minute not yet final at receive timestamp")
        if self._start is None:
            self._start = start
            self._first = self._high = self._low = trade.price
            self._volume = Decimal("0")
            self._count = 0
        self._high = max(self._high, trade.price)
        self._low = min(self._low, trade.price)
        self._last = trade.price
        self._volume += trade.quantity
        self._count += 1
        self._last_ts = trade.exchange_ns
        self._trade_ids.add(trade.trade_id)
        return out

    def flush(self, watermark_ns: int) -> tuple[MinuteBar, ...]:
        if not isinstance(watermark_ns, int) or watermark_ns < self._watermark:
            raise ValueError("watermark must be monotonic Unix nanoseconds")
        self._watermark = watermark_ns
        if self._start is None or watermark_ns < self._start + MINUTE_NS:
            return ()
        gap = 0 if self._last_end is None else (self._start - self._last_end) // MINUTE_NS
        if gap < 0:
            raise ValueError("overlapping or regressed bar interval")
        bar = MinuteBar(
            start_ns=self._start,
            end_ns=self._start + MINUTE_NS,
            open=self._first,
            high=self._high,
            low=self._low,
            close=self._last,
            volume=self._volume,
            trades=self._count,
            missing_minutes_before=gap,
        )
        self._last_end = bar.end_ns
        self._start = None
        self._first = self._high = self._low = self._last = None
        self._volume = Decimal("0")
        self._count = 0
        return (bar,)
