"""Separate, fixed leg budgets on a hypothetical carry path; never exchange liquidation evidence."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from lab.carry_study import CarrySettings

MODEL = "separate-leg-close-collateral-scenario-v1"
MARK_MODEL = "separate-leg-mark-ohlc-collateral-scenario-v1"
ASSUMPTIONS = ("collateral_long", "collateral_short", "maintenance_long", "maintenance_short")
LIMITATION = (
    "Hypothetical hourly close/funding proxies and fixed maintenance fractions, not historical venue rules. "
    "No initial-margin admission, mark/intrahour evidence, tiers, close-fee reserve, liquidation execution/losses, "
    "transfers or financing. The unchanged strategy path after a breach is counterfactual, not funded returns."
)
MARK_LIMITATION = (
    "Historical hourly mark closes and adverse OHLC/debit envelopes with explicit fixed maintenance fractions; "
    "not historical venue margin rules. Funding cash flows retain trade-close proxies. Intrahour ordering is unknown; "
    "interval envelope breaches are not observed liquidation events. No initial-margin admission, tiers, close-fee "
    "reserve, liquidation execution/losses, transfers or financing. Post-breach paths are counterfactual, not funded returns."
)


def model_of(settings):
    return MARK_MODEL if settings is not None and settings.collateral_valuation == "mark-ohlc" else MODEL


def _validate_marks(frame, settings):
    from lab import carry_marks

    if frame.attrs.get("mark_model") != carry_marks.MODEL:
        raise ValueError("mark collateral requires identified historical mark OHLC")
    for leg in ("short",) if settings.mode == "cash" else ("long", "short"):
        columns = [f"mark_{c}_{leg}" for c in carry_marks.COLUMNS]
        if not {*columns, f"funding_debit_{leg}"} <= set(frame.columns):
            raise ValueError(f"mark collateral requires individual OHLC and funding debit evidence: {leg}")
        qty = frame[f"qty_{leg}"]
        previous = qty.shift(1, fill_value=0.0)
        active = (qty != 0) | (previous != 0)
        subset = frame.loc[active, columns].copy()
        subset.columns = list(carry_marks.COLUMNS)
        try:
            carry_marks._validate(subset)
        except ValueError as e:
            raise ValueError(f"missing or invalid historical mark OHLC for exposed {leg} leg: {e}") from e
        if ((previous != 0) & (frame.index.to_series().diff() != pd.Timedelta(hours=1))).any():
            raise ValueError("mark collateral requires an uninterrupted hourly grid while exposed")
        debit = frame[f"funding_debit_{leg}"]
        if not np.isfinite(debit).all() or (debit > 0).any():
            raise ValueError("mark collateral requires finite nonpositive funding debit evidence")


def collateral_path(frame: pd.DataFrame, settings: CarrySettings) -> pd.DataFrame:
    """Pre-decision settlement and post-trade checks; retain losses across exits/reentries, never transfer gains.

    Equity is initial deposit plus each leg's cumulative marked P&L. Spot inventory ties up its full value;
    perpetual maintenance is the explicit scenario fraction times absolute notional. At a perpetual threshold,
    equality breaches; zero unused spot cash or a zero flat balance does not. No liquidation is executed.
    """
    from lab.carry_study import FUNDING_VALUATION, LEG_MODEL

    if settings.collateral_long is None:
        raise ValueError("collateral scenario requires explicit assumptions")
    required = {
        f"{name}_{leg}" for name in ("qty", "notional", "basis", "funding", "cost") for leg in ("long", "short")
    }
    if (
        frame.empty
        or frame.attrs.get("leg_model") != LEG_MODEL
        or frame.attrs.get("funding_valuation") != FUNDING_VALUATION
        or frame.attrs.get("mode") != settings.mode
        or not required <= set(frame.columns)
    ):
        raise ValueError("collateral scenario requires an identified leg ledger")
    if (
        not isinstance(frame.index, pd.DatetimeIndex)
        or frame.index.tz is None
        or not frame.index.is_unique
        or not frame.index.is_monotonic_increasing
        or not np.isfinite(frame[sorted(required)]).all().all()
        or (frame[["cost_long", "cost_short"]] < 0).any().any()
    ):
        raise ValueError("invalid collateral ledger observations")
    mark_valuation = settings.collateral_valuation == "mark-ohlc"
    if mark_valuation:
        _validate_marks(frame, settings)
    stages = {}
    for stage in ("before_decision", "after_trade"):
        out = pd.DataFrame(index=frame.index)
        for leg in ("long", "short"):
            cost = frame[f"cost_{leg}"]
            change = frame[f"basis_{leg}"] + frame[f"funding_{leg}"] - cost
            pnl = change.cumsum()
            qty = frame[f"qty_{leg}"]
            notional = frame[f"notional_{leg}"]
            if stage == "before_decision":
                pnl = pnl + cost  # current settlement belongs to the OLD position, before any trade fees
                qty = qty.shift(1, fill_value=0.0)
                notional = notional.shift(1, fill_value=0.0) + frame[f"basis_{leg}"]
            spot = settings.mode == "cash" and leg == "long"
            if mark_valuation and not spot:
                marked = (qty * frame[f"mark_close_{leg}"]).where(qty != 0, 0.0)
                pnl = pnl + marked - notional
                notional = marked
            requirement = notional.abs() * (1.0 if spot else getattr(settings, f"maintenance_{leg}"))
            equity = getattr(settings, f"collateral_{leg}") + pnl
            headroom = equity - requirement
            out[f"pnl_{leg}"] = pnl
            out[f"equity_{leg}"] = equity
            out[f"requirement_{leg}"] = requirement
            out[f"headroom_{leg}"] = headroom
            out[f"breached_{leg}"] = (headroom < 0) | ((headroom == 0) & (qty != 0) & (not spot))
        stages[stage] = out
    # Interleave the stages in time, so an exit/reversal cannot hide an already exhausted old position.
    result = (
        pd.concat(stages, names=["stage", "time"])
        .reorder_levels(["time", "stage"])
        .sort_index(level="time", sort_remaining=False)
    )
    if not np.isfinite(result).all().all():
        raise ValueError("nonfinite collateral scenario; evaluation is unavailable")
    return result


def mark_interval_path(frame: pd.DataFrame, settings: CarrySettings, boundary_path=None) -> pd.DataFrame:
    """Adverse mark plus all proxy funding debits before credits; an envelope, not an observed event sequence."""
    if settings.collateral_valuation != "mark-ohlc":
        raise ValueError("interval envelopes require mark-ohlc valuation")
    boundaries = collateral_path(frame, settings) if boundary_path is None else boundary_path
    before = boundaries.xs("before_decision", level="stage")
    out = pd.DataFrame(index=frame.index)
    for leg in ("short",) if settings.mode == "cash" else ("long", "short"):
        qty = frame[f"qty_{leg}"].shift(1, fill_value=0.0)
        exposed = qty != 0
        adverse = frame[f"mark_low_{leg}"].where(qty > 0, frame[f"mark_high_{leg}"])
        move = (qty * (adverse - frame[f"mark_close_{leg}"])).where(exposed, 0.0)
        pnl = before[f"pnl_{leg}"] - frame[f"funding_{leg}"] + frame[f"funding_debit_{leg}"] + move
        equity = getattr(settings, f"collateral_{leg}") + pnl
        requirement = (qty * adverse).abs().where(exposed, 0.0) * getattr(settings, f"maintenance_{leg}")
        out[f"pnl_{leg}"] = pnl
        out[f"equity_{leg}"] = equity
        out[f"requirement_{leg}"] = requirement
        out[f"headroom_{leg}"] = equity - requirement
        out[f"exposed_{leg}"] = exposed
        out[f"envelope_breach_{leg}"] = exposed & (equity <= requirement)
    if not np.isfinite(out).all().all():
        raise ValueError("nonfinite mark interval envelope; evaluation is unavailable")
    return out


def collateral_report(pnl: dict[str, pd.DataFrame], settings: CarrySettings | None = None) -> dict:
    """Ex-post budget boundaries per symbol, not a pooled portfolio or a prospective capital recommendation."""
    configured = settings is not None and settings.collateral_long is not None
    report = {
        "model": model_of(settings),
        "valuation": settings.collateral_valuation if settings is not None else "trade-close",
        "mode": settings.mode if settings is not None else None,
        "configured": configured,
        "available": False,
        "qualified": False,
        "unit": "quote-currency units per symbol's one-unit initial notional per leg; no pooling or top-K scaling",
        "limitation": MARK_LIMITATION if model_of(settings) == MARK_MODEL else LIMITATION,
        "assumptions": {k: asdict(settings)[k] for k in ASSUMPTIONS} if configured else None,
        "per_symbol": {},
    }
    if not configured:
        report["reason"] = "no explicit collateral/maintenance scenario supplied"
        return report
    for symbol, frame in pnl.items():
        path = collateral_path(frame, settings)
        intervals = mark_interval_path(frame, settings, path) if settings.collateral_valuation == "mark-ohlc" else None
        detail = {}
        for leg in ("long", "short"):
            breached = path[path[f"breached_{leg}"]]
            first = None
            if not breached.empty:
                (time, stage), row = next(iter(breached.iterrows()))
                first = {
                    "time": time.isoformat(),
                    "stage": stage,
                    **{k: float(row[f"{k}_{leg}"]) for k in ("equity", "requirement", "headroom", "pnl")},
                }
            detail[leg] = {
                "market": "spot" if settings.mode == "cash" and leg == "long" else "perpetual",
                "initial_collateral": getattr(settings, f"collateral_{leg}"),
                "first_breach": first,
                "minimum_headroom": float(path[f"headroom_{leg}"].min()),
                "terminal_hypothetical_equity": float(path[f"equity_{leg}"].iloc[-1]),
                "ex_post_initial_collateral_boundary": max(
                    0.0, float((path[f"requirement_{leg}"] - path[f"pnl_{leg}"]).max())
                ),
            }
            if intervals is not None and f"exposed_{leg}" in intervals:
                active = intervals[intervals[f"exposed_{leg}"]]
                breached_intervals = active[active[f"envelope_breach_{leg}"]]
                first_interval = None
                if not breached_intervals.empty:
                    time, row = next(iter(breached_intervals.iterrows()))
                    first_interval = {
                        "from": (time - pd.Timedelta(hours=1)).isoformat(),
                        "through": time.isoformat(),
                        **{k: float(row[f"{k}_{leg}"]) for k in ("equity", "requirement", "headroom", "pnl")},
                    }
                detail[leg]["interval_stress"] = {
                    "exposed_hours": len(active),
                    "first_envelope_breach": first_interval,
                    "minimum_headroom": float(active[f"headroom_{leg}"].min()) if len(active) else None,
                    "ex_post_initial_collateral_boundary": max(
                        0.0, float((active[f"requirement_{leg}"] - active[f"pnl_{leg}"]).max())
                    )
                    if len(active)
                    else 0.0,
                    "ordering": "adverse mark after all proxy funding debits and before credits; not observed timing",
                }
                detail[leg]["ex_post_stress_collateral_boundary"] = max(
                    detail[leg]["ex_post_initial_collateral_boundary"],
                    detail[leg]["interval_stress"]["ex_post_initial_collateral_boundary"],
                )
        report["per_symbol"][symbol] = detail
    report["available"] = bool(pnl)
    return report
