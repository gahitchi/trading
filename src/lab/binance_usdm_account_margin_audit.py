"""Offline Binance USD-M demo position-vs-tier cross-check; no account authority.

Reads a caller-supplied, SHA-256-pinned bundle of REST response snapshots.
Each endpoint and capture timestamp is separately checked. Binance V3
positionRisk lacks leverage/marginType, so symbolConfig and account modes
are mandatory. The result never authenticates Binance's response, asserts
atomic account state, approves orders or reconstructs historic positions.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from lab.binance_usdm_margin_rules import _decimal, _maintenance, parse_leverage_brackets, source_sha256

MODEL = "binance-usdm-demo-position-margin-crosscheck-v1"
_MAX_CAPTURE_WINDOW_MS = 5_000
_MAX_BYTES = 1_000_000
_ENDPOINTS = {
    "position": "/fapi/v3/positionRisk",
    "symbol_config": "/fapi/v1/symbolConfig",
    "position_mode": "/fapi/v1/positionSide/dual",
    "asset_mode": "/fapi/v1/multiAssetsMargin",
    "brackets": "/fapi/v1/leverageBracket",
}
_SECRET_FIELDS = {"apikey", "apisecret", "secret", "signature", "privatekey", "authorization"}


@dataclass(frozen=True)
class AccountMarginDiagnostic:
    model: str
    environment: str
    symbol: str
    bundle_sha256: str
    bracket_sha256: str
    collected_start_ms: int
    collected_end_ms: int
    quantity: Decimal
    entry_price: Decimal
    snapshot_mark_price: Decimal
    isolated_wallet: Decimal
    reported_unrealized: Decimal
    modeled_unrealized: Decimal
    reported_maintenance: Decimal
    modeled_maintenance: Decimal
    current_equity: Decimal
    current_maintenance_excess: Decimal
    maintenance_bracket: int
    selected_leverage: int
    status: str = "CONSISTENT_UNAUTHENTICATED_RESEARCH_ONLY"
    provider_authenticated: bool = False
    account_state_atomic: bool = False
    mark_history_verified: bool = False
    liquidation_qualified: bool = False
    execution_authorized: bool = False


def _deny_secrets(item: object) -> None:
    if isinstance(item, dict):
        for k, value in item.items():
            if not isinstance(k, str) or k.lower().replace("-", "").replace("_", "") in _SECRET_FIELDS:
                raise ValueError("account snapshot contains prohibited credential fields")
            _deny_secrets(value)
    elif isinstance(item, (list, tuple)):
        for value in item:
            _deny_secrets(value)


def _single_symbol(payload: object, symbol: str, label: str) -> dict:
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ValueError(f"{label} must contain exactly one full symbol record")
    record = payload[0]
    if record.get("symbol") != symbol:
        raise ValueError(f"{label} symbol mismatch")
    return record


def audit_demo_account_margin(
    bundle: dict,
    *,
    expected_bundle_sha256: str,
    expected_bracket_sha256: str,
    tolerance_usdt: str = "0.01",
) -> AccountMarginDiagnostic:
    """Reconcile claimed V3 position risk to account-specific tier math.

    Positive/negative funding and order modifications cannot be inferred
    from this snapshot. No attribution of current wallet to entry-time
    margin, or backward application of current tiers, is permitted.
    """
    if not isinstance(bundle, dict) or set(bundle) != {
        "version", "environment", "symbol", "collected_start_ms", "collected_end_ms", "sources"
    }:
        raise ValueError("invalid account snapshot envelope schema")
    _deny_secrets(bundle)
    digest = source_sha256(bundle)
    if (
        not isinstance(expected_bundle_sha256, str)
        or len(expected_bundle_sha256) != 64
        or digest != expected_bundle_sha256
    ):
        raise ValueError("account snapshot SHA-256 mismatch")
    if len(str(bundle)) > _MAX_BYTES:
        raise ValueError("account snapshot exceeds size limit")
    if type(bundle["version"]) is not int or bundle["version"] != 1:
        raise ValueError("unsupported account snapshot version")
    if bundle["environment"] != "binance-usdm-demo":
        raise ValueError("only Binance demo account snapshots are accepted")
    symbol = bundle["symbol"]
    # The margin-tier parser validates the exact symbol grammar.
    sources = bundle["sources"]
    if not isinstance(sources, dict) or set(sources) != set(_ENDPOINTS):
        raise ValueError("missing or unexpected account snapshot endpoints")
    start, end = bundle["collected_start_ms"], bundle["collected_end_ms"]
    if (
        type(start) is not int or type(end) is not int or start <= 0
        or not start <= end <= start + _MAX_CAPTURE_WINDOW_MS
    ):
        raise ValueError("account collection intervals must be bounded and ordered")

    data = {}
    for key, endpoint in _ENDPOINTS.items():
        item = sources[key]
        if not isinstance(item, dict) or set(item) != {"endpoint", "captured_ms", "payload"}:
            raise ValueError("invalid account source envelope")
        timestamp = item["captured_ms"]
        if item["endpoint"] != endpoint or type(timestamp) is not int or not start <= timestamp <= end:
            raise ValueError("source endpoint identity or timestamp mismatch")
        data[key] = item["payload"]

    if data["position_mode"] != {"dualSidePosition": False}:
        raise ValueError("one-way position mode not independently confirmed")
    if data["asset_mode"] != {"multiAssetsMargin": False}:
        raise ValueError("single-asset mode not independently confirmed")

    config = _single_symbol(data["symbol_config"], symbol, "symbolConfig")
    position = _single_symbol(data["position"], symbol, "positionRisk V3")
    if (
        config.get("marginType") != "ISOLATED"
        or config.get("isAutoAddMargin") is not False
        or position.get("positionSide") != "BOTH"
        or position.get("marginAsset") != "USDT"
    ):
        raise ValueError("unsupported isolated or one-way account position settings")
    leverage = config.get("leverage")
    if type(leverage) is not int or leverage < 1:
        raise ValueError("invalid symbolConfig leverage")
    if type(position.get("updateTime")) is not int or not 0 < position["updateTime"] <= end:
        raise ValueError("invalid position last-update timestamp")
    # updateTime is position's LAST CHANGE, not endpoint response time.
    # A quiet position can have an old updateTime; do not misuse it as
    # freshness proof. The separately captured endpoint times are bounded.

    with localcontext() as ctx:
        ctx.prec = 70
        amt = _decimal(position.get("positionAmt"), "positionAmt")
        if amt >= 0:
            raise ValueError("diagnostic requires one live one-way SHORT position")
        q = -amt
        entry = _decimal(position.get("entryPrice"), "entryPrice", positive=True)
        mark = _decimal(position.get("markPrice"), "markPrice", positive=True)
        wallet = _decimal(position.get("isolatedWallet"), "isolatedWallet", positive=True)
        upnl = _decimal(position.get("unRealizedProfit"), "unRealizedProfit")
        notional = _decimal(position.get("notional"), "notional")
        reported_mm = _decimal(position.get("maintMargin"), "maintMargin")
        reported_initial = _decimal(position.get("positionInitialMargin"), "positionInitialMargin")
        max_notional = _decimal(config.get("maxNotionalValue"), "maxNotionalValue", positive=True)
        tol = _decimal(tolerance_usdt, "USDT tolerance")
        if tol < 0 or tol > Decimal("0.1") or reported_mm < 0 or reported_initial < 0:
            raise ValueError("unsupported tolerance or negative reported margin")
        if abs(abs(notional) - q * mark) > tol:
            raise ValueError("position notional disagrees with quantity times mark")
        expected_upnl = q * (entry - mark)
        if abs(upnl - expected_upnl) > tol:
            raise ValueError("reported unrealized PnL disagrees with short mark")
        bracket_source = data["brackets"]
        bracket_sha = source_sha256(bracket_source)
        if bracket_sha != expected_bracket_sha256:
            raise ValueError("bracket SHA-256 disagrees with separately frozen source")
        schedule = parse_leverage_brackets(
            bracket_source, symbol=symbol, expected_sha256=expected_bracket_sha256
        )
        tier = schedule.tier(q * mark)
        if leverage > tier.max_initial_leverage or q * mark > max_notional:
            raise ValueError("account configured leverage/notional exceeds supported tier")
        modeled_mm, _ = _maintenance(schedule, q, mark)
        if abs(modeled_mm - reported_mm) > tol:
            raise ValueError("exchange-reported maintenance disagrees with pinned bracket math")
        # These are SNAPSHOT diagnostics, not entry-time collateral
        # requirements or actual liquidation/insurance reserve.
        equity = wallet + upnl
        excess = equity - modeled_mm
        return AccountMarginDiagnostic(
            model=MODEL,
            environment=bundle["environment"],
            symbol=symbol,
            bundle_sha256=digest,
            bracket_sha256=bracket_sha,
            collected_start_ms=start,
            collected_end_ms=end,
            quantity=q,
            entry_price=entry,
            snapshot_mark_price=mark,
            isolated_wallet=wallet,
            reported_unrealized=upnl,
            modeled_unrealized=expected_upnl,
            reported_maintenance=reported_mm,
            modeled_maintenance=modeled_mm,
            current_equity=equity,
            current_maintenance_excess=excess,
            maintenance_bracket=tier.bracket,
            selected_leverage=leverage,
        )
