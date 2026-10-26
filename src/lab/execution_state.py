"""Validate execution ledgers before any recovery side effect; accept legacy v1 state."""

import math
from datetime import date


def validate_ledger(state):
    def number(value, name, minimum=None, integer=False):
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or (integer and type(value) is not int)
            or (minimum is not None and value < minimum)
        ):
            raise ValueError(f"invalid execution ledger {name}")

    if (
        not isinstance(state, dict)
        or type(state.get("version", 1)) is not int
        or state.get("version", 1) not in (1, 2, 3)
    ):
        raise ValueError("unknown execution ledger schema")
    if state.get("version") == 3:
        store = state.get("authoritative_store")
        if not isinstance(store, str) or not store.endswith(".sqlite3") or "/" in store or "\\" in store:
            raise ValueError("schema 3 requires its authoritative execution database")
    if "cash" not in state:
        raise ValueError("execution ledger cash is missing")
    for name in ("cash", "commissions", "financing", "external_cash", "borrow_fees"):
        number(state.get(name, 0), name)
    number(state.get("accrued_to", 0), "accrued_to", 0, integer=True)
    if type(state.get("synced_cash", False)) is not bool:
        raise ValueError("invalid execution ledger synced_cash")
    if state.get("account") is not None and (not isinstance(state["account"], str) or not state["account"].strip()):
        raise ValueError("invalid execution ledger account")
    positions = state.get("positions", {})
    if not isinstance(positions, dict):
        raise ValueError("invalid execution ledger positions")
    for ticker, qty in positions.items():
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError("invalid execution ledger ticker")
        number(qty, "position quantity")
    for bucket in ("pending", "disposed", "terminal"):
        records = state.get(bucket, {})
        if not isinstance(records, dict):
            raise ValueError(f"invalid execution ledger {bucket}")
        for coid, intent in records.items():
            if (
                not isinstance(coid, str)
                or not coid.strip()
                or not isinstance(intent, dict)
                or intent.get("side") not in ("BUY", "SELL")
                or not isinstance(intent.get("ticker"), str)
                or not intent["ticker"].strip()
            ):
                raise ValueError(f"invalid {bucket} order intent")
            number(intent.get("qty"), f"{bucket} order quantity", 0)
            if intent["qty"] == 0:
                raise ValueError(f"invalid {bucket} order quantity")
            number(intent.get("filled", 0), f"{bucket} filled quantity", 0)
            for key in ("ts", "decision_ts"):
                if intent.get(key) is not None:
                    number(intent[key], key, 0, integer=True)
            if intent.get("decision_price") is not None:
                number(intent["decision_price"], "decision_price", 0)
            for key in ("adds", "reactivated", "overfilled", "invalid_fill"):
                if key in intent and type(intent[key]) is not bool:
                    raise ValueError(f"invalid {bucket} {key}")
            if bucket == "disposed" and (not isinstance(intent.get("evidence"), str) or not intent["evidence"].strip()):
                raise ValueError("disposed order requires textual evidence")
    trades = state.get("applied_trades", [])
    if not isinstance(trades, list):
        raise ValueError("invalid applied trade records")
    seen = set()
    for trade in trades:
        if isinstance(trade, str):
            ident = trade
        elif isinstance(trade, list) and len(trade) == 3:
            ident, received, coid = trade
            number(received, "trade receipt time", 0, integer=True)
            if coid is not None and (not isinstance(coid, str) or not coid.strip()):
                raise ValueError("invalid applied trade order ID")
        else:
            raise ValueError("invalid applied trade record")
        if not isinstance(ident, str) or not ident.strip() or ident in seen:
            raise ValueError("invalid or duplicate applied trade ID")
        seen.add(ident)
    caps = state.get("daily_caps", {})
    if not isinstance(caps, dict):
        raise ValueError("invalid execution ledger daily caps")
    if caps.get("day") is not None:
        try:
            date.fromisoformat(caps["day"])
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid execution ledger cap day") from exc
    number(caps.get("orders", 0), "cap orders", 0, integer=True)
    number(caps.get("opened", 0), "cap opened", 0)
