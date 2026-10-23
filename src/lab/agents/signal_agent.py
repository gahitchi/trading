"""SignalAgent: runs one Signal on one stock and publishes its target exposure."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from nautilus_trader.common import DataActor, DataActorConfig

from lab.agents.messages import T_RISK, T_SIGNAL, RiskEvent, SignalUpdate
from lab.agents.schedule import (
    FUTURE_TOLERANCE,
    MAX_BAR_AGE_DAYS,
    Session,
    bar_freshness,
    market_now,
    next_at,
    now_ny,
    parse_clock,
)
from lab.signals.base import SESSION_MINUTES, Signal, Window, clamp_target
from lab.universe import bar_type

NY = ZoneInfo("America/New_York")


def _session_date(ts_ns: int):
    """The New York calendar date of a bar, as numpy datetime64[D]."""
    import numpy as np

    return np.datetime64(datetime.fromtimestamp(ts_ns / 1e9, NY).date(), "D")


def bar_minute(ts_ns: int, session: Session | None) -> float:
    """Window.minute: market minutes since the open at the bar's close (bars are stamped at their close).
    Daily: 390."""
    if session is None:
        return SESSION_MINUTES
    return session.minute_of(datetime.fromtimestamp(ts_ns / 1e9, NY))


def history_days(lookback: int, session: Session | None) -> int:
    """Calendar days of history that cover `lookback` bars (weekends and a holiday included)."""
    if session is None:
        return int(lookback * 1.6) + 15
    per_session = len(session.bar_ends(datetime.now(NY)))
    sessions = -(-max(lookback, 1) // per_session)
    return int(sessions * 1.6) + 6


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
        market_offset_days: float = 0.0,
        max_bar_age_days: float = MAX_BAR_AGE_DAYS,
        bar_spec: str = "1-DAY",
        session: Session | None = None,
        ib_timestamps: bool = False,
        **_kwargs,
    ):
        """signal_time / session_close: New York clock times, HH:MM or HH:MM:SS (see agents/schedule.py).
        bar_spec / session: the trading speed (lab.intraday); session None = daily bars. A quick speed's
        session also says how late the broker's bars are and when each bar is requested (live)."""
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
        self.market_offset_days = market_offset_days  # harness only (agents/schedule.market_now)
        self.max_bar_age_days = max_bar_age_days
        self.bar_spec = bar_spec
        self.session = session
        self.ib_timestamps = ib_timestamps
        # [(from_ns, until_ns or None), ...]: when this stock may be held. None = always.
        self.active_windows = active_windows


class SignalAgent(DataActor):
    """Backtest: fed by the replayed daily bars.
    Paper: requests completed daily bars from the broker once per day after the close
    (plus history at startup to warm up), so it never acts on a partial bar."""

    def __init__(self, config: SignalAgentConfig):
        super().__init__(config)
        self.signal = config.signal
        self.bar_type = bar_type(config.instrument_id, config.bar_spec)
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
        if self.config.ib_timestamps:
            from lab.agents.broker_bars import normalize_ib_bar

            bars = [normalize_ib_bar(b, self.config.session) for b in bars]
        now = market_now(self.clock, self.config.market_offset_days)
        close = parse_clock(self.config.session_close)
        fresh, future = [], 0
        for b in sorted(bars, key=lambda b: b.ts_event):
            if b.ts_event <= self.last_ts:
                continue
            t = datetime.fromtimestamp(b.ts_event / 1e9, NY)
            if t > now + FUTURE_TOLERANCE:
                future += 1  # never act on (or even learn from) a bar stamped after now
                continue
            if self.config.session is not None:
                if t > now - self.config.session.delay():
                    continue  # its close is not yet in the (delayed) data: the bar may still be forming
            elif t.date() == now.date() and now.time() < close:
                continue  # today's bar is still forming
            fresh.append(b)
        if future:
            self.errors += 1
            self._event("future_bar", f"{self.config.ticker}: {future} bar(s) dated after now ignored")
        stale = bar_freshness(fresh[-1].ts_event, now, self.config.max_bar_age_days) if fresh else None
        if stale:
            self._event("stale_data", f"{self.config.ticker}: {stale}; history kept, no target published")
        for i, b in enumerate(fresh):
            self._process(b, publish=(i == len(fresh) - 1 and stale is None))

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
                bar_minute(bar.ts_event, self.config.session),
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
        days = history_days(self.signal.lookback, self.config.session)
        start = market_now(self.clock, self.config.market_offset_days) - timedelta(days=days)
        self.request_bars(self.bar_type, start=start)

    def _event(self, kind: str, detail: str):
        self.log.warning(detail)
        self.publish_message(T_RISK, RiskEvent(self.clock.timestamp_ns(), kind, detail))

    def _schedule_daily_request(self):
        if self.config.session is not None:
            nxt = self.config.session.next_fetch(now_ny(self.clock))  # quick speed: after every bar
        else:
            nxt = next_at(now_ny(self.clock), self.config.signal_time, weekdays_only=self.config.weekdays_only)
        self.clock.set_time_alert_ns(
            f"signal-{self.config.sleeve}-{nxt:%Y%m%d-%H%M%S%f}",
            int(nxt.timestamp() * 1e9),
            callback=self._on_signal_time,
        )

    def _on_signal_time(self, _event):
        self._request_history()
        self._schedule_daily_request()
