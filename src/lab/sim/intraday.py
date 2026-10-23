"""
The live-node harness at a quick speed: one recorded intraday session through the real node in seconds.

The replay session is mapped onto a compressed session that opens a few seconds from now
(agents.schedule.Session with a fraction of a second per market minute). Earlier sessions keep their
real calendar distance and are stamped at the same compressed times of day, so the agents' warm-up,
session dates and time of day (Window.minute) are what they would be in production.

The market serves bars the way a delayed feed does: a bar is in the data only `delay` after its close.
Trade prints are the bars' opening and closing prices, delivered to the simulated broker at every bar's
start and end while the node runs: they fill working market orders and crossing limit orders. DAY orders
expire at the close.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timedelta

import pandas as pd
from nautilus_trader.model import Bar, Price, Quantity

from lab import paper
from lab.agents.schedule import NY, Session
from lab.config import LabConfig
from lab.notify import Notifier
from lab.sim.broker import Fill
from lab.sim.clients import sim_clients
from lab.sim.harness import LiveHarness, SessionResult


def _ns(t: datetime) -> int:
    return int(t.timestamp() * 1e9)


class IntradayReplay:
    """Recorded intraday bars (lab.intraday frames: bar starts, New York) replayed on a compressed session."""

    def __init__(self, frames: dict[str, pd.DataFrame], day: int, interval_min: int):
        self.frames = frames
        self.day = day
        self.interval = interval_min
        days = None
        for f in frames.values():
            d = set(pd.DatetimeIndex(f.index).normalize())
            days = d if days is None else days & d
        self.sessions = sorted(days or [])
        self.session: Session | None = None  # set per harness session

    def replay_date(self) -> pd.Timestamp:
        return self.sessions[self.day]

    def _wall(self, start: pd.Timestamp, *, end: bool) -> datetime:
        """Wall-clock time of a recorded bar's start (or end) on the compressed timeline."""
        s = self.session
        day = start.normalize()
        minute = (start - day - pd.Timedelta(hours=9, minutes=30)).total_seconds() / 60
        if end:
            minute = min(minute + self.interval, 390.0)
        today_open = s.open_at(datetime.now(NY))
        shift = (self.replay_date() - day).days
        return today_open - timedelta(days=shift) + timedelta(seconds=minute * s.seconds_per_minute)

    def bars(self, ticker: str, bt, start_ns: int | None) -> list[Bar]:
        """Every bar up to the replay session that a delayed feed has by now (its close + delay <= now)."""
        now = datetime.now(NY)
        df = self.frames[ticker]
        df = df[df.index.normalize() <= self.replay_date()]
        out = []
        for t, row in df.iterrows():
            close_at = self._wall(t, end=True)
            if close_at + self.session.delay() > now:
                continue
            ts = _ns(close_at)
            if start_ns is not None and ts < start_ns:
                continue
            o, h, lo, c = (round(float(row[k]), 2) for k in ("open", "high", "low", "close"))
            out.append(
                Bar(
                    bt,
                    Price(o, 2),
                    Price(max(h, o, c), 2),
                    Price(min(lo, o, c), 2),
                    Price(c, 2),
                    Quantity(max(1, int(row["volume"])), 0),
                    ts,
                    ts,
                )
            )
        return out

    def print_times(self) -> list[tuple[datetime, dict[str, float]]]:
        """(wall time, prices) of the replay session's trade prints: every bar's open at its start and its
        close just before its end, so an order decided at any moment of the session meets a real price."""
        day = self.replay_date()
        starts = sorted({t for f in self.frames.values() for t in f.index if t.normalize() == day})
        out = []
        for t in starts:
            for col, at in (("open", self._wall(t, end=False)), ("close", self._wall(t, end=True))):
                prices = {k: float(round(f.loc[t, col], 2)) for k, f in self.frames.items() if t in f.index}
                out.append((at - timedelta(seconds=0.02) if col == "close" else at, prices))
        return out

    def next_opens(self) -> dict[str, float]:
        return {}

    def day_offset(self) -> float:
        return 0.0  # the replay session is today


class IntradayHarness(LiveHarness):
    """LiveHarness at a quick speed. `session()` runs one whole compressed intraday session."""

    def __init__(
        self,
        cfg: LabConfig,
        specs,
        frames: dict[str, pd.DataFrame],
        workdir,
        *,
        first_day: int,
        seconds_per_minute: float = 0.04,
        **kw,
    ):
        super().__init__(cfg, specs, frames, workdir, first_day=0, **kw)
        from lab import intraday

        self.speed = intraday.speed(cfg.trading.speed)
        self.market = IntradayReplay(self.frames, first_day, self.speed.minutes)
        self.spm = seconds_per_minute

    def _intraday_schedule(self) -> paper.Schedule:
        now = datetime.now(NY)
        length = timedelta(seconds=390 * self.spm + 20)
        if (now + length).date() != now.date():  # keep the compressed session inside one calendar date
            time.sleep((datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), NY) - now).seconds + 2)
            now = datetime.now(NY)
        opens = (now + timedelta(seconds=4)).replace(microsecond=0)
        sess = Session(
            self.speed.minutes,
            delay_min=self.cfg.trading.data_delay_min,
            open=opens.time(),
            seconds_per_minute=self.spm,
            fetch_lag_s=0.1,
            eval_s=0.05,
            decide_s=0.2,
            weekdays_only=False,
        )
        self.market.session = sess
        return paper.Schedule(
            signal_time="16:10", rebalance_time="16:15", weekdays_only=False, market_offset_days=0.0, session=sess
        )

    async def _session(self, *, fill: str, adopt_broker: bool, before_start=None) -> SessionResult:
        broker = self.broker()
        if before_start:
            before_start(broker)
        sink: dict = {}
        schedule = self._intraday_schedule()
        sess = schedule.session
        node = paper.build_node(
            self.cfg,
            self.specs,
            self.tickers,
            adopt_broker=adopt_broker,
            notifier=Notifier(token="", chat_id=""),
            clients=sim_clients(self.market, broker, sink),
            schedule=schedule,
        )
        handle = node.handle()
        task = asyncio.ensure_future(node.run_async())
        fills: list[Fill] = []
        error = None
        close = sess.close_at(datetime.now(NY))
        try:
            for at, prices in self.market.print_times():
                while datetime.now(NY) < at + timedelta(seconds=0.01):
                    if task.done():
                        break
                    await asyncio.sleep(0.005)
                if task.done():
                    break
                if fill == "running" and "exec" in sink:
                    fills += sink["exec"].deliver_open(prices)
            while datetime.now(NY) < close + timedelta(seconds=0.5) and not task.done():
                await asyncio.sleep(0.05)
            expired = broker.expire_day(time.time_ns())
            if "exec" in sink and not task.done():
                client = sink["exec"]
                for rec in expired:
                    client._report_canceled(rec)
            await asyncio.sleep(0.5)
        finally:
            if not task.done():
                handle.stop()
            try:
                await asyncio.wait_for(task, 30)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            node.dispose()
        b = self.broker()
        result = SessionResult(
            day=self.market.day,
            fills=fills,
            ledger=self.ledger(),
            broker_positions=b.positions(),
            broker_cash=b.cash,
            risk_events=[e for e in self.journal() if e.get("type") == "RiskEvent"],
            error=error,
        )
        self.sessions.append(result)
        self.market.day += 1
        return result


def harness_config(cfg: LabConfig, speed: str = "15m", **trading) -> LabConfig:
    return replace(cfg, trading=replace(cfg.trading, speed=speed, **trading))
