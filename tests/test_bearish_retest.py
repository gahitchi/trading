"""Causal invariants for the untested frozen bearish support-retest candidate."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lab import bearish_retest as br


def sample():
    idx = pd.date_range("2026-01-01T00:00:00Z", periods=12*1440, freq="min")
    b = pd.DataFrame({
        "open": np.full(len(idx), 100.0),
        "high": np.full(len(idx), 100.2),
        "low": np.full(len(idx), 99.8),
        "close": np.full(len(idx), 100.0),
        "quote_volume": np.ones(len(idx)),
        "oi_invalid": np.zeros(len(idx), dtype=bool),
    }, index=idx)
    return b


def setup(*, target=False, stop=True, confirm=True, retest=True, high_volume=True):
    b = sample()
    start = 9*1440 + 60
    if high_volume:
        b.loc[b.index[start-59:start+1], "quote_volume"] = 3.0
    # Prior 24h low 99.8; T0 closes 99.4 < 99.5006.
    b.loc[b.index[start], ["open", "high", "low", "close"]] = [100,100.2,99.2,99.4]
    if retest:
        b.loc[b.index[start+1], ["open","high","low","close"]] = [99.4,99.8,99.3,99.5]
    else:
        for i in range(start+1,start+61):
            b.loc[b.index[i], ["open","high","low","close"]] = [99.4,99.5,99.2,99.4]
    if confirm:
        b.loc[b.index[start+2], ["open","high","low","close"]] = [99.5,99.5,99.0,99.1]
    else:
        for i in range(start+2,start+7):
            b.loc[b.index[i], ["open","high","low","close"]] = [99.5,99.5,99.4,99.5]
    # Entry is at open of bar confirmation+2 => start+4.
    b.loc[b.index[start+4], ["open","high","low","close"]] = [99.0,99.5,98.4,99.2]
    if stop:
        b.loc[b.index[start+5], ["open","high","low","close"]] = [99.2,100.3,97.0 if target else 99.0,99.6]
    elif target:
        b.loc[b.index[start+5], ["open","high","low","close"]] = [99.2,99.3,97.0,97.2]
    return b, start


def run(b):
    return br.simulate(b, boundary=pd.Timestamp("2026-02-01T00:00:00Z"))


def test_frozen_candidate_waits_for_completed_confirmation_plus_a_full_bar_delay():
    b, t0 = setup()
    result = run(b)
    assert result.model == br.MODEL
    assert len(result.trades) == 1
    row = result.trades.iloc[0]
    assert row["breakout"] == b.index[t0]
    assert row["retest"] == b.index[t0+1]
    assert row["signal"] == b.index[t0+2]
    assert row["entry"] == b.index[t0+4]-pd.Timedelta(minutes=1)
    assert row["exit"] == b.index[t0+5]
    assert row["reason"] == "stop"
    assert result.event_counts["breakout"] == 1
    assert result.event_counts["trade"] == 1
    assert 0 < row["weight"] <= 2
    assert row["net"] < row["gross"]


def test_intrabar_stop_wins_when_stop_and_target_both_touched():
    b, _ = setup(target=True)
    result = run(b)
    assert len(result.trades) == 1
    assert result.trades.iloc[0]["reason"] == "stop"


def test_retest_and_confirmation_required_before_entry():
    b, t0 = setup(retest=False, confirm=False)
    result = run(b)
    assert result.trades.empty
    assert result.event_counts["breakout"] == 1
    assert result.event_counts["no_retest"] == 1
    b2, _ = setup(confirm=False)
    result2 = run(b2)
    assert result2.trades.empty
    assert result2.event_counts["no_confirmation"] == 1


def test_volume_regime_gate_and_causal_hourly_volume_buckets():
    b, _ = setup(high_volume=False)
    assert run(b).trades.empty
    # A volume surge on the next day cannot retroactively make the
    # prior under-volume breakout eligible.
    b.iloc[-60:, b.columns.get_loc("quote_volume")] = 1000.0
    assert run(b).trades.empty


def test_support_reclaim_cancels_before_confirmation():
    b, t0 = setup()
    b.loc[b.index[t0+1], ["open","high","low","close"]] = [99.4,100.0,99.3,99.9]
    result = run(b)
    assert result.trades.empty
    assert result.event_counts["support_reclaimed"] >= 1


def test_missing_minute_is_never_interpolated():
    b, t0 = setup()
    b = b.drop(b.index[t0-2])
    with pytest.raises(ValueError, match="missing one-minute"):
        run(b)


def test_bad_oi_quality_vetoes_prior_24h_and_retest_confirmation_windows():
    b, t0 = setup()
    b.loc[b.index[t0-60], "oi_invalid"] = True
    assert run(b).trades.empty
    b, t0 = setup()
    b.loc[b.index[t0+1], "oi_invalid"] = True
    result = run(b)
    assert result.trades.empty
    assert result.event_counts["untrusted_oi"] >= 1


def test_holdout_is_enforced_by_exact_utc_bar_completion():
    b, t0 = setup()
    boundary = b.index[t0+4]
    with pytest.raises(ValueError, match="protected holdout"):
        br.simulate(b, boundary=boundary)
    with pytest.raises(ValueError, match="UTC"):
        br.simulate(b, boundary=pd.Timestamp("2026-02-01"))


def test_invalid_candle_or_nonfinite_source_fails_closed():
    b, t0 = setup()
    b.loc[b.index[t0], "high"] = 99.0
    with pytest.raises(ValueError, match="inconsistent"):
        run(b)
    b, t0 = setup()
    b.loc[b.index[t0-1], "quote_volume"] = float("nan")
    with pytest.raises(ValueError, match="nonfinite"):
        run(b)


def test_stop_reference_and_target_are_frozen_two_to_one_not_parameters():
    b, _ = setup()
    row = run(b).trades.iloc[0]
    stop = 1.001 * max(row["support"], b.loc[row["retest"], "high"])
    assert row["stop_reference"] == pytest.approx(stop)
    assert row["target_reference"] == pytest.approx(3 * row["entry_reference"] - 2 * stop)
    assert br.REWARD_TO_RISK == 2.0
    assert br.MAX_HOLD_MINUTES == 240
    assert br.VOL_TARGET == 0.5
