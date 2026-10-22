"""Counterfactual, auditable multi-asset stress reasoning. No order permissions.

A factor model is an explicit *scenario assumption* rather than a trained
forecast. This engine computes reproducible component-level loss attribution,
checks internal arithmetic and refuses ambiguous/contradictory factor paths.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from lab.multiasset import (
    AssetClass,
    FXQuote,
    FXRates,
    Holding,
    Instrument,
    MarketPrice,
    MultiAssetError,
    portfolio_liquidation_value,
)

MAX_SCENARIOS = 64
MAX_FACTORS = 32
MAX_EXPOSURES = 512


def _fraction(raw, name: str, *, lower: Decimal = Decimal("-0.9999"), upper: Decimal = Decimal("10")) -> Decimal:
    if not isinstance(raw, str):
        raise MultiAssetError(f"{name}: scenario values must be decimal strings")
    try:
        value = Decimal(raw)
    except Exception as exc:
        raise MultiAssetError(f"{name}: invalid decimal") from exc
    if not value.is_finite() or not lower <= value <= upper:
        raise MultiAssetError(f"{name}: outside supported bounded shock range")
    return value


def _components(
    *,
    cash: dict[str, Decimal],
    holdings: list[Holding],
    instruments: dict[str, Instrument],
    prices: dict[str, MarketPrice],
    fx: FXRates,
    reporting_currency: str,
    now: datetime,
    age: timedelta,
) -> dict[str, Decimal]:
    result = {}
    for ccy, balance in sorted(cash.items()):
        key = f"CASH:{ccy}"
        result[key] = fx.convert(balance, ccy, reporting_currency, as_of=now, max_age=age)
    for position in holdings:
        key = f"POSITION:{position.instrument_id}"
        if key in result:
            raise MultiAssetError(f"duplicate holding: {position.instrument_id}")
        if position.quantity == 0:
            result[key] = Decimal(0)
            continue
        inst = instruments[position.instrument_id]
        px = prices[position.instrument_id]
        divisor = Decimal("100") if inst.asset_class == AssetClass.BOND else Decimal(1)
        gross = position.quantity * (px.bid + (px.accrued_interest_per_100 if divisor == 100 else 0)) / divisor
        result[key] = fx.convert(gross, inst.quote_currency, reporting_currency, as_of=now, max_age=age)
    return result


def _validate_factor_exposures(exposures: list[dict], instruments: dict[str, Instrument], quotes: list[FXQuote]):
    if not isinstance(exposures, list) or len(exposures) > MAX_EXPOSURES:
        raise MultiAssetError("invalid factor exposure count")
    allowed = {i for i, inst in instruments.items() if inst.asset_class != AssetClass.FX_SPOT}
    allowed.update(f"FX:{q.base}/{q.quote}" for q in quotes)
    mapping: dict[str, dict[str, Decimal]] = {}
    for row in exposures:
        if not isinstance(row, dict):
            raise MultiAssetError("malformed factor exposure")
        iid, name = row["instrument_id"], row["factor"]
        if iid not in allowed or not isinstance(name, str) or not name.strip():
            raise MultiAssetError(f"unrecognized instrument/factor in exposure: {iid}")
        if name in mapping.get(iid, {}):
            raise MultiAssetError(f"duplicate factor exposure for {iid}: {name}")
        if not isinstance(row.get("source"), str) or not row["source"].strip():
            raise MultiAssetError("factor exposure needs an explicit assumption source")
        slope = _fraction(row["loading"], f"{iid}/{name}", lower=Decimal("-100"), upper=Decimal("100"))
        mapping.setdefault(iid, {})[name] = slope
    return mapping


def _scenario_shock(key: str, factor_values: dict[str, Decimal], exposure_map: dict, direct: dict) -> tuple[Decimal, list]:
    exposure = exposure_map.get(key, {})
    if key in direct and any(factor_values.get(f, 0) != 0 for f in exposure):
        raise MultiAssetError(f"{key}: both direct and factor-derived shocks, ambiguous precedence")
    breakdown = [
        {"factor": name, "shock": str(factor_values[name]), "loading": str(load), "contribution": str(factor_values[name] * load)}
        for name, load in sorted(exposure.items()) if name in factor_values
    ]
    net = sum((factor_values[name] * load for name, load in exposure.items() if name in factor_values), Decimal(0))
    if key in direct:
        net = direct[key]
        breakdown.append({"factor": "EXPLICIT_DIRECT", "contribution": str(net)})
    if not Decimal("-0.9999") <= net <= Decimal("10"):
        raise MultiAssetError(f"{key}: combined price shock is impossible or out of bounded range")
    return net, breakdown


def evaluate_stress(
    *,
    cash: dict[str, Decimal],
    holdings: list[Holding],
    instruments: dict[str, Instrument],
    prices: dict[str, MarketPrice],
    fx_quotes: list[FXQuote],
    reporting_currency: str,
    as_of: datetime,
    max_age: timedelta,
    factor_exposures: list[dict],
    scenarios: list[dict],
    max_loss_fraction: str | None = None,
    max_loss_amount: str | None = None,
) -> dict:
    """Reprice each declared scenario, provide additive P&L attribution and risk breaches.

    No optimization, portfolio changes or broker calls; missing-factor and
    duplicate-shock conditions abort the entire research report.
    """
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= MAX_SCENARIOS:
        raise MultiAssetError("stress requires 1-64 explicit scenarios")
    seen = set()
    fx = FXRates(fx_quotes)
    before = portfolio_liquidation_value(
        cash=cash, holdings=holdings, instruments=instruments, prices=prices,
        fx=fx, reporting_currency=reporting_currency, as_of=as_of, max_age=max_age,
    )
    if before <= 0:
        raise MultiAssetError("scenario percentage risk requires strictly positive baseline portfolio value")
    prev_components = _components(
        cash=cash, holdings=holdings, instruments=instruments, prices=prices, fx=fx,
        reporting_currency=reporting_currency, now=as_of, age=max_age,
    )
    if sum(prev_components.values(), Decimal(0)) != before:
        raise MultiAssetError("internal baseline component accounting mismatch")
    factor_map = _validate_factor_exposures(factor_exposures, instruments, fx_quotes)
    available = {x for group in factor_map.values() for x in group}
    universe = set(instruments)
    fx_set = {f"FX:{q.base}/{q.quote}" for q in fx_quotes}
    results = []
    limit_fraction = None if max_loss_fraction is None else _fraction(
        max_loss_fraction, "max_loss_fraction", lower=Decimal("0"), upper=Decimal("1")
    )
    limit_amount = None if max_loss_amount is None else _fraction(
        max_loss_amount, "max_loss_amount", lower=Decimal("0"), upper=Decimal("1000000000000000000")
    )
    for row in scenarios:
        if not isinstance(row, dict):
            raise MultiAssetError("scenario must be an object")
        name = row.get("name")
        if not isinstance(name, str) or not name.strip() or name in seen:
            raise MultiAssetError("empty or duplicate scenario name")
        seen.add(name)
        factors_raw = row.get("factors", {})
        direct_raw = row.get("direct_shocks", {})
        if not isinstance(factors_raw, dict) or not isinstance(direct_raw, dict):
            raise MultiAssetError("scenario shocks must be mappings")
        if len(factors_raw) > MAX_FACTORS:
            raise MultiAssetError("too many scenario factors")
        factors = {key: _fraction(raw, key) for key, raw in factors_raw.items()}
        if set(factors) - available:
            raise MultiAssetError(f"scenario {name}: factor lacks sensitivity evidence: {sorted(set(factors) - available)}")
        direct = {key: _fraction(raw, key) for key, raw in direct_raw.items()}
        if set(direct) - (universe | fx_set):
            raise MultiAssetError(f"scenario {name}: unknown shock targets: {sorted(set(direct) - (universe | fx_set))}")
        if any(key in instruments and instruments[key].asset_class == AssetClass.FX_SPOT for key in direct):
            raise MultiAssetError("FX spot shocks must target FX:BASE/QUOTE, never a duplicated security")
        projected_prices = {}
        projected_fx = []
        details = {}
        for ticker, market in sorted(prices.items()):
            inst = instruments.get(ticker)
            if inst is None:
                raise MultiAssetError(f"no instrument metadata for {ticker}")
            if inst.asset_class == AssetClass.FX_SPOT:
                continue
            shock, contrib = _scenario_shock(ticker, factors, factor_map, direct)
            factor = Decimal(1) + shock
            bid = (market.bid * factor / inst.price_tick).to_integral_value(rounding=ROUND_FLOOR) * inst.price_tick
            ask = (market.ask * factor / inst.price_tick).to_integral_value(rounding=ROUND_CEILING) * inst.price_tick
            projected_prices[ticker] = MarketPrice(
                ticker, bid, ask, market.timestamp,
                market.accrued_interest_per_100 if inst.asset_class == AssetClass.BOND else Decimal(0),
            )
            details[ticker] = {"net_return_shock": str(shock), "factor_contributions": contrib}
        for quote in sorted(fx_quotes, key=lambda q: (q.base, q.quote)):
            key = f"FX:{quote.base}/{quote.quote}"
            shock, contrib = _scenario_shock(key, factors, factor_map, direct)
            factor = Decimal(1) + shock
            projected_fx.append(
                FXQuote(quote.base, quote.quote, quote.bid * factor, quote.ask * factor, quote.timestamp, quote.source)
            )
            details[key] = {"net_return_shock": str(shock), "factor_contributions": contrib}
        changed_fx = FXRates(projected_fx)
        after = portfolio_liquidation_value(
            cash=cash, holdings=holdings, instruments=instruments, prices=projected_prices,
            fx=changed_fx, reporting_currency=reporting_currency, as_of=as_of, max_age=max_age,
        )
        after_components = _components(
            cash=cash, holdings=holdings, instruments=instruments, prices=projected_prices,
            fx=changed_fx, reporting_currency=reporting_currency, now=as_of, age=max_age,
        )
        delta_by_component = {
            key: after_components[key] - prev_components[key] for key in sorted(prev_components)
        }
        pnl = after - before
        if sum(delta_by_component.values(), Decimal(0)) != pnl:
            raise MultiAssetError(f"scenario {name}: accounting attribution mismatch")
        loss = before - after
        frac = loss / before
        breached = []
        if limit_fraction is not None and frac > limit_fraction:
            breached.append("MAX_STRESS_LOSS_FRACTION")
        if limit_amount is not None and loss > limit_amount:
            breached.append("MAX_STRESS_LOSS_AMOUNT")
        result = {
            "name": name,
            "scenario_value": str(after),
            "pnl": str(pnl),
            "loss_fraction": str(frac),
            "breached_limits": breached,
            "component_pnl": {k: str(v) for k, v in delta_by_component.items()},
            "dominant_loss_component": min(delta_by_component, key=delta_by_component.get) if delta_by_component else None,
            "factor_trace": details,
        }
        results.append((loss, result))
    ordered = [r for _, r in sorted(results, key=lambda pair: (-pair[0], pair[1]["name"]))]
    max_loss = max(loss for loss, _ in results)
    failures = [r["name"] for r in ordered if r["breached_limits"]]
    payload = {
        "schema_version": 1,
        "baseline_value": str(before),
        "reporting_currency": reporting_currency,
        "risk_status": "LIMIT_BREACH_IN_ASSUMED_SCENARIOS" if failures else "NO_BREACH_IN_DECLARED_SCENARIOS_ONLY",
        "worst_case_name": ordered[0]["name"],
        "worst_case_loss": str(max_loss),
        "failed_scenarios": failures,
        "assumptions": {"factor_exposures": factor_exposures, "scenarios": scenarios},
        "stress_limit_loss_fraction": max_loss_fraction,
        "stress_limit_loss_amount": max_loss_amount,
        "ranked_scenarios": ordered,
        "warnings": [
            "Stress shocks and sensitivities are user assumptions, not predicted probabilities",
            "Linear factor sensitivities can fail nonlinearly, especially for bonds and crisis liquidity",
            "Bid/ask conversion is a conservative proxy, not a guaranteed executable multi-leg FX path",
            "Only declared scenarios were tested; a non-breach is NOT a safety or execution approval",
        ],
    }
    payload["analysis_id"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return payload
