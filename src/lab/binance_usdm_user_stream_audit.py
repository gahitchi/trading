"""Offline USD-M user-data event consistency audit; never continuity proof.

Only checks a caller-supplied bounded stream segment against two snapshots.
Binance websocket events have no global gap-free sequence counter. Therefore
even a fully self-consistent segment CANNOT authenticate source provenance,
prove completeness, reconstruct executions or authorize risk/trading actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from lab.binance_usdm_margin_rules import _decimal, source_sha256

MODEL = "binance-usdm-observed-user-stream-consistency-v1"
MAX_EVENTS = 10_000
MAX_SPAN_MS = 3_600_000
MAX_CLOCK_SKEW_MS = 60_000
_SUPPORTED_REASONS = {"ORDER", "FUNDING_FEE", "DEPOSIT", "WITHDRAW", "MARGIN_TYPE_CHANGE", "ASSET_TRANSFER"}
_SECRET_NAMES = {
    "apikey",
    "apisecret",
    "authorization",
    "signature",
    "privatekey",
    "password",
    "token",
    "secret",
}


@dataclass(frozen=True)
class StreamAudit:
    model: str
    source_sha256: str
    environment: str
    first_event_ms: int | None
    last_event_ms: int | None
    account_updates: int
    order_updates: int
    observed_trade_ids: tuple[int, ...]
    unresolved_order_updates: int
    reported_wallet_delta: Decimal
    observed_wallet_delta: Decimal
    ending_quantity: Decimal
    status: str = "OBSERVED_EVENTS_CONSISTENT_UNVERIFIED"
    provider_authenticated: bool = False
    websocket_delivery_complete: bool = False
    execution_history_complete: bool = False
    risk_ready: bool = False
    execution_authorized: bool = False


def _no_secrets(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key.lower().replace("_", "").replace("-", "") in _SECRET_NAMES:
                raise ValueError("stream evidence contains prohibited credential fields")
            _no_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _no_secrets(child)


def _ms(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"invalid {label} timestamp")
    return value


def _snapshot(value: object, symbol: str) -> tuple[Decimal, Decimal]:
    """Require a comparable full-USDT wallet and one-way net position snapshot."""
    if not isinstance(value, dict) or set(value) != {"wallet_balance", "position_amt", "symbol"}:
        raise ValueError("incomplete comparison snapshot")
    if value["symbol"] != symbol:
        raise ValueError("snapshot symbol mismatch")
    wallet = _decimal(value["wallet_balance"], "wallet balance")
    quantity = _decimal(value["position_amt"], "position quantity")
    return wallet, quantity


def audit_user_stream(bundle: object, *, expected_sha256: str) -> StreamAudit:
    """Validate observed events and reconcile only explicit comparable fields.

    ACCOUNT_UPDATE is sparse, not a full snapshot; absent B/P items mean
    unchanged by *that observed update*, not evidence of global continuity.
    ORDER_TRADE_UPDATE reports a trade identifier, but it cannot by itself
    prove the account-update PnL and commission ledger is complete.
    """
    if not isinstance(bundle, dict):
        raise ValueError("user-data evidence must be a JSON object")
    _no_secrets(bundle)
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64 or source_sha256(bundle) != expected_sha256:
        raise ValueError("user-data source SHA-256 mismatch")
    required = {
        "version",
        "environment",
        "symbol",
        "start_ms",
        "end_ms",
        "opening",
        "closing",
        "stream",
        "events",
    }
    if set(bundle) != required or type(bundle["version"]) is not int or bundle["version"] != 1:
        raise ValueError("unsupported user-data evidence schema")
    if bundle["environment"] not in {"binance-usdm-demo", "binance-usdm-mainnet"}:
        raise ValueError("unsupported stream environment")
    symbol = bundle["symbol"]
    if not isinstance(symbol, str) or not symbol.endswith("USDT"):
        raise ValueError("unsupported stream symbol")
    start = _ms(bundle["start_ms"], "start")
    end = _ms(bundle["end_ms"], "end")
    if not start < end <= start + MAX_SPAN_MS:
        raise ValueError("invalid bounded stream interval")
    opened_wallet, opened_position = _snapshot(bundle["opening"], symbol)
    ending_wallet, ending_position = _snapshot(bundle["closing"], symbol)
    stream = bundle["stream"]
    if not isinstance(stream, dict) or set(stream) != {
        "connected_ms",
        "disconnected_ms",
        "reconnects",
        "listen_key_expired",
        "message_capture_complete",
    }:
        raise ValueError("incomplete websocket capture metadata")
    if (
        _ms(stream["connected_ms"], "connected") > start
        or _ms(stream["disconnected_ms"], "disconnected") < end
        or type(stream["reconnects"]) is not int
        or stream["reconnects"] != 0
        or stream["listen_key_expired"] is not False
        or stream["message_capture_complete"] is not True
    ):
        raise ValueError("websocket capture gap, expiry, or unverified local recording")
    events = bundle["events"]
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError("invalid bounded event list")
    wallet, quantity = opened_wallet, opened_position
    account_updates = order_updates = unresolved = 0
    seen_trades: set[tuple[str, int, int]] = set()
    seen_orders: dict[tuple[str, int], tuple[Decimal, int]] = {}
    last_time = None
    first_time = None
    for item in events:
        if not isinstance(item, dict) or set(item) != {"received_ms", "payload"}:
            raise ValueError("invalid captured event envelope")
        received = _ms(item["received_ms"], "receive")
        event = item["payload"]
        if not isinstance(event, dict):
            raise ValueError("invalid websocket event body")
        event_time = _ms(event.get("E"), "event")
        transaction_time = _ms(event.get("T"), "transaction")
        if (
            not start <= event_time <= end
            or not event_time <= received <= end + MAX_CLOCK_SKEW_MS
            or abs(event_time - transaction_time) > MAX_CLOCK_SKEW_MS
            or (last_time is not None and event_time <= last_time)
        ):
            raise ValueError("unordered, out-of-window or delayed user-data event")
        last_time = event_time
        if first_time is None:
            first_time = event_time
        kind = event.get("e")
        if kind == "ACCOUNT_UPDATE":
            if set(event) != {"e", "E", "T", "a"} or not isinstance(event["a"], dict):
                raise ValueError("incomplete account update")
            data = event["a"]
            if set(data) != {"m", "B", "P"} or data["m"] not in _SUPPORTED_REASONS:
                raise ValueError("unsupported account update reason or schema")
            if not isinstance(data["B"], list) or not isinstance(data["P"], list):
                raise ValueError("incomplete account balances/positions")
            used_assets = set()
            for balance in data["B"]:
                if not isinstance(balance, dict) or set(balance) != {"a", "wb", "cw", "bc"}:
                    raise ValueError("incomplete balance update")
                asset = balance["a"]
                if asset in used_assets or asset != "USDT":
                    raise ValueError("duplicate or unsupported balance asset")
                used_assets.add(asset)
                updated_wallet = _decimal(balance["wb"], "USDT wallet")
                _decimal(balance["cw"], "cross wallet")
                balance_change = _decimal(balance["bc"], "balance change")
                if updated_wallet - wallet != balance_change:
                    raise ValueError("balance change does not conserve observed wallet cash")
                wallet = updated_wallet
            used_positions = set()
            for position in data["P"]:
                required_fields = {"s", "pa", "ep", "cr", "up", "mt", "iw", "ps"}
                if not isinstance(position, dict) or set(position) != required_fields:
                    raise ValueError("incomplete position update")
                key = (position["s"], position["ps"])
                if key in used_positions or key != (symbol, "BOTH") or position["mt"] != "isolated":
                    raise ValueError("unsupported/duplicate one-way isolated position update")
                used_positions.add(key)
                quantity = _decimal(position["pa"], "positionAmt")
                if quantity > 0:
                    raise ValueError("one-way short-only scope violated by intermediate long")
                _decimal(position["ep"], "entryPrice")
                _decimal(position["cr"], "accumulated realized")
                _decimal(position["up"], "unrealized profit")
                _decimal(position["iw"], "isolated wallet")
            account_updates += 1
        elif kind == "ORDER_TRADE_UPDATE":
            if set(event) != {"e", "E", "T", "o"} or not isinstance(event["o"], dict):
                raise ValueError("incomplete order update")
            order = event["o"]
            needed = {"s", "i", "t", "x", "X", "z", "l", "rp", "n", "N", "S", "ps"}
            if not needed <= set(order) or order["s"] != symbol or order["ps"] != "BOTH":
                raise ValueError("unsupported order update")
            oid, tid = order["i"], order["t"]
            if type(oid) is not int or oid < 0 or type(tid) is not int or tid < 0:
                raise ValueError("invalid exchange order/trade IDs")
            filled = _decimal(order["z"], "cumulative filled")
            last_fill = _decimal(order["l"], "last fill")
            if filled < 0 or last_fill < 0:
                raise ValueError("negative trade fill quantity")
            previous = seen_orders.get((symbol, oid))
            if previous is not None and filled < previous[0]:
                raise ValueError("order cumulative fill quantity decreased")
            if order["x"] == "TRADE":
                if tid == 0 or last_fill <= 0 or (symbol, oid, tid) in seen_trades:
                    raise ValueError("duplicate or empty trade execution ID")
                if previous is not None and filled - previous[0] != last_fill:
                    raise ValueError("trade fill does not conserve cumulative quantity")
                seen_trades.add((symbol, oid, tid))
                _decimal(order["rp"], "realized PnL")
                _decimal(order["n"], "commission")
                unresolved += 1
            elif last_fill != 0:
                raise ValueError("nontrade order update contains a fill")
            seen_orders[(symbol, oid)] = (filled, event_time)
            order_updates += 1
        else:
            raise ValueError("unsupported stream event: possible gap or mode change")
    if wallet != ending_wallet or quantity != ending_position:
        raise ValueError("observed stream does not reconcile with closing account snapshot")
    if opened_position > 0 or ending_position > 0:
        raise ValueError("one-way short-only research scope was violated")
    return StreamAudit(
        model=MODEL,
        source_sha256=expected_sha256,
        environment=bundle["environment"],
        first_event_ms=first_time,
        last_event_ms=last_time,
        account_updates=account_updates,
        order_updates=order_updates,
        observed_trade_ids=tuple(sorted(tid for _, _, tid in seen_trades)),
        unresolved_order_updates=unresolved,
        reported_wallet_delta=ending_wallet - opened_wallet,
        observed_wallet_delta=wallet - opened_wallet,
        ending_quantity=quantity,
    )
