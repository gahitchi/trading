"""SmokeTest: checks paper connectivity; optional test-order mode submits a real paper trade."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nautilus_trader.model import OrderSide, TimeInForce
from nautilus_trader.trading import Strategy, StrategyConfig

from lab.persistence import atomic_json, read_json
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
    4. (optional, --test-order) a REAL paper order: a 1-share limit buy at half the last close,
       cancelled as soon as IB accepts it. It normally does not fill, but it can (a gap down
       through the limit, or a fill before the cancel lands: G007-F13, ChatGPT). If it fills, a
       closing order is sent at once and the smoke test FAILS, naming both orders; should even
       that not settle, the agents' start-up reconciliation finds the stray share and hard-halts.
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
        self.close_order_id = None
        self.done = False
        self.state_path = Path(config.report_path).parent / "smoke-state.json"
        self.intents = {}
        self.seen_trades = set()
        self.initial_positions = {}
        self.had_fill = False
        self.state_account = config.paper_account
        if self.state_path.exists():
            saved = read_json(self.state_path)
            if saved.get("unresolved"):
                self.state_account = saved.get("account")
                self.intents = saved["intents"]
                self.seen_trades = set(saved.get("trades", []))
                self.initial_positions = saved["initial_positions"]
                self.had_fill = saved.get("had_fill", False)

    def on_start(self):
        if self.intents:
            self._check("recovery", False, "prior smoke order intents require terminal reconciliation")
            self._after(5, self._recover)
            self._after(self.config.wait_s + 90, self._timeout)
            return
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
            self.initial_positions = positions
            self.intents[str(order.client_order_id)] = {"side": "BUY", "ticker": t, "qty": 1}
            self._persist(True)
            self.order_events.append(f"submitted BUY 1 {t} LIMIT {px} (half the last close; a real paper order)")
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
            self._check("test order", not self.had_fill, "; ".join(self.order_events))
            self._after(1, self._recover)

    def on_order_rejected(self, ev):
        if ev.client_order_id == self.test_order_id:
            self.order_events.append(f"rejected: {ev.reason}")
            self._check("test order", False, "; ".join(self.order_events))
            self._after(1, self._recover)

    def on_order_filled(self, ev):
        trade = str(ev.trade_id)
        if trade in self.seen_trades:
            return
        self.seen_trades.add(trade)
        if ev.client_order_id == self.test_order_id:
            self.had_fill = True
            # Unlikely, not impossible. Undo it, and fail: the operator must look at the account.
            self.order_events.append(f"UNEXPECTED FILL {ev.last_qty} @ {ev.last_px}")
            close = self.order_factory.market(
                ev.instrument_id, OrderSide.SELL, ev.last_qty, time_in_force=TimeInForce.DAY
            )
            self.close_order_id = close.client_order_id
            self.intents[str(close.client_order_id)] = {
                "side": "SELL",
                "ticker": str(ev.instrument_id.symbol),
                "qty": float(ev.last_qty),
            }
            self._persist(True)
            self.submit_order(close)
            self.order_events.append(f"closing order {close.client_order_id} sent (SELL {ev.last_qty} at market)")
            self._check("test order", False, "; ".join(self.order_events) + "; check the paper account")
        elif str(ev.client_order_id) in self.intents:
            self.order_events.append(f"closing order filled {ev.last_qty} @ {ev.last_px}")
            self._check("test order", False, "; ".join(self.order_events) + "; position closed, check the account")
            self._persist(True)
            self._after(1, self._recover)

    def _persist(self, unresolved):
        atomic_json(
            self.state_path,
            {
                "version": 1,
                "account": self.state_account,
                "unresolved": unresolved,
                "intents": self.intents,
                "trades": sorted(self.seen_trades),
                "initial_positions": self.initial_positions,
                "had_fill": self.had_fill,
            },
        )

    def _recover(self, _event):
        from nautilus_trader.model import ClientOrderId

        account = str(self.cache.account_id(self.config.venue))
        if (
            self.state_account != self.config.paper_account
            or not is_paper_account(account)
            or account.split("-", 1)[-1] != self.state_account
        ):
            self._check("recovery account", False, "journal account identity is missing or differs from the broker")
            return self._finish()
        terminal = all((o := self.cache.order(ClientOrderId(k))) is not None and o.is_closed for k in self.intents)
        positions = {t: float(self.portfolio.net_position(iid)) for t, iid in self.config.instruments.items()}
        flat = positions == self.initial_positions
        if terminal and flat:
            self._persist(False)
            self._check("settled", True, "every journaled order terminal; initial positions restored")
            self._finish()
        elif not self.done:
            self._persist(True)
            self._check("settled", False, "unresolved orders or changed positions; no replacement order permitted")
            self._after(5, self._recover)

    def _timeout(self, _event):
        if not self.done:
            if self.config.test_order and "test order" not in self.checks:
                self._check("test order", False, "no broker response in time: " + "; ".join(self.order_events))
            self._finish()

    def _finish(self):
        if self.done:
            return
        if self.intents and not self.checks.get("settled", {}).get("ok"):
            self._check("settled", False, "terminal order and restored-position evidence is still missing")
        self.done = True
        ok = bool(self.checks) and all(c["ok"] for c in self.checks.values())
        report = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "passed": ok, "checks": self.checks}
        Path(self.config.report_path).parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.config.report_path, report)
        self.log.info(f"smoke test {'PASSED' if ok else 'FAILED'}: {report}")
        self.shutdown_system(f"smoke test finished ({'passed' if ok else 'failed'})")
