"""
A simulated Interactive Brokers paper account, for the live-node harness.

Plain Python with its state in one JSON file, so it outlives node restarts exactly like a
real broker does: orders sent before a crash are still there afterwards, and fills that
happen while the node is down are reported when it reconnects.

Behaviour modelled on IBKR (as used by this lab):
  - market-on-open orders (MARKET + AT_THE_OPEN) queue until the next session's open and
    fill at its opening price; a partially filled remainder is cancelled after the auction
  - intraday (quick speeds): MARKET and LIMIT DAY orders fill at the next trade print the harness
    delivers (open_session with the bar's opening prices); DAY orders expire at the close (expire_day)
  - resting limit orders fill at the open if the open crosses their limit
  - IBKR Pro Fixed commissions, and optionally one tick of slippage on market orders. With slippage off,
    a harness run and a backtest with [backtest] slippage = false over the same bars must produce the same
    ledger (the backtest's modelled spread and impact have no counterpart here)
  - sells beyond the position are refused unless short selling is enabled for the account
  - an account ID ("DU..." = paper)

Fault injection (one-shot, consumed in order): rejecting the next order, partially filling
the next fill, reporting a different account. They are what the harness tests are about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from lab.costs import ibkr_fixed_commission

TICK = 0.01


class BrokerError(Exception):
    pass


@dataclass
class Fill:
    client_order_id: str
    venue_order_id: str
    trade_id: str
    ticker: str
    side: str  # BUY | SELL
    qty: float
    px: float
    commission: float
    ts: int


class SimBroker:
    def __init__(
        self,
        path: str | Path,
        *,
        account: str = "DU1234567",
        cash: float = 100_000.0,
        slippage: bool = True,
        commission: dict | None = None,
        allow_short: bool = False,
        shortable: set[str] | None = None,
    ):
        self.path = Path(path)
        self.shortable = shortable  # None = every stock can be borrowed; otherwise only these
        self.commission = commission or {}
        self.slippage = slippage
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
        else:
            self.state = {
                "account": account,
                "cash": cash,
                "positions": {},
                "orders": {},  # client_order_id -> order record
                "fills": [],
                "faults": [],
                "seq": 0,
                "allow_short": allow_short,
            }
            self.save()

    # ------------------------------------------------------------------ persistence

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.path)

    def _next(self, prefix: str) -> str:
        self.state["seq"] += 1
        return f"{prefix}{self.state['seq']}"

    # ------------------------------------------------------------------ faults

    def inject(self, kind: str, **params):
        """kinds: reject (reason), partial (fraction), account (id)."""
        if kind not in ("reject", "partial", "account"):
            raise BrokerError(f"unknown fault {kind!r}")
        if kind == "account":
            self.state["account"] = params["id"]
        else:
            self.state["faults"].append({"kind": kind, **params})
        self.save()

    def _take_fault(self, kind: str) -> dict | None:
        for i, f in enumerate(self.state["faults"]):
            if f["kind"] == kind:
                return self.state["faults"].pop(i)
        return None

    # ------------------------------------------------------------------ account

    @property
    def account(self) -> str:
        return self.state["account"]

    @property
    def cash(self) -> float:
        return float(self.state["cash"])

    def position(self, ticker: str) -> float:
        return float(self.state["positions"].get(ticker, 0.0))

    def positions(self) -> dict[str, float]:
        return {t: float(q) for t, q in self.state["positions"].items() if q}

    def open_orders(self) -> list[dict]:
        return [o for o in self.state["orders"].values() if o["status"] == "ACCEPTED"]

    # ------------------------------------------------------------------ orders

    def submit(
        self,
        *,
        client_order_id: str,
        ticker: str,
        side: str,
        qty: float,
        order_type: str,
        tif: str,
        limit: float | None = None,
        ts: int = 0,
    ) -> dict:
        """Returns the order record; status ACCEPTED or REJECTED (with reason)."""
        if client_order_id in self.state["orders"]:
            # IBKR refuses a duplicate order ID; the harness tests that a restarted node never reuses one.
            rec = {"client_order_id": client_order_id, "status": "REJECTED", "reason": "duplicate order id"}
            return rec
        rec = {
            "client_order_id": client_order_id,
            "venue_order_id": self._next("V"),
            "ticker": ticker,
            "side": side,
            "qty": float(qty),
            "filled": 0.0,
            "type": order_type,
            "tif": tif,
            "limit": limit,
            "status": "ACCEPTED",
            "reason": None,
            "ts_accepted": ts,
            "ts_last": ts,
        }
        fault = self._take_fault("reject")
        if fault:
            rec["status"], rec["reason"] = "REJECTED", fault.get("reason", "rejected by broker")
        elif order_type == "LIMIT" and limit is None:
            rec["status"], rec["reason"] = "REJECTED", "limit order without a price"
        elif order_type not in ("MARKET", "LIMIT"):
            rec["status"], rec["reason"] = "REJECTED", f"order type {order_type} not supported"
        elif side == "SELL":
            pending = sum(
                o["qty"] - o["filled"] for o in self.open_orders() if o["ticker"] == ticker and o["side"] == "SELL"
            )
            opens_short = qty + pending > self.position(ticker) + 1e-9
            if opens_short and not self.state["allow_short"]:
                rec["status"], rec["reason"] = "REJECTED", "short sale not permitted in this account"
            elif opens_short and self.shortable is not None and ticker not in self.shortable:
                rec["status"], rec["reason"] = "REJECTED", "no shares available to borrow"
        self.state["orders"][client_order_id] = rec
        self.save()
        return rec

    def cancel(self, client_order_id: str, ts: int = 0) -> dict | None:
        rec = self.state["orders"].get(client_order_id)
        if rec is None or rec["status"] != "ACCEPTED":
            return None
        rec["status"], rec["ts_last"] = "CANCELED", ts
        self.save()
        return rec

    # ------------------------------------------------------------------ the open

    def open_session(self, opens: dict[str, float], ts: int) -> tuple[list[Fill], list[dict]]:
        """Run the opening auction: fill queued orders at `opens` (ticker -> opening price).

        Returns (fills, orders that were cancelled after a partial fill)."""
        fills, cancelled = [], []
        for rec in sorted(self.open_orders(), key=lambda r: r["side"] != "SELL"):  # sells first, as IB nets
            t = rec["ticker"]
            if t not in opens:
                continue  # no auction for this stock today (halted / delisted): order stays queued
            open_px = float(opens[t])
            if rec["type"] == "LIMIT":
                crosses = open_px <= rec["limit"] if rec["side"] == "BUY" else open_px >= rec["limit"]
                if not crosses:
                    continue
            px = open_px
            if self.slippage and rec["type"] == "MARKET":
                px = round(open_px + (TICK if rec["side"] == "BUY" else -TICK), 2)
            qty = rec["qty"] - rec["filled"]
            fault = self._take_fault("partial")
            if fault:
                qty = max(1.0, float(int(qty * float(fault.get("fraction", 0.5)))))
            fee = round(
                ibkr_fixed_commission(
                    qty,
                    px,
                    per_share=self.commission.get("per_share", 0.005),
                    minimum=self.commission.get("minimum", 1.0),
                    max_pct=self.commission.get("max_pct", 0.01),
                ),
                2,
            )
            sign = 1 if rec["side"] == "BUY" else -1
            self.state["cash"] = self.cash - sign * qty * px - fee
            self.state["positions"][t] = self.position(t) + sign * qty
            rec["filled"] += qty
            rec["ts_last"] = ts
            fill = Fill(
                rec["client_order_id"], rec["venue_order_id"], self._next("T"), t, rec["side"], qty, px, fee, ts
            )
            self.state["fills"].append(fill.__dict__)
            fills.append(fill)
            if rec["filled"] >= rec["qty"] - 1e-9:
                rec["status"] = "FILLED"
            elif rec["type"] == "MARKET":
                rec["status"] = "CANCELED"  # an opening-auction remainder does not carry into the session
                rec["reason"] = "unfilled remainder after the opening auction"
                cancelled.append(rec)
        self.save()
        return fills, cancelled

    def expire_day(self, ts: int) -> list[dict]:
        """The close: working DAY orders expire, as at IBKR."""
        out = []
        for rec in self.open_orders():
            if rec["tif"] != "AT_THE_OPEN":
                rec["status"], rec["reason"], rec["ts_last"] = "CANCELED", "DAY order expired at the close", ts
                out.append(rec)
        self.save()
        return out

    def fills_for(self, client_order_id: str) -> list[Fill]:
        return [Fill(**f) for f in self.state["fills"] if f["client_order_id"] == client_order_id]

    def all_fills(self) -> list[Fill]:
        return [Fill(**f) for f in self.state["fills"]]
