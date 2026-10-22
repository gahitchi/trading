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


def test_staggered_listing_and_delisting_preserve_exact_calendar_masks():
    prices = _prices(np.linspace(90, 130, 150))
    newcomer = prices.iloc[40:115].copy()
    # Gaps in a short-lived ticker must not be filled with invented bars.
    newcomer = newcomer.drop(newcomer.index[17])
    source = {"CORE": prices, "YOUNG": newcomer}
    first = data.block_bootstrap_panel(source, 31)
    second = data.block_bootstrap_panel(dict(reversed(list(source.items()))), 31)
    for ticker in source:
        assert first[ticker].index.equals(source[ticker].index)
        pd.testing.assert_frame_equal(first[ticker], second[ticker])
        assert np.isfinite(first[ticker]["close"]).all()
        assert (first[ticker]["volume"] >= 0).all()
    # Real calendar overlap preserves exact cross-market shock selection:
    # proportional instruments with identical full histories stay proportional.
    twin = data.block_bootstrap_panel({"CORE": prices, "TWIN": prices * 3}, 99)
    np.testing.assert_allclose(twin["TWIN"]["close"], twin["CORE"]["close"] * 3, rtol=1e-11)


def test_missing_internal_session_does_not_backfill():
    prices = _prices(np.linspace(90, 130, 120))
    missing = prices.drop(prices.index[25])
    out = data.block_bootstrap_panel({"X": prices, "Y": missing}, 7, block=8)
    assert prices.index[25] not in out["Y"].index
    assert out["X"].index.equals(prices.index)


def test_joint_bootstrap_rejects_invalid_block_size_and_missing_data():
    prices = _prices(np.linspace(90, 130, 40))
    with pytest.raises(data.DataError, match="positive integer"):
        data.block_bootstrap_panel({"X": prices}, 7, block=0)
    with pytest.raises(data.DataError, match="at least"):
        data.block_bootstrap_panel({"X": prices.iloc[:1]}, 7)
    invalid = prices.copy()
    invalid.iloc[5, invalid.columns.get_loc("close")] = np.nan
    with pytest.raises(data.DataError):
        data.block_bootstrap_panel({"X": invalid}, 7)


def test_late_listing_does_not_change_established_assets_null():
    """G002-F2 regression: an unrelated late listing must not lower GOOG volatility."""
    rng = np.random.default_rng(14)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.022, 600)))
    established = _prices(close)
    young = established.iloc[-90:].copy()
    for seed in range(12):
        alone = data.block_bootstrap_panel({"CORE": established}, seed)["CORE"]
        panel = data.block_bootstrap_panel({"CORE": established, "LATE": young}, seed)
        pd.testing.assert_frame_equal(alone, panel["CORE"])
        assert panel["LATE"].index.equals(young.index)


def test_fallback_diagnostics_and_explicit_significance_limit():
    established = _prices(100 * np.exp(np.cumsum(np.random.default_rng(2).normal(0, 0.01, 240))))
    late = established.iloc[-35:]
    result, info = data.block_bootstrap_panel({"CORE": established, "LATE": late}, 19, return_diagnostics=True)
    assert result["LATE"].index.equals(late.index)
    assert info["per_instrument"]["CORE"]["fallback_blocks"] == 0
    assert info["per_instrument"]["LATE"]["fallback_blocks"] > 0
    assert 0.0 < info["fallback_fraction"] <= 1.0
    assert info["fallback_blocks"] <= info["participating_blocks"]
