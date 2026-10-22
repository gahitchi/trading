"""Read-only multi-asset portfolio audit from a strictly typed JSON manifest."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lab.multiasset import (
    AssetClass,
    FXQuote,
    FXRates,
    Holding,
    Instrument,
    MarketPrice,
    MultiAssetError,
    cash_after_spot_trade,
    portfolio_liquidation_value,
)


def _unique(pairs: list[tuple]) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            raise MultiAssetError(f"duplicate JSON field: {key}")
        out[key] = value
    return out


def _amount(raw, label: str) -> Decimal:
    if not isinstance(raw, str):
        raise MultiAssetError(f"{label} must be a decimal encoded as a JSON string")
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise MultiAssetError(f"{label} is not a decimal") from exc
    if not parsed.is_finite():
        raise MultiAssetError(f"{label} must be finite")
    return parsed


def _utc(raw: str) -> datetime:
    if not isinstance(raw, str):
        raise MultiAssetError("timestamp must be an ISO-8601 string")
    try:
        result = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise MultiAssetError(f"invalid ISO timestamp: {raw!r}") from exc
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise MultiAssetError("timestamps must be UTC-aware")
    return result


def load_manifest(path: str | Path) -> dict:
    """Refuse ambiguous/oversized JSON. Does not access any brokerage account."""
    file = Path(path)
    try:
        if file.stat().st_size > 2_000_000:
            raise MultiAssetError("portfolio manifest exceeds 2MB limit")
        parsed = json.loads(file.read_text(encoding="utf-8"), object_pairs_hook=_unique)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MultiAssetError(f"cannot read portfolio manifest: {exc}") from exc
    if not isinstance(parsed, dict):
        raise MultiAssetError("portfolio manifest must be a JSON object")
    return parsed


def analyze_manifest(manifest: dict) -> dict:
    """Validate account-free capital-market and FX marks; never authorize orders."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise MultiAssetError("unsupported portfolio manifest schema")
    now = _utc(manifest["as_of"])
    seconds = manifest["max_age_seconds"]
    if type(seconds) is not int or not 0 < seconds <= 604800:
        raise MultiAssetError("max_age_seconds must be a positive integer <=604800")
    age = timedelta(seconds=seconds)
    currency = manifest["reporting_currency"]
    registry = {}
    for row in manifest["instruments"]:
        asset = Instrument(
            row["instrument_id"],
            AssetClass(row["asset_class"]),
            row["quote_currency"],
            _amount(row["quantity_step"], "quantity_step"),
            _amount(row["price_tick"], "price_tick"),
            _amount(row["min_quantity"], "min_quantity"),
            row.get("base_currency"),
        )
        if asset.instrument_id in registry:
            raise MultiAssetError("duplicate instrument identifier")
        registry[asset.instrument_id] = asset
    fx_quotes = [
        FXQuote(
            row["base"],
            row["quote"],
            _amount(row["bid"], "FX bid"),
            _amount(row["ask"], "FX ask"),
            _utc(row["timestamp"]),
            row["source"],
        )
        for row in manifest["fx_quotes"]
    ]
    rates = FXRates(fx_quotes)
    prices = {}
    for row in manifest["prices"]:
        price = MarketPrice(
            row["instrument_id"],
            _amount(row["bid"], "market bid"),
            _amount(row["ask"], "market ask"),
            _utc(row["timestamp"]),
            _amount(row.get("accrued_interest_per_100", "0"), "accrued interest"),
        )
        asset = registry.get(price.instrument_id)
        if asset is None or price.instrument_id in prices:
            raise MultiAssetError("unknown/duplicate market instrument")
        asset.validate_price(price.bid)
        asset.validate_price(price.ask)
        if not timedelta(0) <= now - price.timestamp <= age:
            raise MultiAssetError("stale or future market price")
        prices[price.instrument_id] = price
    fx_index = {(quote.base, quote.quote): quote for quote in fx_quotes}
    for asset in registry.values():
        if asset.asset_class == AssetClass.FX_SPOT and asset.instrument_id in prices:
            quote = fx_index.get((asset.base_currency, asset.quote_currency))
            mark = prices[asset.instrument_id]
            if quote is None or (mark.bid, mark.ask, mark.timestamp) != (quote.bid, quote.ask, quote.timestamp):
                raise MultiAssetError("spot FX executable mark disagrees with currency conversion feed")
    positions = [
        Holding(row["instrument_id"], _amount(row["quantity"], "holding quantity"))
        for row in manifest["holdings"]
    ]
    cash = {ccy: _amount(value, f"cash {ccy}") for ccy, value in manifest["cash"].items()}
    before = portfolio_liquidation_value(
        cash=cash,
        holdings=positions,
        instruments=registry,
        prices=prices,
        fx=rates,
        reporting_currency=currency,
        as_of=now,
        max_age=age,
    )
    preview = []
    updated = dict(cash)
    for action in manifest.get("spot_previews", []):
        asset = registry.get(action["instrument_id"])
        price = prices.get(action["instrument_id"])
        if asset is None or price is None:
            raise MultiAssetError("unknown spot FX instrument/mark for preview")
        updated = cash_after_spot_trade(
            updated, asset,
            side=action["side"],
            base_quantity=_amount(action["base_quantity"], "base_quantity"),
            market=price,
            commission_quote=_amount(action["commission_quote"], "commission_quote"),
            as_of=now,
            max_age=age,
        )
        preview.append(
            {
                "instrument_id": asset.instrument_id,
                "side": action["side"],
                "base_quantity": action["base_quantity"],
                "cash_after": {k: str(v) for k, v in sorted(updated.items())},
            }
        )
    after = portfolio_liquidation_value(
        cash=updated,
        holdings=positions,
        instruments=registry,
        prices=prices,
        fx=rates,
        reporting_currency=currency,
        as_of=now,
        max_age=age,
    )
    stress_before = None
    stress_after = None
    if "scenarios" in manifest or "factor_exposures" in manifest or "stress_policy" in manifest:
        from lab.stress_reasoning import evaluate_stress

        policy = manifest.get("stress_policy", {})
        if not isinstance(policy, dict):
            raise MultiAssetError("stress_policy must be a mapping")
        params = dict(
            holdings=positions,
            instruments=registry,
            prices=prices,
            fx_quotes=fx_quotes,
            reporting_currency=currency,
            as_of=now,
            max_age=age,
            factor_exposures=manifest.get("factor_exposures", []),
            scenarios=manifest.get("scenarios", []),
            max_loss_fraction=policy.get("max_loss_fraction"),
            max_loss_amount=policy.get("max_loss_amount"),
        )
        stress_before = evaluate_stress(cash=cash, **params)
        stress_after = evaluate_stress(cash=updated, **params) if preview else stress_before
    source = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False)
    fingerprint = hashlib.sha256(source.encode()).hexdigest()
    return {
        "schema_version": 1,
        "qualification": "RESEARCH_ONLY_NOT_FOR_TRADING",
        "analysis_id": fingerprint,
        "as_of": now.isoformat(),
        "reporting_currency": currency,
        "asset_classes": sorted({asset.asset_class.value for asset in registry.values()}),
        "liquidation_mark_before": str(before),
        "liquidation_mark_after_spot_previews": str(after),
        "spot_previews": preview,
        "stress_before": stress_before,
        "stress_after_spot_previews": stress_after,
        "cash_after_spot_previews": {k: str(v) for k, v in sorted(updated.items())},
        "warnings": [
            "Synthetic/offline cash-account scenario only: never sends an order",
            "Bid-side valuation approximates liquidation; spread/depth/market impact and taxes are excluded",
            "FX conversion crosses are indicative; simultaneous executable liquidity is not established",
            "Spot FX previews omit settlement date, interest, counterparty, margin and broker fees beyond explicit commission",
            "Bond values require validated accrued interest, settlement, coupon schedule and corporate-action records",
            "Broker-supported instrument qualification and per-market risk controls are required before any execution",
            "Stress factor sensitivities are uncalibrated user hypotheses, never probability forecasts or trading authorization",
        ],
    }
