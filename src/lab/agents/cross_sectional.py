"""CrossSectionalAgent: runs one ranking strategy across the whole universe."""

from __future__ import annotations

from collections import deque
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from nautilus_trader.common import DataActor, DataActorConfig

from lab.agents.messages import T_SIGNAL, SignalUpdate
from lab.signals.base import CrossSectionalSignal, Window, clean_weights
from lab.universe import bar_type

NY = ZoneInfo("America/New_York")


def _session_date(ts_ns: int):
    """The New York calendar date of a bar, as numpy datetime64[D]."""
    import numpy as np

    return np.datetime64(datetime.fromtimestamp(ts_ns / 1e9, NY).date(), "D")


class CrossSectionalAgentConfig(DataActorConfig):
    def __init__(
        self,
        *,
        signal: CrossSectionalSignal,
        instruments: dict,
        trade_start_ns: int = 0,
        live: bool = False,
        signal_time: str = "16:10",
        active_windows: dict | None = None,
        **_kwargs,
    ):
        """instruments: ticker -> InstrumentId. active_windows: ticker -> [(from_ns, until_ns)] or None."""
        super().__init__()
        self.signal = signal
        self.instruments = instruments
        self.trade_start_ns = trade_start_ns
        self.live = live
        self.signal_time = signal_time
        self.active_windows = active_windows or {}


class CrossSectionalAgent(DataActor):
    """Publishes, for every stock, the share of this strategy's capital it should hold.

    Sleeves are named "<signal>:<TICKER>"; the AllocatorAgent treats their targets as
    fractions of the strategy's capital (absolute), not per-stock exposures.
    """

    def __init__(self, config: CrossSectionalAgentConfig):
        super().__init__(config)
        self.signal = config.signal
        n = max(1, self.signal.lookback)
        self.rows = {t: deque(maxlen=n) for t in config.instruments}
        self.last_bar = {}
        self.last_ts = dict.fromkeys(config.instruments, -1)
        self.was_active: set[str] = set()
        self.pending = False
        self.errors = 0
        self.evaluations = 0

    # ------------------------------------------------------------------ lifecycle

    def on_start(self):
        if self.config.live:
            self._request_history()
            self._schedule_live()
        else:
            for iid in self.config.instruments.values():
                self.subscribe_bars(bar_type(iid))

    def on_bar(self, bar):
        self._add(bar)
        if not self.pending and bar.ts_event >= self.config.trade_start_ns:
            # Evaluate once all of today's bars are in: one minute after the close.
            self.pending = True
            day = datetime.fromtimestamp(bar.ts_event / 1e9, NY)
            at = day.replace(hour=16, minute=1, second=0, microsecond=0)
            self.clock.set_time_alert_ns(f"xs-{at:%Y%m%d}", int(at.timestamp() * 1e9), callback=self._evaluate)

    def on_historical_bars(self, bars):
        now_ny = datetime.fromtimestamp(self.clock.timestamp_ns() / 1e9, NY)
        for b in sorted(bars, key=lambda b: b.ts_event):
            day = datetime.fromtimestamp(b.ts_event / 1e9, NY).date()
            if day == now_ny.date() and now_ny.time() < time(16, 0):
                continue  # today's bar is still forming
            self._add(b)

    def _add(self, bar):
        t = str(bar.bar_type.instrument_id.symbol)
        if t not in self.rows or bar.ts_event <= self.last_ts[t]:
            return
        self.last_ts[t] = bar.ts_event
        self.last_bar[t] = bar
        self.rows[t].append(
            (
                float(bar.open),
                float(bar.high),
                float(bar.low),
                float(bar.close),
                float(bar.volume),
                _session_date(bar.ts_event),
            )
        )

    # ------------------------------------------------------------------ the daily ranking

    def _is_active(self, t: str, ts: int) -> bool:
        windows = self.config.active_windows.get(t)
        if windows is None:
            return True
        return any(a <= ts and (b is None or ts <= b) for a, b in windows)

    def _evaluate(self, event):
        self.pending = False
        ts = event.ts_event
        today = {t for t, b in self.last_bar.items() if ts - b.ts_event < 4 * 86_400 * 1_000_000_000}
        eligible = {
            t: Window(*zip(*self.rows[t], strict=True))
            for t in today
            if self._is_active(t, ts) and len(self.rows[t]) >= self.signal.lookback
        }
        try:
            weights = clean_weights(self.signal.weights(eligible)) if eligible else {}
        except Exception as e:  # a buggy strategy must never take the system down
            self.errors += 1
            self.log.error(f"{self.signal.name}: weights() raised {type(e).__name__}: {e}; going flat")
            weights = {}
        weights = {t: w for t, w in weights.items() if t in eligible}
        self.evaluations += 1
        for t in sorted(today):
            bar = self.last_bar[t]
            active = self._is_active(t, ts)
            if not active and t not in self.was_active:
                continue
            if active:
                self.was_active.add(t)
            else:
                self.was_active.discard(t)
            self.publish_message(
                T_SIGNAL,
                SignalUpdate(
                    f"{self.signal.name}:{t}",
                    t,
                    weights.get(t, 0.0) if active else 0.0,
                    float(bar.close),
                    bar.ts_event,
                    active,
                ),
            )

    # ------------------------------------------------------------------ paper mode

    def _request_history(self, *_):
        days = int(self.signal.lookback * 1.6) + 15
        start = datetime.fromtimestamp(self.clock.timestamp_ns() / 1e9, NY) - timedelta(days=days)
        for iid in self.config.instruments.values():
            self.request_bars(bar_type(iid), start=start)

    def _schedule_live(self):
        hh, mm = map(int, self.config.signal_time.split(":"))
        now = datetime.fromtimestamp(self.clock.timestamp_ns() / 1e9, NY)
        fetch = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if fetch <= now:
            fetch += timedelta(days=1)
        while fetch.weekday() >= 5:
            fetch += timedelta(days=1)
        self.clock.set_time_alert_ns(
            f"xs-fetch-{fetch:%Y%m%d}", int(fetch.timestamp() * 1e9), callback=self._on_fetch_time
        )
        evaluate = fetch + timedelta(minutes=2)  # after the requested bars have arrived
        self.clock.set_time_alert_ns(
            f"xs-eval-{evaluate:%Y%m%d}", int(evaluate.timestamp() * 1e9), callback=self._on_live_evaluate
        )

    def _on_fetch_time(self, _event):
        self._request_history()

    def _on_live_evaluate(self, event):
        self._evaluate(event)
        self._schedule_live()
