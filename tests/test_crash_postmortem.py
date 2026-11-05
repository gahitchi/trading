"""Synthetic crash postmortem and exposure-matched calendar placebo tests."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lab import crash_placebo, crash_postmortem


def minute_bars(start="2026-01-01", days=31):
    idx = pd.date_range(
        pd.Timestamp(start, tz="UTC") + pd.Timedelta(minutes=1),
        periods=days * 1440,
        freq="min",
    )
    # Synthetic drift is deterministic but never consulted by sampling.
    o = 100 + np.arange(len(idx)) * 0.00002
    c = o + np.sin(np.arange(len(idx)) / 1000) * 0.1
    return pd.DataFrame(
        {
            "open": o,
            "high": np.maximum(o, c) + 0.2,
            "low": np.minimum(o, c) - 0.2,
            "close": c,
        },
        index=idx,
    )


def original():
    start = pd.Timestamp("2026-01-15T04:00:00Z")
    end = start + pd.Timedelta(hours=4)
    # Reference gross 5% for short; price-level economics are
    # deliberately independent of the synthetic control bars.
    return pd.DataFrame(
        [
            {
                "signal": start - pd.Timedelta(minutes=2),
                "entry": start,
                "exit": end,
                "gross": 0.05,
                "net": 0.047,
                "weight": 1.4,
                "reason": "stop",
                "entry_reference": 100.0,
                "exit_reference": 95.0,
                "fee_cost": 0.001,
                "slippage_cost": 0.002,
                "funding_return": 0.0,
                "funding_return_upper": 0.0,
                "funding_ambiguous_events": 0,
            }
        ]
    )


def test_postmortem_reconciles_cashflows_and_exit_reason():
    bars = minute_bars()
    report = crash_postmortem.attribution(original(), bars, -1)
    assert len(report) == 1
    assert report.loc[0, "gross_bps"] == pytest.approx(500)
    assert report.loc[0, "net_bps"] == pytest.approx(470)
    assert report.loc[0, "duration_min"] == 240
    assert report.loc[0, "fullbar_favorable_bps"] >= 0
    assert report.loc[0, "fullbar_adverse_bps"] >= 0
    summary = crash_postmortem.describe(report)
    assert summary["by_exit"]["stop"]["trades"] == 1
    assert summary["weighted_exposure_minutes"] == pytest.approx(336)
    assert summary["fullbar_extrema_are_not_executable"] is True
    assert summary["cost_decomposition_bps"]["net_bps"] == pytest.approx(
        summary["cost_decomposition_bps"]["gross_bps"]
        - summary["cost_decomposition_bps"]["fee_bps"]
        - summary["cost_decomposition_bps"]["slippage_bps"]
        + summary["cost_decomposition_bps"]["funding_bps"]
    )


def test_postmortem_rejects_unreconciled_economics():
    bad = original()
    bad.loc[0, "net"] = 0.1
    with pytest.raises(ValueError, match="reconciliation"):
        crash_postmortem.attribution(bad, minute_bars(), -1)


def test_postmortem_rejects_missing_candle_without_skip():
    bars = minute_bars()
    missing = bars.drop(pd.Timestamp("2026-01-15T06:00:00Z"))
    with pytest.raises(ValueError, match="missing"):
        crash_postmortem.attribution(original(), missing, -1)


def test_utc_month_and_duration_weight_matched_without_return_optimization():
    bars, ref = minute_bars(), original()
    first = crash_placebo.matched_placebo(
        bars,
        ref,
        side=-1,
        fee_bps=5.0,
        slippage_bps=5.0,
        seed=20261105,
    )
    second = crash_placebo.matched_placebo(
        bars,
        ref,
        side=-1,
        fee_bps=5.0,
        slippage_bps=5.0,
        seed=20261105,
    )
    pd.testing.assert_frame_equal(first, second)
    assert len(first) == len(ref) == 1
    assert first.entry.iloc[0].month == ref.entry.iloc[0].month
    assert (first.entry.iloc[0].hour, first.entry.iloc[0].minute) == (4, 0)
    assert first.entry.iloc[0].day != ref.entry.iloc[0].day
    assert abs(first.entry.iloc[0].day - ref.entry.iloc[0].day) >= 3
    assert first.weight.iloc[0] == ref.weight.iloc[0]
    compare = crash_placebo.comparison(ref, first)
    assert compare["identical_realized_exposure_minutes"]
    assert compare["identical_entry_notional_weights"]
    assert compare["trades"] == 1
    assert compare["status"] == "descriptive_no_hypothesis_test"


def test_placebo_fails_when_not_enough_complete_same_month_data():
    bars = minute_bars().loc["2026-01-14":"2026-01-16"]
    with pytest.raises(ValueError, match="no valid"):
        crash_placebo.matched_placebo(
            bars,
            original(),
            side=-1,
            fee_bps=5,
            slippage_bps=5,
            seed=42,
        )


def test_matched_placebo_refuses_weight_or_exposure_mismatch():
    bars, ref = minute_bars(), original()
    control = crash_placebo.matched_placebo(
        bars,
        ref,
        side=-1,
        fee_bps=5,
        slippage_bps=5,
        seed=17,
    )
    changed = control.copy()
    changed.loc[0, "weight"] *= 1.01
    with pytest.raises(ValueError, match="not duration/size"):
        crash_placebo.comparison(ref, changed)
    changed = control.copy()
    changed.loc[0, "exit"] += pd.Timedelta(minutes=1)
    with pytest.raises(ValueError, match="not duration/size"):
        crash_placebo.comparison(ref, changed)


def test_match_seed_schedule_frozen_and_no_sharpe_claim():
    assert len(crash_placebo.SEEDS) == len(set(crash_placebo.SEEDS)) == 16
    assert crash_placebo.MIN_DAY_SEPARATION == 3
    assert crash_placebo.MAX_DAY_OFFSET == 14
    assert "infer" not in crash_placebo.POLICY
