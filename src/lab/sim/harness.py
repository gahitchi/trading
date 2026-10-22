"""
The live-node harness: the real paper-trading node, the real agents, a simulated broker.

One harness session is one trading day, compressed into seconds on the wall clock:

  T-60s  "the close": the replay day's bar is stamped at this time of day (history is shifted by a
         whole number of days so the first replay day is today and the real calendar gaps remain)
  T      the node starts: connects, reconciles with the broker, agents load history
  T+2s   signal time: agents request the day's bars and publish targets
  T+3.5s cross-sectional strategies rank the universe
  T+5s   rebalance time: the ExecutionAgent reconciles its ledger and sends market-on-open orders
  then   the next session's opening auction fills them, either while the node runs
         (deliver_open) or while it is down (the fills are reported at the next start)

The broker's state lives in a file and outlives the node, so restarts, crashes between
order and fill, rejects and partial fills are tested against the same code path that runs
against IBKR. Nothing in the agents knows it is in a harness: only the clients and the
schedule's clock times differ (paper.build_node(..., clients=..., schedule=...)).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from lab import paper
from lab.agents.schedule import NY
from lab.config import LabConfig
from lab.notify import Notifier
from lab.signals.registry import SignalSpec
from lab.sim.broker import Fill, SimBroker
from lab.sim.clients import ReplayMarket, sim_clients

PAPER_ACCOUNT = "DU1234567"


@dataclass
class SessionResult:
    day: int
    fills: list[Fill]
    ledger: dict
    broker_positions: dict[str, float]
    broker_cash: float
    risk_events: list[dict] = field(default_factory=list)
    error: str | None = None


class LiveHarness:
    def __init__(
        self,
        cfg: LabConfig,
        specs: list[SignalSpec],
        frames: dict[str, pd.DataFrame],
        workdir: Path,
        *,
        first_day: int,
        broker_cash: float | None = None,
        allow_short: bool = False,
        shortable: set[str] | None = None,
    ):
        self.cfg = replace(cfg, paper=replace(cfg.paper, account_id=PAPER_ACCOUNT, port=4002))
        self.specs = specs
        self.tickers = sorted(frames)
        self.frames = {t: frames[t] for t in self.tickers}
        self.workdir = Path(workdir)
        self.market = ReplayMarket(self.frames, first_day)
        c = cfg.costs
        self.broker_path = self.workdir / "broker.json"
        self.broker_kwargs = {
            "account": PAPER_ACCOUNT,
            "cash": broker_cash if broker_cash is not None else cfg.paper.capital,
            "slippage": c.slippage,
            "commission": {
                "per_share": c.commission_per_share,
                "minimum": c.commission_min,
                "max_pct": c.commission_max_pct,
            },
            "allow_short": allow_short,
            "shortable": shortable,
        }
        self.sessions: list[SessionResult] = []

    # ------------------------------------------------------------------ helpers

    def broker(self) -> SimBroker:
        """A fresh handle on the broker's persistent state (as a separate process would see it)."""
        return SimBroker(self.broker_path, **self.broker_kwargs)

    @property
    def runtime(self) -> Path:
        return paper.runtime_dir(self.cfg)

    def ledger(self) -> dict:
        p = self.runtime / "ledger.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def journal(self) -> list[dict]:
        out = []
        for p in sorted(self.runtime.glob("journal-*.jsonl")):
            out += [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
        return out

    def _schedule(self) -> paper.Schedule:
        now = datetime.now(NY)
        if now.hour == 23 and now.minute >= 58:  # keep the compressed day inside one calendar date
            time.sleep((60 - now.second) + 60 * (59 - now.minute) + 2)
            now = datetime.now(NY)
        if now.hour == 0 and now.minute < 2:
            time.sleep(120)
            now = datetime.now(NY)
        close = (now - timedelta(seconds=60)).replace(microsecond=0)
        self.market.close_tod = pd.Timedelta(hours=close.hour, minutes=close.minute, seconds=close.second)

        def at(sec: int) -> str:
            return (now + timedelta(seconds=sec)).strftime("%H:%M:%S")

        self._rebalance_at = now + timedelta(seconds=5)
        return paper.Schedule(
            signal_time=at(2),
            rebalance_time=at(5),
            session_close=close.strftime("%H:%M:%S"),
            weekdays_only=False,
            xs_eval_delay_s=1.5,
        )

    def fill_open_offline(self) -> list[Fill]:
        """The next session's opening auction while no node is running (e.g. after a crash)."""
        fills, _ = self.broker().open_session(self.market.next_opens(), time.time_ns())
        return fills

    # ------------------------------------------------------------------ one trading day

    async def _session(self, *, fill: str, adopt_broker: bool, before_start=None) -> SessionResult:
        broker = self.broker()
        if before_start:
            before_start(broker)
        sink: dict = {}
        schedule = self._schedule()
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
        try:
            # Wait for the rebalance, then a little for orders to reach the broker.
            while datetime.now(NY) < self._rebalance_at + timedelta(seconds=1.5):
                if task.done():
                    break
                await asyncio.sleep(0.2)
            if fill == "running" and not task.done() and "exec" in sink:
                fills = sink["exec"].deliver_open(self.market.next_opens())
                await asyncio.sleep(1.0)  # let the fill events reach the agents and the ledger
        finally:
            if not task.done():
                handle.stop()
            try:
                await asyncio.wait_for(task, 30)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            node.dispose()
        if fill == "offline":
            fills = self.fill_open_offline()
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

    def session(self, *, fill: str = "running", adopt_broker: bool = False, before_start=None) -> SessionResult:
        """Run one trading day. fill: 'running' (open auction while the node runs), 'offline'
        (after it stops, as if it crashed before the open) or 'none' (no auction)."""
        if fill not in ("running", "offline", "none"):
            raise ValueError("fill must be running, offline or none")
        return asyncio.run(self._session(fill=fill, adopt_broker=adopt_broker, before_start=before_start))
