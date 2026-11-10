"""Synthetic-only maintenance-envelope scenarios. No exchange liquidation claim."""

from dataclasses import replace

import pandas as pd
import pytest

from lab.bearish_equity import EquityAssumptions
from lab.bearish_margin_audit import MarginPolicy, margin_envelope
from tests.test_bearish_equity import BOUNDARY, COSTS, candles, replay, trade


def sample():
    b = candles()
    entry = pd.Timestamp("2026-01-01T02:00:00Z")
    exit_ = pd.Timestamp("2026-01-01T02:03:00Z")
    b.loc[exit_, "close"] = 99.0
    return b, trade(b, entry, exit_)


def test_default_short_remains_above_margin_requirement():
    b, t = sample()
    report = margin_envelope(b, replay(t), COSTS, boundary=BOUNDARY)
    assert report.qualified is False
    assert len(report.trades) == 1
    assert report.trades[0].maintenance_excess > 0
    assert report.trades[0].worst_high == 101.0
    assert report.minimum_maintenance_excess == report.trades[0].maintenance_excess


def test_intrabar_adverse_spike_triggers_margin_breach_without_fabricated_exit():
    b, t = sample()
    b.loc[pd.Timestamp("2026-01-01T02:02:00Z"), "high"] = 192.0
    # An intrabar margin breach can occur even before the account is insolvent.
    a = EquityAssumptions(notional_fraction=1, minimum_equity_fraction=0)
    with pytest.raises(ValueError, match="maintenance margin breached"):
        margin_envelope(b, replay(t), COSTS, boundary=BOUNDARY, assumptions=a)


def test_higher_maintenance_margin_reduces_excess_without_changing_trade():
    b, t = sample()
    lower = margin_envelope(
        b, replay(t), COSTS, boundary=BOUNDARY, policy=MarginPolicy(maintenance_margin_fraction=0.05)
    )
    higher = margin_envelope(
        b, replay(t), COSTS, boundary=BOUNDARY, policy=MarginPolicy(maintenance_margin_fraction=0.09)
    )
    assert higher.minimum_maintenance_excess < lower.minimum_maintenance_excess
    assert t.net_return == replay(t).trades[0].net_return


def test_empty_replay_reports_no_margin_positions():
    result = margin_envelope(candles(), replay(), COSTS, boundary=BOUNDARY)
    assert result.trades == ()
    assert result.minimum_maintenance_excess is None


def test_malformed_or_excessive_initial_margin_policy_rejected():
    with pytest.raises(ValueError, match="maintenance margin"):
        MarginPolicy(initial_margin_fraction=0.05, maintenance_margin_fraction=0.10)
    with pytest.raises(ValueError, match="liquidation buffer"):
        MarginPolicy(liquidation_buffer_bps=10_000)
    b, t = sample()
    with pytest.raises(ValueError, match="insufficient initial margin"):
        margin_envelope(
            b,
            replay(t),
            COSTS,
            boundary=BOUNDARY,
            assumptions=EquityAssumptions(notional_fraction=1),
            policy=MarginPolicy(initial_margin_fraction=1),
        )


def test_margin_preflight_inherits_full_day_and_holdout_guards():
    b, t = sample()
    with pytest.raises(ValueError, match="protected holdout"):
        margin_envelope(b, replay(t), COSTS, boundary=b.index[-1].normalize())
    with pytest.raises(ValueError, match="minute-price gaps"):
        margin_envelope(b.drop(index=b.index[50]), replay(t), COSTS, boundary=BOUNDARY)


def test_tampered_trade_rejected_before_margin_accounting():
    b, t = sample()
    with pytest.raises(ValueError, match="conservation"):
        margin_envelope(b, replay(replace(t, fee_cost=t.fee_cost + 0.001)), COSTS, boundary=BOUNDARY)


def test_adverse_funding_reserve_reduces_available_margin():
    b, t = sample()
    base = margin_envelope(b, replay(t), COSTS, boundary=BOUNDARY)
    # Larger frozen funding reserve must be reflected in both the trade
    # economics and its margin headroom; don't mutate t to fake a fill.
    from lab.bearish_ensemble import RoundTripCosts

    more = RoundTripCosts(5, 5, 2, 20)
    # Keep the original fill references while matching the new reserve.
    shifted = replace(
        t,
        funding_reserve_cost=more.adverse_funding_bps / 10_000,
        net_return=t.net_return - (more.adverse_funding_bps - COSTS.adverse_funding_bps) / 10_000,
    )
    stressed = margin_envelope(b, replay(shifted), more, boundary=BOUNDARY)
    assert stressed.minimum_maintenance_excess < base.minimum_maintenance_excess
