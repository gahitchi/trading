"""CrossSectionalAgent: runs one ranking strategy across the whole universe."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from nautilus_trader.common import DataActor, DataActorConfig

from lab.agents.messages import T_SIGNAL, SignalUpdate
from lab.agents.schedule import FUTURE_TOLERANCE, MAX_BAR_AGE_DAYS, market_now, next_at, now_ny, parse_clock
from lab.agents.signal_agent import bar_minute, history_days
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
        weekdays_only: bool = True,
        eval_delay_s: float = 120.0,
        session_close: str = "16:00",
        market_offset_days: float = 0.0,
        max_bar_age_days: float = MAX_BAR_AGE_DAYS,
        bar_spec: str = "1-DAY",
        session=None,
        ib_timestamps: bool = False,
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
        self.weekdays_only = weekdays_only
        self.eval_delay_s = eval_delay_s  # seconds between requesting bars and ranking them
        self.session_close = session_close
        self.market_offset_days = market_offset_days
        self.max_bar_age_days = max_bar_age_days
        self.bar_spec = bar_spec  # the trading speed, as in SignalAgentConfig
        self.session = session  # agents.schedule.Session, or None (daily)
        self.ib_timestamps = ib_timestamps


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
                self.subscribe_bars(bar_type(iid, self.config.bar_spec))

    def on_bar(self, bar):
        self._add(bar)
        if not self.pending and bar.ts_event >= self.config.trade_start_ns:
            self.pending = True
            if self.config.session is not None:
                # Every stock's bar for this interval arrives at the same moment: rank half a second later,
                # before the ExecutionAgent decides (one second after the bars).
                at_ns = self.clock.timestamp_ns() + 500_000_000
                self.clock.set_time_alert_ns(f"xs-{at_ns}", at_ns, callback=self._evaluate)
                return
            # Evaluate once all of today's bars are in: one minute after the close.
            day = datetime.fromtimestamp(bar.ts_event / 1e9, NY)
            at = day.replace(hour=16, minute=1, second=0, microsecond=0)
            self.clock.set_time_alert_ns(f"xs-{at:%Y%m%d}", int(at.timestamp() * 1e9), callback=self._evaluate)

    def on_historical_bars(self, bars):
        if self.config.ib_timestamps:
            from lab.agents.broker_bars import normalize_ib_bar

            bars = [normalize_ib_bar(b, self.config.session) for b in bars]
        now = market_now(self.clock, self.config.market_offset_days)
        close = parse_clock(self.config.session_close)
        for b in sorted(bars, key=lambda b: b.ts_event):
            t = datetime.fromtimestamp(b.ts_event / 1e9, NY)
            if t > now + FUTURE_TOLERANCE:
                continue  # stamped after now: never used
            if self.config.session is not None:
                if t > now - self.config.session.delay():
                    continue  # not yet in the (delayed) data: may still be forming
            elif t.date() == now.date() and now.time() < close:
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
                bar_minute(bar.ts_event, self.config.session),
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
        ts = event.ts_event + int(self.config.market_offset_days * 86_400e9)
        # Only stocks whose latest bar is current: not older than the age limit, and never from the future.
        limit, ahead = self.config.max_bar_age_days * 86_400e9, FUTURE_TOLERANCE.total_seconds() * 1e9
        today = {t for t, b in self.last_bar.items() if -ahead <= ts - b.ts_event <= limit}
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
        days = history_days(self.signal.lookback, self.config.session)
        start = market_now(self.clock, self.config.market_offset_days) - timedelta(days=days)
        for iid in self.config.instruments.values():
            self.request_bars(bar_type(iid, self.config.bar_spec), start=start)

    def _schedule_live(self):
        session = self.config.session
        if session is not None:
            fetch = session.next_fetch(now_ny(self.clock))
            evaluate = fetch + timedelta(seconds=session.eval_s)
        else:
            fetch = next_at(now_ny(self.clock), self.config.signal_time, weekdays_only=self.config.weekdays_only)
            evaluate = fetch + timedelta(seconds=self.config.eval_delay_s)  # after the requested bars arrived
        self.clock.set_time_alert_ns(
            f"xs-fetch-{fetch:%Y%m%d-%H%M%S%f}", int(fetch.timestamp() * 1e9), callback=self._on_fetch_time
        )
        self.clock.set_time_alert_ns(
            f"xs-eval-{evaluate:%Y%m%d-%H%M%S%f}", int(evaluate.timestamp() * 1e9), callback=self._on_live_evaluate
        )

    def _on_fetch_time(self, _event):
        self._request_history()

    def _on_live_evaluate(self, event):
        self._evaluate(event)
        self._schedule_live()
