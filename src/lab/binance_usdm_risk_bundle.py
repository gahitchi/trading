"""Read-only offline Binance USD-M isolated short risk evidence integration.

Inputs are *user-supplied* endpoint snapshots with bounded capture metadata.
Hash checking and endpoint-shape validation are not provider authentication.
Repeated snapshots detect some state drift but cannot establish a continuous,
transactionally atomic account history or authorize trading.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from lab.binance_usdm_margin_rules import (
    MINUTE_MS,
    IsolatedShort,
    IsolatedShortRisk,
    MarkEvidence,
    _decimal,
    assess_isolated_short,
    source_sha256,
)
from lab.persistence import parse_json

KIND = "binance-usdm-isolated-short-risk-offline-v1"
_MAX_BYTES = 2_097_152
_MAX_SPAN_MS = 15 * MINUTE_MS
_MAX_SNAPSHOT_AGE_MS = MINUTE_MS
_OPENING_KEYS = (
    "opening_position",
    "opening_brackets",
    "opening_config",
    "opening_asset_mode",
    "opening_position_mode",
)
_CURRENT_KEYS = (
    "current_position",
    "current_brackets",
    "current_config",
    "current_asset_mode",
    "current_position_mode",
    "marks",
)
_KEYS = set(_OPENING_KEYS + _CURRENT_KEYS)
_SECRET_KEYS = {
    "apikey",
    "apisecret",
    "secret",
    "signature",
    "authorization",
    "cookie",
    "token",
    "accesstoken",
    "privatekey",
    "xmbxapikey",
    "password",
    "passphrase",
}
_POSITION_FIELDS = {
    "symbol",
    "positionSide",
    "positionAmt",
    "entryPrice",
    "markPrice",
    "unRealizedProfit",
    "isolatedWallet",
    "marginAsset",
    "updateTime",
}


@dataclass(frozen=True)
class OfflineRiskAssessment:
    environment: str
    bundle_sha256: str
    source_hashes: dict[str, str]
    risk: IsolatedShortRisk
    two_point_snapshot_consistent: bool = True
    source_authenticated: bool = False
    continuous_account_state_verified: bool = False
    mark_price_stream_authenticated: bool = False
    execution_authorized: bool = False


def _no_secrets(obj: object) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise ValueError("invalid source field")
            if key.lower().replace("-", "").replace("_", "") in _SECRET_KEYS:
                raise ValueError("risk bundle contains credential or secret field")
            _no_secrets(value)
    elif isinstance(obj, list):
        for value in obj:
            _no_secrets(value)


def _ms(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"invalid {field} capture millisecond timestamp")
    return value


def _snapshot_position(row: object, symbol: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if not isinstance(row, dict) or not set(row) >= _POSITION_FIELDS:
        raise ValueError("incomplete positionRisk V3 snapshot")
    if row["symbol"] != symbol or row["positionSide"] != "BOTH" or row["marginAsset"] != "USDT":
        raise ValueError("position symbol/side/collateral mismatch")
    quantity = _decimal(row["positionAmt"], "positionAmt")
    if quantity >= 0:
        raise ValueError("positionRisk snapshot does not contain a short")
    price = _decimal(row["entryPrice"], "entryPrice", positive=True)
    wallet = _decimal(row["isolatedWallet"], "isolatedWallet", positive=True)
    mark = _decimal(row["markPrice"], "position markPrice", positive=True)
    unrealized = _decimal(row["unRealizedProfit"], "unRealizedProfit")
    if type(row["updateTime"]) is not int or row["updateTime"] < 0:
        raise ValueError("positionRisk updateTime must be integer milliseconds")
    # This is a consistency check, not an authenticated account-equity claim.
    expected = (-quantity) * (price - mark)
    if abs(unrealized - expected) > max(Decimal("0.01"), (-quantity) * mark * Decimal("0.000001")):
        raise ValueError("positionRisk unrealized PnL disagrees with short quantity and mark")
    return quantity, price, wallet, mark


def _config(row: object, symbol: str) -> int:
    if not isinstance(row, dict) or not {"symbol", "marginType", "leverage", "isAutoAddMargin"} <= set(row):
        raise ValueError("incomplete symbolConfig snapshot")
    if row["symbol"] != symbol or row["marginType"] != "ISOLATED" or row["isAutoAddMargin"] is not False:
        raise ValueError("unsupported margin mode or auto-add isolated collateral")
    level = row["leverage"]
    if type(level) is not int or not 1 <= level <= 1000:
        raise ValueError("invalid symbolConfig leverage")
    return level


def validate_offline_risk_bundle(bundle: object, *, expected_sha256: str) -> OfflineRiskAssessment:
    """Fail closed on drift, stale snapshots and inconsistent offline evidence."""
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError("invalid externally pinned risk bundle digest")
    if not isinstance(bundle, dict):
        raise ValueError("risk evidence bundle must be a JSON object")
    _no_secrets(bundle)
    if source_sha256(bundle) != expected_sha256:
        raise ValueError("risk evidence bundle SHA-256 mismatch")
    required = {
        "version",
        "kind",
        "environment",
        "symbol",
        "entry_minute_start_ms",
        "assessment_ms",
        "captures",
        "sources",
        "costs",
    }
    if set(bundle) != required or type(bundle["version"]) is not int:
        raise ValueError("unsupported risk bundle schema")
    if bundle["version"] != 1 or bundle["kind"] != KIND:
        raise ValueError("unsupported risk bundle schema")
    if not isinstance(bundle["environment"], str) or bundle["environment"] not in {"demo", "mainnet"}:
        raise ValueError("unsupported or unknown risk snapshot environment")
    symbol = bundle["symbol"]
    captures, sources, costs = bundle["captures"], bundle["sources"], bundle["costs"]
    if not isinstance(captures, dict) or set(captures) != _KEYS:
        raise ValueError("missing account or market capture timestamps")
    if not isinstance(sources, dict) or set(sources) != _KEYS:
        raise ValueError("incomplete account/mark evidence sources")
    if not isinstance(costs, dict) or set(costs) != {"funding_debit_reserve", "close_fee_bps", "buffer_bps"}:
        raise ValueError("incomplete funding and closing-cost assumptions")
    start = _ms(bundle["entry_minute_start_ms"], "entry")
    finish = _ms(bundle["assessment_ms"], "assessment")
    if start % MINUTE_MS or not start + MINUTE_MS < finish <= start + _MAX_SPAN_MS:
        raise ValueError("risk bundle requires a short, aligned 1m-to-15m position window")
    for key, value in captures.items():
        timestamp = _ms(value, key)
        if key in _OPENING_KEYS:
            if not start <= timestamp < start + MINUTE_MS:
                raise ValueError("opening account capture must occur in entry minute")
        elif not finish - _MAX_SNAPSHOT_AGE_MS <= timestamp <= finish:
            raise ValueError("current account/mark capture stale or after assessment")
    # Two polls are not a transactional account stream; fail if any
    # explicitly observed wallet, quantity, collateral or mode changed.
    for kind in ("asset_mode", "position_mode"):
        opening = sources[f"opening_{kind}"]
        current = sources[f"current_{kind}"]
        field = "multiAssetsMargin" if kind == "asset_mode" else "dualSidePosition"
        if not isinstance(opening, dict) or not isinstance(current, dict):
            raise ValueError("missing account-mode endpoint response")
        if opening.get(field) is not False or current.get(field) is not False:
            raise ValueError("unsupported hedge or multi-assets mode")
    opening = _snapshot_position(sources["opening_position"], symbol)
    current = _snapshot_position(sources["current_position"], symbol)
    if opening[:3] != current[:3]:
        raise ValueError("position quantity, entry, or isolated wallet changed between snapshots")
    first_row, last_row = sources["opening_position"], sources["current_position"]
    if first_row["updateTime"] != last_row["updateTime"] or first_row["updateTime"] > captures["opening_position"]:
        raise ValueError("position update time changed or follows opening capture")
    first_leverage = _config(sources["opening_config"], symbol)
    if _config(sources["current_config"], symbol) != first_leverage:
        raise ValueError("symbol leverage changed between snapshots")
    if sources["opening_brackets"] != sources["current_brackets"]:
        raise ValueError("account notional brackets differ between opening and assessment")
    marks = sources["marks"]
    if not isinstance(marks, dict) or set(marks) != {"endpoint", "symbol", "candles"}:
        raise ValueError("mark-price envelope schema mismatch")
    if marks["endpoint"] != "/fapi/v1/markPriceKlines" or marks["symbol"] != symbol:
        raise ValueError("mark-price endpoint or symbol mismatch")
    bars = marks["candles"]
    if (
        not isinstance(bars, list)
        or not bars
        or not isinstance(bars[0], list)
        or not isinstance(bars[-1], list)
        or len(bars[0]) != 12
        or len(bars[-1]) != 12
    ):
        raise ValueError("missing or incomplete mark-price minute candles")
    if type(bars[-1][6]) is not int or captures["marks"] <= bars[-1][6]:
        raise ValueError("mark-price candles were captured before their final completed close")
    first_low, first_high = _decimal(bars[0][3], "opening mark low"), _decimal(bars[0][2], "opening mark high")
    last_low, last_high = _decimal(bars[-1][3], "current mark low"), _decimal(bars[-1][2], "current mark high")
    if not first_low <= opening[3] <= first_high or not last_low <= current[3] <= last_high:
        raise ValueError("positionRisk mark price inconsistent with observed mark candle range")
    hashes = {key: source_sha256(data) for key, data in sources.items()}
    position = IsolatedShort(
        symbol=symbol,
        quantity=-opening[0],
        entry_fill_price=opening[1],
        isolated_wallet_after_entry=opening[2],
        leverage=first_leverage,
        funding_debit_reserve=_decimal(costs["funding_debit_reserve"], "funding debit reserve"),
        close_fee_bps=_decimal(costs["close_fee_bps"], "close fee bps"),
        buffer_bps=_decimal(costs["buffer_bps"], "risk buffer bps"),
    )
    risk = assess_isolated_short(
        sources["current_brackets"],
        position,
        MarkEvidence(
            raw_candles=bars,
            source_sha256=hashes["marks"],
            entry_minute_start_ms=start,
            bracket_capture_ms=captures["current_brackets"],
            assessment_ms=finish,
            symbol=symbol,
            endpoint=marks["endpoint"],
        ),
        expected_bracket_sha256=hashes["current_brackets"],
        expected_mark_sha256=hashes["marks"],
    )
    return OfflineRiskAssessment(
        environment=bundle["environment"],
        bundle_sha256=expected_sha256,
        source_hashes=hashes,
        risk=risk,
    )


def load_offline_risk_bundle(path: Path, *, expected_sha256: str) -> OfflineRiskAssessment:
    """Read one bounded regular JSON file without following a symbolic link."""
    target = Path(path)
    if target.is_symlink():
        raise ValueError("risk evidence bundle must not be a symlink")
    fd = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_BYTES:
            raise ValueError("risk evidence must be a bounded regular file")
        raw = os.read(fd, _MAX_BYTES + 1)
    finally:
        os.close(fd)
    if not 0 < len(raw) <= _MAX_BYTES:
        raise ValueError("risk evidence missing or oversized")
    try:
        bundle = parse_json(raw.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ValueError("risk evidence contains malformed, duplicate or nonfinite JSON") from exc
    return validate_offline_risk_bundle(bundle, expected_sha256=expected_sha256)
