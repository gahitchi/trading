"""Portable, no-I/O tests for pre-holdout bearish signal screening."""

import pandas as pd
import pytest

from lab.bearish_ensemble import RoundTripCosts
from lab.bearish_ensemble_screen import screen_research_frame
from tests.test_bearish_ensemble import bar

COSTS = RoundTripCosts(5, 5, 2, 1)


def history(n=202):
    candles = [bar(i, volume=3000 if i == 200 else 1000, sell_share=0.72 if i == 200 else 0.4) for i in range(n)]
    index = pd.to_datetime([b.end_ns for b in candles], unit="ns", utc=True)
    data = [
        {
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "quote_volume": b.quote_volume,
            "taker_buy_quote": b.taker_buy_quote,
            "oi_invalid": False,
        }
        for b in candles
    ]
    return pd.DataFrame(data, index=index)


def boundary(b):
    return b.index[-1].normalize() + pd.Timedelta(days=1)


def scan(b):
    return screen_research_frame(b, boundary=boundary(b), costs=COSTS)


def test_signal_census_reports_candidates_not_fills():
    b = history()
    result = scan(b)
    assert result.bars == 202
    assert result.valid_bars == 202
    assert result.discarded_quality_bars == 0
    assert result.candidate_times_ns == (int(b.index[200].value),)
    assert result.candidate_setups[0] in {"breakdown", "continuation"}
    assert result.veto_counts["warmup"] > 0


def test_prefix_invariance_for_sequential_completed_bars():
    b = history()
    prefix = scan(b.iloc[:201])
    whole = scan(b)
    assert prefix.candidate_times_ns == whole.candidate_times_ns
    assert prefix.candidate_setups == whole.candidate_setups


def test_invalid_oi_marker_resets_warmup_without_bridging():
    b = history()
    b.loc[b.index[199], "oi_invalid"] = True
    result = scan(b)
    assert result.candidate_times_ns == ()
    assert result.discarded_quality_bars == 1
    assert result.veto_counts["untrusted_oi"] == 1
    assert result.veto_counts["warmup"] > 0


def test_bad_candle_is_quarantined_without_constructing_a_signal():
    b = history()
    b.loc[b.index[199], "quote_volume"] = float("nan")
    result = scan(b)
    assert result.candidate_times_ns == ()
    assert result.discarded_quality_bars == 1
    assert result.veto_counts["invalid_candle"] == 1


def test_missing_timestamp_discards_previous_indicator_context():
    b = history()
    shifted = list(b.index)
    shifted[200:] = [ts + pd.Timedelta(minutes=1) for ts in shifted[200:]]
    b.index = pd.DatetimeIndex(shifted)
    result = scan(b)
    assert result.candidate_times_ns == ()
    assert result.veto_counts["history_gap"] == 1


def test_refuses_any_protected_holdout_candle_even_if_old_candles_exist():
    b = history()
    with pytest.raises(ValueError, match="protected holdout"):
        screen_research_frame(b, boundary=b.index[-1].normalize(), costs=COSTS)


def test_refuses_malformed_provenance_or_time_order():
    b = history()
    with pytest.raises(ValueError, match="quality"):
        scan(b.drop(columns=["oi_invalid"]))
    b.loc[b.index[199], "oi_invalid"] = None
    with pytest.raises(ValueError, match="quality"):
        scan(b)
    b = history()
    with pytest.raises(ValueError, match="ordered unique"):
        scan(b.iloc[::-1])


def test_never_short_bullish_history():
    b = history()
    for i in range(len(b)):
        candle = bar(i, falling=False, volume=3000 if i == 200 else 1000, sell_share=0.8)
        b.iloc[i, b.columns.get_loc("open")] = candle.open
        b.iloc[i, b.columns.get_loc("high")] = candle.high
        b.iloc[i, b.columns.get_loc("low")] = candle.low
        b.iloc[i, b.columns.get_loc("close")] = candle.close
    assert scan(b).candidate_times_ns == ()


def test_rejects_subminute_or_non_utc_data():
    b = history()
    b.index = b.index + pd.Timedelta(seconds=20)
    with pytest.raises(ValueError, match="invalid bar timestamp"):
        scan(b)
    b = history()
    b.index = b.index.tz_convert("Europe/Rome")
    with pytest.raises(ValueError, match="UTC"):
        scan(b)


def test_read_only_input_preserved():
    b = history()
    original = b.copy(deep=True)
    scan(b)
    pd.testing.assert_frame_equal(b, original)
