"""SmokeTest: verifies a live deployment end to end without trading."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nautilus_trader.model import OrderSide, TimeInForce
from nautilus_trader.trading import Strategy, StrategyConfig

from lab.safety import is_paper_account
from lab.universe import bar_type


class SmokeTestConfig(StrategyConfig):
    def __init__(
        self,
        *,
        instruments: dict,
        venue,
        paper_account: str,
        report_path: str,
        wait_s: int = 45,
        test_order: bool = False,
        **_kwargs,
    ):
        super().__init__()
        self.instruments = instruments
        self.venue = venue
        self.paper_account = paper_account
        self.report_path = report_path
        self.wait_s = wait_s
        self.test_order = test_order


class SmokeTest(Strategy):
    """Checks, in order:
    1. the broker reports exactly the configured paper account
    2. every instrument loaded from IB
    3. historical daily bars arrive for every instrument
    4. (optional) a 1-share limit buy at half the last close is accepted by IB and then
       cancelled. It cannot fill, so nothing is traded.
    Writes a JSON report and shuts the node down.
    """

    def __init__(self, config: SmokeTestConfig):
        super().__init__(config)
        self.bars: dict[str, int] = {}
        self.last_close: dict[str, float] = {}
        self.last_bar: dict[str, str] = {}
        self.checks: dict[str, dict] = {}
        self.order_events: list[str] = []
        self.test_order_id = None
        self.done = False

    def on_start(self):
        start = datetime.now(UTC) - timedelta(days=14)
        for t, iid in self.config.instruments.items():
            self.bars[t] = 0
            self.request_bars(bar_type(iid), start=start)
        self._after(self.config.wait_s, self._evaluate)
        self._after(self.config.wait_s + 90, self._timeout)

    def _after(self, seconds: float, cb):
        self.clock.set_time_alert_ns(
            f"smoke-{cb.__name__}", self.clock.timestamp_ns() + int(seconds * 1e9), callback=cb
        )

    def on_historical_bars(self, bars):
        for b in bars:
            t = str(b.bar_type.instrument_id.symbol)
            self.bars[t] = self.bars.get(t, 0) + 1
            self.last_close[t] = float(b.close)
            # The bar's own timestamp, so the report shows which session the broker's "last bar" is.
            self.last_bar[t] = datetime.fromtimestamp(b.ts_event / 1e9, UTC).isoformat(timespec="minutes")

    def _check(self, name: str, ok: bool, detail: str):
        self.checks[name] = {"ok": bool(ok), "detail": detail}

    def _evaluate(self, _event):
        aid = self.cache.account_id(self.config.venue)
        account = str(aid) if aid is not None else None
        paper_ok = (
            account is not None and is_paper_account(account) and account.split("-", 1)[-1] == self.config.paper_account
        )
        self._check("paper account", paper_ok, f"broker reports {account}, expected {self.config.paper_account}")
        missing = [t for t, iid in self.config.instruments.items() if self.cache.instrument(iid) is None]
        self._check(
            "instruments", not missing, f"missing {missing}" if missing else f"{len(self.config.instruments)} loaded"
        )
        nobars = [t for t, n in self.bars.items() if n == 0]
        self._check(
            "historical bars",
            not nobars,
            f"none for {nobars}"
            if nobars
            else ", ".join(
                f"{t}: {n} bars, last close {self.last_close.get(t)} at {self.last_bar.get(t)} UTC"
                for t, n in self.bars.items()
            ),
        )
        positions = {t: float(self.portfolio.net_position(iid)) for t, iid in self.config.instruments.items()}
        self._check("broker positions", True, json.dumps({t: q for t, q in positions.items() if q}) or "{}")

        if self.config.test_order:
            if not all(c["ok"] for c in self.checks.values()):
                self._check("test order", False, "skipped: earlier checks failed")
                return self._finish()
            t, iid = next(iter(self.config.instruments.items()))
            inst = self.cache.instrument(iid)
            px = round(self.last_close[t] * 0.5, 2)
            order = self.order_factory.limit(
                iid, OrderSide.BUY, inst.make_qty(1), inst.make_price(px), time_in_force=TimeInForce.DAY
            )
            self.test_order_id = order.client_order_id
            self.order_events.append(f"submitted BUY 1 {t} LIMIT {px} (half the last close: cannot fill)")
            self.submit_order(order)
            return  # continues in on_order_accepted / on_order_rejected
        self._finish()

    def on_order_accepted(self, ev):
        if ev.client_order_id == self.test_order_id:
            self.order_events.append("accepted by the broker")
            self.cancel_order(self.cache.order(ev.client_order_id))

    def on_order_canceled(self, ev):
        if ev.client_order_id == self.test_order_id:
            self.order_events.append("cancelled")
            self._check("test order", True, "; ".join(self.order_events))
            self._finish()

    def on_order_rejected(self, ev):
        if ev.client_order_id == self.test_order_id:
            self.order_events.append(f"rejected: {ev.reason}")
            self._check("test order", False, "; ".join(self.order_events))
            self._finish()

    def on_order_filled(self, ev):  # should be impossible at half the market price
        self.order_events.append(f"UNEXPECTED FILL {ev.last_qty} @ {ev.last_px}")
        self._check("test order", False, "; ".join(self.order_events))

    def _timeout(self, _event):
        if not self.done:
            if self.config.test_order and "test order" not in self.checks:
                self._check("test order", False, "no broker response in time: " + "; ".join(self.order_events))
            self._finish()

    def _finish(self):
        if self.done:
            return
        self.done = True
        ok = all(c["ok"] for c in self.checks.values())
        report = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "passed": ok, "checks": self.checks}
        Path(self.config.report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.config.report_path).write_text(json.dumps(report, indent=2))
        self.log.info(f"smoke test {'PASSED' if ok else 'FAILED'}: {report}")
        self.shutdown_system(f"smoke test finished ({'passed' if ok else 'failed'})")
