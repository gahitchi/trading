"""Joint market bootstrap preserves contemporaneous cross-asset dependence."""

import numpy as np
import pandas as pd
import pytest

from lab import data


def _prices(close):
    c = np.asarray(close, dtype=float)
    idx = pd.bdate_range("2020-01-01", periods=len(c))
    return pd.DataFrame(
        {"open": c, "high": c * 1.02, "low": c * 0.98, "close": c, "volume": np.ones(len(c)) * 1e6},
        index=idx,
    )


def test_joint_bootstrap_uses_identical_market_shocks():
    rng = np.random.default_rng(1)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 240)))
    original = _prices(close)
    twin = _prices(close * 3.0)
    samples = data.block_bootstrap_panel({"X": original, "Y": twin}, 31)
    x, y = samples["X"]["close"], samples["Y"]["close"]
    assert np.allclose(y.to_numpy(), 3 * x.to_numpy(), rtol=1e-10)
    assert x.index.equals(original.index)


def test_joint_bootstrap_rejects_misaligned_ticker_histories():
    prices = _prices(np.linspace(90, 130, 30))
    missing = prices.drop(prices.index[10])
    with pytest.raises(data.DataError, match="unaligned"):
        data.block_bootstrap_panel({"X": prices, "Y": missing}, 7)
