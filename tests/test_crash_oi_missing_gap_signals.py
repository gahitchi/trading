"""Missing five-minute source snapshots cannot be bridged by crash signals."""

import numpy as np
import pandas as pd

from lab.crash_study import CrashSettings, bars, signals


def source_oi(start: str, end: str, *, missing: pd.DatetimeIndex):
    ts = pd.date_range(start, end, freq="5min", tz="UTC")
    ts = ts.difference(missing)
    return pd.DataFrame({
        "ts": ts,
        "oi_qty": 1000.0 + 3.0 * np.arange(len(ts)),
        "oi_value": 40_000_000.0 + 300.0 * np.arange(len(ts)),
    })


def minute_klines(start: str, end: str):
    ts = pd.date_range(start, end, freq="min", tz="UTC")
    return pd.DataFrame({
        "ts": ts,
        "close": 100.0 * np.power(.999, np.arange(len(ts))),
        "quote_volume": np.full(len(ts), 100.0),
    })


def test_october_two_missing_snapshots_become_explicit_delayed_quality_events():
    missing = pd.DatetimeIndex([
        "2024-10-28T16:25:00Z", "2024-10-28T16:30:00Z",
    ])
    oi = source_oi(
        "2024-10-27T15:00:00Z", "2024-10-28T18:00:00Z",
        missing=missing,
    )
    b = bars(
        minute_klines("2024-10-27T15:00:00Z", "2024-10-28T18:00:00Z"),
        oi,
    )
    assert not bool(b.loc[pd.Timestamp("2024-10-28T16:25:00Z"), "oi_invalid"])
    assert bool(b.loc[pd.Timestamp("2024-10-28T16:30:00Z"), "oi_invalid"])
    assert bool(b.loc[pd.Timestamp("2024-10-28T16:35:00Z"), "oi_invalid"])
    assert not bool(b.loc[pd.Timestamp("2024-10-28T16:40:00Z"), "oi_invalid"])
    # The ffill tolerance itself cannot detect both lost observations:
    # the most recent published observation can still be non-NaN.
    assert pd.notna(b.loc[pd.Timestamp("2024-10-28T16:35:00Z"), "oi"])


def test_short_side_breakdown_signal_is_vetoed_until_full_oi_lookback_is_clean():
    missing = pd.DatetimeIndex([
        "2024-10-28T16:25:00Z", "2024-10-28T16:30:00Z",
    ])
    start = "2024-10-27T15:00:00Z"
    end = "2024-10-28T19:00:00Z"
    oi = source_oi(start, end, missing=missing)
    b = bars(minute_klines(start, end), oi)
    s = CrashSettings.preset(
        "breakdown", drop_pct=.01, window_min=60,
        vol_mult=0, oi_pct=0, vol_target=0,
    )
    hit = pd.Series(signals(b, s), index=b.index)
    assert hit.loc[pd.Timestamp("2024-10-28T16:29:00Z")]
    assert not hit.loc[pd.Timestamp("2024-10-28T16:30:00Z")]
    assert not hit.loc[pd.Timestamp("2024-10-28T16:40:00Z")]
    assert not hit.loc[pd.Timestamp("2024-10-28T17:35:00Z")]
    assert hit.loc[pd.Timestamp("2024-10-28T17:36:00Z")]


def test_february_125_missing_snapshots_have_no_invented_values():
    start = "2024-02-16T12:00:00Z"
    end = "2024-02-17T02:00:00Z"
    missing = pd.date_range(
        "2024-02-16T13:35:00Z", periods=125, freq="5min"
    )
    oi = source_oi(start, end, missing=missing)
    b = bars(minute_klines(start, end), oi)
    assert b["oi_invalid"].sum() == 125
    assert not bool(b.loc[pd.Timestamp("2024-02-16T13:35:00Z"), "oi_invalid"])
    assert bool(b.loc[pd.Timestamp("2024-02-16T13:40:00Z"), "oi_invalid"])
    assert bool(b.loc[pd.Timestamp("2024-02-17T00:00:00Z"), "oi_invalid"])
    assert not bool(b.loc[pd.Timestamp("2024-02-17T00:05:00Z"), "oi_invalid"])
    assert pd.isna(b.loc[pd.Timestamp("2024-02-16T14:10:00Z"), "oi"])


def test_no_false_missing_snapshot_when_source_is_complete():
    start = "2024-10-27T15:00:00Z"
    end = "2024-10-28T18:00:00Z"
    oi = source_oi(start, end, missing=pd.DatetimeIndex([]))
    b = bars(minute_klines(start, end), oi)
    assert not b["oi_invalid"].any()


def test_quarantined_observations_and_absent_snapshots_both_invalidate_signals():
    start = "2024-10-27T15:00:00Z"
    end = "2024-10-28T18:00:00Z"
    missing = pd.DatetimeIndex(["2024-10-28T16:25:00Z"])
    oi = source_oi(start, end, missing=missing)
    oi["oi_valid"] = True
    idx = oi.index[oi["ts"] == pd.Timestamp("2024-10-28T16:45:00Z")][0]
    oi.loc[idx, "oi_valid"] = False
    oi.loc[idx, "oi_qty"] = np.nan
    oi.loc[idx, "oi_value"] = 0.0
    b = bars(minute_klines(start, end), oi)
    assert b.loc[pd.Timestamp("2024-10-28T16:30:00Z"), "oi_invalid"]
    assert b.loc[pd.Timestamp("2024-10-28T16:50:00Z"), "oi_invalid"]
    assert pd.isna(b.loc[pd.Timestamp("2024-10-28T16:50:00Z"), "oi"])
    assert b["oi_invalid"].sum() == 2
