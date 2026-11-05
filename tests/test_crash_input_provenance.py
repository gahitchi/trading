"""Crash input content fingerprints and pre-access holdout isolation."""

import numpy as np
import pandas as pd
import pytest

from lab import crash_data, crash_study
from lab.trials import InputEvidence


def sample():
    return pd.DataFrame(
        {
            "ts": pd.date_range("2026-05-07", periods=3, freq="min", tz="UTC"),
            "close": [100.0, float("nan"), -0.0],
            "quote_volume": [100, 200, 300],
        }
    )


def test_canonical_fingerprint_is_deterministic_and_content_sensitive():
    original = sample()
    a = InputEvidence()
    a.frame("klines:BTCUSDT", original)
    first = a.snapshot()
    again = InputEvidence()
    again.frame("klines:BTCUSDT", original.copy())
    assert first == again.snapshot()
    assert first["streams"][0]["complete"]
    assert first["streams"][0]["records"] == 4  # schema + three rows

    altered = original.copy()
    altered.loc[0, "close"] = np.nextafter(100.0, 101.0)
    other = InputEvidence()
    other.frame("klines:BTCUSDT", altered)
    assert first["sha256"] != other.snapshot()["sha256"]

    ordered = InputEvidence()
    ordered.frame("klines:BTCUSDT", original.iloc[::-1].reset_index(drop=True))
    assert first["sha256"] != ordered.snapshot()["sha256"]


def test_canonical_fingerprint_includes_schema_and_negative_zero():
    a = InputEvidence()
    a.frame("oi:BTCUSDT", sample())
    original = a.snapshot()["sha256"]

    columns = InputEvidence()
    columns.frame("oi:BTCUSDT", sample()[["ts", "quote_volume", "close"]])
    assert columns.snapshot()["sha256"] != original

    sign = sample()
    sign.loc[2, "close"] = 0.0
    changed = InputEvidence()
    changed.frame("oi:BTCUSDT", sign)
    assert changed.snapshot()["sha256"] != original


def test_absent_stream_is_recorded_and_duplicate_name_rejected():
    evidence = InputEvidence()
    evidence.frame("oi:BTCUSDT", None)
    entry = evidence.snapshot()["streams"][0]
    assert entry["complete"] and entry["records"] == 1
    with pytest.raises(ValueError, match="duplicate input stream"):
        evidence.frame("oi:BTCUSDT", None)


def test_naive_timestamp_fails_closed():
    f = sample()
    f["ts"] = f["ts"].dt.tz_localize(None)
    evidence = InputEvidence()
    with pytest.raises(ValueError, match="timezone-aware"):
        evidence.frame("klines:BTCUSDT", f)
    assert not evidence.snapshot()["streams"][0]["complete"]


def test_research_candle_cutoff_never_opens_holdout_daily_or_monthly_file(tmp_path, monkeypatch):
    folder = tmp_path / "klines" / "BTCUSDT"
    folder.mkdir(parents=True)
    before = sample()
    before.to_parquet(folder / "2026-05-07.parquet", index=False)
    (folder / "2026-05-08.parquet").write_bytes(b"must not read")
    (folder / "2026-05.parquet").write_bytes(b"must not read")

    original = pd.read_parquet

    def guarded(path, *args, **kwargs):
        assert str(path).endswith("2026-05-07.parquet"), "read an archive overlapping holdout"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", guarded)
    checked = crash_data.load(
        tmp_path,
        "klines",
        "BTCUSDT",
        before=pd.Timestamp("2026-05-08T00:00:00Z"),
    )
    assert checked.equals(before)


def test_crash_run_fingerprints_pre_holdout_inputs_without_reading_holdout(tmp_path, monkeypatch):
    root = tmp_path / "crash"
    directory = root / "klines" / "BTCUSDT"
    directory.mkdir(parents=True)
    for day in ("2026-05-06", "2026-05-07"):
        ts = pd.date_range(day, periods=1440, freq="min", tz="UTC")
        k = pd.DataFrame(
            {
                "ts": ts,
                "open": [100.0] * len(ts),
                "high": [101.0] * len(ts),
                "low": [99.0] * len(ts),
                "close": [100.0] * len(ts),
                "quote_volume": [100.0] * len(ts),
            }
        )
        k.to_parquet(directory / f"{day}.parquet", index=False)
    (directory / "2026-05-08.parquet").write_bytes(b"protected holdout")
    original = pd.read_parquet

    def guarded(path, *args, **kwargs):
        assert "2026-05-08.parquet" not in str(path), "holdout candle was opened"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", guarded)
    evidence = InputEvidence()
    crash_study.run(
        root,
        ["BTCUSDT"],
        crash_study.CrashSettings.preset("breakdown"),
        pd.Timestamp("2026-05-08T00:00:00Z"),
        evidence=evidence,
    )
    streams = evidence.snapshot()["streams"]
    assert [s["name"] for s in streams] == ["klines:BTCUSDT", "oi:BTCUSDT"]
    assert all(s["complete"] for s in streams)
    assert streams[0]["records"] == 2881
    assert streams[1]["records"] == 1
