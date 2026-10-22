"""No silent data loss during research ingestion or strict saved-data access."""

import numpy as np
import pandas as pd
import pytest

from lab import data


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
def test_ingestion_rejects_missing_fields_instead_of_erasing_rows(field):
    idx = pd.bdate_range("2024-01-01", periods=8)
    close = np.linspace(90, 110, 8)
    frame = pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e6},
        index=idx,
    )
    frame.loc[idx[3], field] = np.nan
    with pytest.raises(data.DataError, match="missing/nonfinite OHLCV"):
        data.validate(frame, "X")


def test_ingestion_rejects_infinite_prices_and_volume():
    idx = pd.bdate_range("2024-01-01", periods=8)
    close = np.linspace(90, 110, 8)
    frame = pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e6},
        index=idx,
    )
    frame.loc[idx[4], "close"] = np.inf
    with pytest.raises(data.DataError, match="missing/nonfinite OHLCV"):
        data.validate(frame, "X")


def test_analysis_loader_does_not_mutate_saved_rows(goog_lab, tmp_path):
    from dataclasses import replace

    config = replace(goog_lab, data_dir=tmp_path)
    idx = pd.bdate_range("2024-01-01", periods=8)
    raw = pd.DataFrame(
        {"open": 100, "high": 102, "low": 98, "close": 101, "volume": 1e6}, index=idx
    )
    raw.iloc[2, raw.columns.get_loc("volume")] = np.nan
    raw.to_parquet(tmp_path / "X.parquet")
    restored = data.load_many_unmodified(config, ["X"])["X"]
    assert pd.isna(restored["volume"].iloc[2])
    with pytest.raises(data.DataError, match="missing/nonfinite OHLCV"):
        data.load(config, "X")
