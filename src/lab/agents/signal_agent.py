"""SignalAgent: runs one Signal on one stock and publishes its target exposure."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from nautilus_trader.common import DataActor, DataActorConfig

from lab.agents.messages import T_SIGNAL, SignalUpdate
from lab.agents.schedule import next_at, now_ny, parse_clock
from lab.signals.base import Signal, Window, clamp_target
from lab.universe import bar_type

NY = ZoneInfo("America/New_York")


def _session_date(ts_ns: int):
    """The New York calendar date of a bar, as numpy datetime64[D]."""
    import numpy as np

    return np.datetime64(datetime.fromtimestamp(ts_ns / 1e9, NY).date(), "D")


class SignalAgentConfig(DataActorConfig):
    def __init__(
        self,
        *,
        signal: Signal,
        sleeve: str,
        ticker: str,
        instrument_id,
        trade_start_ns: int = 0,
        live: bool = False,
        signal_time: str = "16:10",
        active_windows: list | None = None,
        session_close: str = "16:00",
        weekdays_only: bool = True,
        **_kwargs,
    ):
        """signal_time / session_close: New York clock times, HH:MM or HH:MM:SS (see agents/schedule.py)."""
        super().__init__()
        self.signal = signal
        self.sleeve = sleeve
        self.ticker = ticker
        self.instrument_id = instrument_id
        self.trade_start_ns = trade_start_ns
        self.live = live
        self.signal_time = signal_time
        self.session_close = session_close
        self.weekdays_only = weekdays_only
        # [(from_ns, until_ns or None), ...]: when this stock may be held. None = always.
        self.active_windows = active_windows


class SignalAgent(DataActor):
    """Backtest: fed by the replayed daily bars.
    Paper: requests completed daily bars from the broker once per day after the close
    (plus history at startup to warm up), so it never acts on a partial bar."""

    def __init__(self, config: SignalAgentConfig):
        super().__init__(config)
        self.signal = config.signal
        self.bar_type = bar_type(config.instrument_id)
        self.rows = deque(maxlen=max(self.signal.lookback, 1))
        self.last_ts = -1
        self.errors = 0
        self.published = 0
        self.was_active = False

    # ------------------------------------------------------------------ lifecycle

    def on_start(self):
        if self.config.live:
            self._request_history()
            self._schedule_daily_request()
        else:
            self.subscribe_bars(self.bar_type)

    def on_bar(self, bar):
        if bar.ts_event > self.last_ts:
            self._process(bar, publish=True)

    def on_historical_bars(self, bars):
        now = now_ny(self.clock)
        close = parse_clock(self.config.session_close)
        fresh = []
        for b in sorted(bars, key=lambda b: b.ts_event):
            if b.ts_event <= self.last_ts:
                continue
            day = datetime.fromtimestamp(b.ts_event / 1e9, NY).date()
            if day == now.date() and now.time() < close:
                continue  # today's bar is still forming
            fresh.append(b)
        for i, b in enumerate(fresh):
            self._process(b, publish=(i == len(fresh) - 1))

    # ------------------------------------------------------------------ core

    def _process(self, bar, publish: bool):
        self.last_ts = bar.ts_event
        self.rows.append(
            (
                float(bar.open),
                float(bar.high),
                float(bar.low),
                float(bar.close),
                float(bar.volume),
                _session_date(bar.ts_event),
            )
        )
        if len(self.rows) < self.signal.lookback:
            return
        cols = list(zip(*self.rows, strict=True))
        try:
            raw = self.signal.target(Window(*cols))
        except Exception as e:  # a buggy signal must never take the system down
            self.errors += 1
            self.log.error(f"{self.config.sleeve}: signal raised {type(e).__name__}: {e}; going flat")
            raw = 0.0
        t = clamp_target(raw)
        self.signal.last = t
        if not publish or bar.ts_event < self.config.trade_start_ns:
            return
        if self._is_active(bar.ts_event):
            self.was_active = True
            self._publish(t, bar, active=True)
        elif self.was_active:
            # Left the universe (delisting or index exit): one last message to exit.
            self.was_active = False
            self._publish(0.0, bar, active=False)

    def _is_active(self, ts: int) -> bool:
        windows = self.config.active_windows
        if windows is None:
            return True
        return any(a <= ts and (b is None or ts <= b) for a, b in windows)

    def _publish(self, target: float, bar, active: bool):
        self.published += 1
        self.publish_message(
            T_SIGNAL,
            SignalUpdate(self.config.sleeve, self.config.ticker, target, float(bar.close), bar.ts_event, active),
        )

    # ------------------------------------------------------------------ paper mode

    def _request_history(self, *_):
        days = int(self.signal.lookback * 1.6) + 15  # calendar days covering `lookback` sessions
        start = now_ny(self.clock) - timedelta(days=days)
        self.request_bars(self.bar_type, start=start)

    def _schedule_daily_request(self):
        nxt = next_at(now_ny(self.clock), self.config.signal_time, weekdays_only=self.config.weekdays_only)
        self.clock.set_time_alert_ns(
            f"signal-{self.config.sleeve}-{nxt:%Y%m%d-%H%M%S}",
            int(nxt.timestamp() * 1e9),
            callback=self._on_signal_time,
        )

    def _on_signal_time(self, _event):
        self._request_history()
        self._schedule_daily_request()
