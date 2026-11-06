"""Offline Binance USD-M *demo* account export validator.

Consumes a user-supplied JSON bundle of documented GET endpoint responses.
No network, API keys, signing, exchange orders, or economic posting.
An export's provenance and all-scope completeness are unverified claims.
The Binance demo account is NOT the Nautilus sandbox account.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lab.persistence import parse_json

_MAX_BYTES = 4_194_304
_MAX_SPAN_NS = 30_000_000_000
_MAX_INCOME_MS = 86_400_000
_MONEY = re.compile(r"-?(?:0|[1-9][0-9]{0,24})(?:\.[0-9]{1,18})?\Z")
_SYMBOL = re.compile(r"[A-Z0-9]{2,16}USDT\Z")
_ASSET = re.compile(r"[A-Z][A-Z0-9]{1,9}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,110}\Z")
_SENSITIVE_KEYS = {"apikey", "secret", "signature", "privatekey", "authorization", "x-mbx-apikey"}
_TYPES = {"FUNDING_FEE", "COMMISSION", "REALIZED_PNL", "TRANSFER"}
_REQUIRED = {
    "schema_version", "source", "scope", "session_id",
    "collected_start_ns", "collected_end_ns",
    "income_start_ms", "income_end_ms",
    "account", "balances", "position_risk", "open_orders", "income",
}


def _number(raw: object, label: str, *, nonnegative: bool = False) -> Decimal:
    if not isinstance(raw, str) or not _MONEY.fullmatch(raw):
        raise ValueError(f"{label} must be a bounded exact decimal string")
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"invalid {label}") from exc
    if not amount.is_finite() or (nonnegative and amount < 0):
        raise ValueError(f"invalid {label}")
    return amount


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _asset(value: object) -> str:
    if not isinstance(value, str) or not _ASSET.fullmatch(value):
        raise ValueError("invalid demo settlement asset")
    return value


def _symbol(value: object) -> str:
    if not isinstance(value, str) or not _SYMBOL.fullmatch(value):
        raise ValueError("unsupported non-USDT USD-M contract exposure")
    return value


def _time(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"invalid {label} timestamp")
    return value


def _list(obj: object, label: str) -> list:
    if not isinstance(obj, list):
        raise ValueError(f"{label} must be a complete array")
    return obj


def _object(obj: object, label: str) -> dict:
    if not isinstance(obj, dict):
        raise ValueError(f"{label} must be an object")
    return obj


def _no_secrets(obj: object):
    if isinstance(obj, dict):
        for name, value in obj.items():
            if name.lower().replace("_", "").replace("-", "") in _SENSITIVE_KEYS:
                raise ValueError("export contains API credential or signature fields")
            _no_secrets(value)
    elif isinstance(obj, list):
        for value in obj:
            _no_secrets(value)


def load_demo_export(path: Path) -> tuple[dict, str]:
    """Only read a bounded regular file; do not materialize any new files."""
    source = Path(path)
    if source.is_symlink():
        raise ValueError("demo account export must not be a symlink")
    fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        entry = os.fstat(fd)
        if not stat.S_ISREG(entry.st_mode) or not 0 < entry.st_size <= _MAX_BYTES:
            raise ValueError("demo export is not a bounded regular file")
        raw = os.read(fd, _MAX_BYTES + 1)
    finally:
        os.close(fd)
    if not 0 < len(raw) <= _MAX_BYTES:
        raise ValueError("demo export is empty or too large")
    try:
        bundle = parse_json(raw.decode("utf-8"))
    except (UnicodeError, TypeError, ValueError) as exc:
        raise ValueError("demo export has malformed, duplicate-key or nonfinite JSON") from exc
    if not isinstance(bundle, dict) or set(bundle) != _REQUIRED:
        raise ValueError("demo export schema mismatch")
    _no_secrets(bundle)
    return bundle, hashlib.sha256(raw).hexdigest()


def normalize_demo_export(bundle: dict, *, source_sha256: str) -> dict:
    """Require exact endpoint semantics; report is diagnostic, not account authority.

    The requested all-symbol and all-income scope is a user claim, because
    endpoint query parameters and HMAC responses are not authenticated by
    this offline file. A response fetched at different times is non-atomic.
    """
    if not isinstance(bundle, dict) or set(bundle) != _REQUIRED:
        raise ValueError("demo export schema mismatch")
    _no_secrets(bundle)
    if type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1:
        raise ValueError("unsupported demo export schema version")
    if bundle["source"] != "binance-usdm-demo-manual-export" or bundle["scope"] != "all-symbols-all-income-types":
        raise ValueError("demo provenance or query scope assertion missing")
    if not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("invalid demo source digest")
    _id(bundle["session_id"], "demo session")
    start = _time(bundle["collected_start_ns"], "collection start")
    end = _time(bundle["collected_end_ns"], "collection end")
    if not start <= end <= start + _MAX_SPAN_NS:
        raise ValueError("demo collection window is reversed or non-atomic")
    income_start = _time(bundle["income_start_ms"], "income start")
    income_end = _time(bundle["income_end_ms"], "income end")
    if not income_start <= income_end < income_start + _MAX_INCOME_MS:
        raise ValueError("demo income query window must be less than one day")
    if income_end * 1_000_000 > end:
        raise ValueError("demo income query includes unobserved future transactions")

    account = _object(bundle["account"], "demo account information V3")
    if account.get("multiAssetsMargin") is not False:
        raise ValueError("multi-assets demo margin mode is unsupported")
    totals = {}
    for field in ("totalWalletBalance", "totalUnrealizedProfit", "totalMarginBalance", "totalInitialMargin", "totalMaintMargin"):
        totals[field] = _number(
            account.get(field), f"demo account {field}",
            nonnegative=field in {"totalInitialMargin", "totalMaintMargin"}
        )
    if totals["totalWalletBalance"] + totals["totalUnrealizedProfit"] != totals["totalMarginBalance"]:
        raise ValueError("demo margin balance does not equal wallet plus unrealized PnL")

    account_assets = {}
    for item in _list(account.get("assets"), "account assets"):
        asset = _object(item, "account asset")
        currency = _asset(asset.get("asset"))
        if currency in account_assets:
            raise ValueError("duplicate demo account asset")
        account_assets[currency] = _number(asset.get("walletBalance"), "account wallet balance")
    if not account_assets or "USDT" not in account_assets:
        raise ValueError("demo account missing USDT wallet")
    balances = {}
    aliases = set()
    for item in _list(bundle["balances"], "demo balance V3"):
        row = _object(item, "demo balance")
        currency = _asset(row.get("asset"))
        if currency in balances:
            raise ValueError("duplicate demo account balance")
        aliases.add(_id(row.get("accountAlias"), "demo account alias"))
        wallet = _number(row.get("balance"), f"{currency} wallet balance")
        _number(row.get("availableBalance"), f"{currency} available balance")
        _number(row.get("crossWalletBalance"), f"{currency} cross wallet balance")
        _number(row.get("crossUnPnl"), f"{currency} cross unrealized PnL")
        balances[currency] = wallet
    if len(aliases) != 1 or balances != account_assets:
        raise ValueError("demo balance/account asset disagreement or ambiguous account aliases")
    account_alias = next(iter(aliases))

    def position_map(rows: object, label: str) -> dict[str, Decimal]:
        positions = {}
        seen = set()
        for item in _list(rows, label):
            row = _object(item, "demo position")
            symbol = _symbol(row.get("symbol"))
            side = row.get("positionSide")
            if side != "BOTH":
                raise ValueError("hedged demo positions are not supported")
            if symbol in seen:
                raise ValueError("duplicate demo position symbol")
            seen.add(symbol)
            qty = _number(row.get("positionAmt"), f"{symbol} position amount")
            if qty:
                positions[symbol] = qty
        return positions

    embedded = position_map(account.get("positions"), "demo account V3 positions")
    risk_positions = position_map(bundle["position_risk"], "demo position risk V3")
    if embedded != risk_positions:
        raise ValueError("demo account V3/position risk position quantities disagree")

    orders = {}
    for item in _list(bundle["open_orders"], "demo all open orders"):
        row = _object(item, "demo order")
        symbol = _symbol(row.get("symbol"))
        order_id = _id(row.get("clientOrderId"), "demo client order ID")
        if order_id in orders:
            raise ValueError("duplicate demo client order ID")
        if row.get("status") not in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}:
            raise ValueError("demo open order has unrecognized nonterminal state")
        if row.get("side") not in {"BUY", "SELL"}:
            raise ValueError("invalid demo open order side")
        original = _number(row.get("origQty"), "demo order original quantity", nonnegative=True)
        executed = _number(row.get("executedQty"), "demo order executed quantity", nonnegative=True)
        remaining = original - executed
        if remaining <= 0:
            raise ValueError("demo open order remaining quantity must be positive")
        orders[order_id] = {"symbol": symbol, "remaining": str(remaining)}
    # Income is at best a bounded, asserted single-page export, with no
    # server-side proof of all transactions or completeness.
    income = _list(bundle["income"], "demo income history")
    if len(income) >= 1000:
        raise ValueError("demo income history reached page limit; completeness unknown")
    seen_income = set()
    totals_income: dict[tuple[str, str], Decimal] = {}
    for item in income:
        row = _object(item, "demo income row")
        income_type = _id(row.get("incomeType"), "demo income type")
        asset = _asset(row.get("asset"))
        ts = _time(row.get("time"), "demo income")
        if not income_start <= ts <= income_end:
            raise ValueError("demo income row outside requested coverage")
        tran = row.get("tranId")
        if type(tran) is not int or tran < 0:
            raise ValueError("invalid demo income transaction ID")
        key = (income_type, tran)
        if key in seen_income:
            raise ValueError("duplicate demo income transaction within type")
        seen_income.add(key)
        _symbol(row["symbol"]) if row.get("symbol") else None
        amount = _number(row.get("income"), "demo income cash amount")
        totals_income[(income_type, asset)] = totals_income.get((income_type, asset), Decimal(0)) + amount
    return {
        "status": "demo_account_export_consistent_unverified",
        "environment": "binance-usdm-demo",
        "session_id": bundle["session_id"],
        "source_sha256": source_sha256,
        "account_alias_sha256": hashlib.sha256(account_alias.encode()).hexdigest(),
        "collected_start_ns": start,
        "collected_end_ns": end,
        "income_start_ms": income_start,
        "income_end_ms": income_end,
        "wallet_balances": {k: str(v) for k, v in sorted(balances.items())},
        "open_positions": {k: str(v) for k, v in sorted(risk_positions.items())},
        "open_orders": dict(sorted(orders.items())),
        "income_claims_by_type_currency": {
            f"{k[0]}:{k[1]}": str(v) for k, v in sorted(totals_income.items())
        },
        "unknown_income_type_count": sum(
            1 for item in income if item["incomeType"] not in _TYPES
        ),
        "provider_identity_authenticated": False,
        "independent_collection_verified": False,
        "complete_income_coverage_verified": False,
        "account_state_atomic": False,
        "nautilus_simulator_account_matched": False,
        "equity_reconciled": False,
        "margin_qualified": False,
        "risk_ready": False,
        "economic_posting_allowed": False,
    }


def inspect_demo_export(path: Path) -> dict:
    bundle, sha = load_demo_export(path)
    return normalize_demo_export(bundle, source_sha256=sha)
