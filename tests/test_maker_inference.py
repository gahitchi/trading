"""Pre-registered numerical and dependence checks for maker markout uncertainty."""

import math

import numpy as np
import pandas as pd
import pytest

from lab import maker_study as maker
from lab.fast_research import DAY_NS

SECOND = 1_000_000_000
NOON = DAY_NS // 2


def test_day_cluster_hac_matches_the_pre_registered_rational_example():
    ts = np.arange(30, dtype=np.int64) * DAY_NS + NOON
    stats = maker.day_inference(ts, np.arange(30, dtype=float), 1.0)
    assert stats["n_infer"] == stats["inference_days"] == 30
    assert stats["inference_net_bps"] == 14.5
    assert stats["se_bps"] == pytest.approx(3.913320899155871)
    assert stats["t"] == pytest.approx(3.7052928634418265)
    assert stats["effective_days"] == pytest.approx(5.060702156005479)


def test_duplicated_fills_do_not_create_additional_day_information():
    ts = np.arange(40, dtype=np.int64) * DAY_NS + NOON
    net = np.sin(np.arange(40)) + 0.2
    one = maker.day_inference(ts, net, 1.0)
    duplicated = maker.day_inference(np.repeat(ts, 100), np.repeat(net, 100), 1.0)
    assert duplicated["n_infer"] == 100 * one["n_infer"]
    for name in ("t", "se_bps", "inference_net_bps", "effective_days", "inference_days"):
        assert duplicated[name] == pytest.approx(one[name])
    assert (np.repeat(net, 100).mean() / (np.repeat(net, 100).std(ddof=1) / math.sqrt(4000))) > 9 * (
        net.mean() / (net.std(ddof=1) / math.sqrt(40))
    )


def test_calendar_gaps_are_not_compressed_into_adjacent_days():
    ts = np.arange(30, dtype=np.int64) * 10 * DAY_NS + NOON
    values = np.arange(30, dtype=float)
    stats = maker.day_inference(ts, values, 1.0)
    assert stats["se_bps"] == pytest.approx(values.std(ddof=1) / math.sqrt(30))
    assert stats["effective_days"] == 30


def test_negative_serial_covariance_cannot_improve_on_day_clustering():
    ts = np.arange(40, dtype=np.int64) * DAY_NS + NOON
    values = np.tile([-1.0, 1.0], 20) + 0.1
    stats = maker.day_inference(ts, values, 1.0)
    assert stats["se_bps"] == pytest.approx(values.std(ddof=1) / math.sqrt(40))
    assert stats["effective_days"] == pytest.approx(40.0)


def test_unequal_day_sizes_are_visible_in_the_effective_day_diagnostic():
    days = np.r_[np.zeros(100, dtype=np.int64), np.arange(1, 30, dtype=np.int64)]
    stats = maker.day_inference(days * DAY_NS + NOON, np.sin(days) + 0.2, 1.0)
    kish = 129**2 / (100**2 + 29)
    assert stats["inference_days"] == 30
    assert 0 < stats["effective_days"] <= kish < 2


def test_cross_midnight_intervals_are_excluded_from_inference():
    ts = np.array([DAY_NS - SECOND, DAY_NS + SECOND, 2 * DAY_NS - 2 * SECOND], dtype=np.int64)
    stats = maker.day_inference(ts, np.array([1000.0, 2.0, 4.0]), 1.0)
    assert stats["n_infer"] == 2
    assert stats["inference_net_bps"] == 3.0
    assert stats["inference_days"] == 1
    assert math.isnan(stats["t"])


def test_too_few_days_or_zero_uncertainty_never_produce_an_inference_t():
    ts = np.arange(29, dtype=np.int64) * DAY_NS + NOON
    few = maker.day_inference(ts, np.arange(29, dtype=float), 1.0)
    assert few["inference_days"] == 29 and math.isnan(few["t"])
    constant = maker.day_inference(np.arange(30, dtype=np.int64) * DAY_NS + NOON, np.ones(30), 1.0)
    assert math.isnan(constant["t"])
    empty = maker.day_inference(np.array([], dtype=np.int64), np.array([]), 1.0)
    assert empty["n_infer"] == empty["inference_days"] == 0 and math.isnan(empty["t"])


def test_input_order_cannot_change_the_adjusted_statistic():
    ts = np.arange(40, dtype=np.int64) * DAY_NS + NOON
    values = np.sin(np.arange(40)) + 0.2
    expected = maker.day_inference(ts, values, 1.0)
    order = np.random.default_rng(17).permutation(len(ts))
    assert maker.day_inference(ts[order], values[order], 1.0) == pytest.approx(expected)


def test_unsigned_epoch_timestamps_do_not_wrap_when_calendar_lags_precede_day_zero():
    ts = np.arange(40, dtype=np.uint64) * DAY_NS + NOON
    values = np.sin(np.arange(40)) + 0.2
    with np.errstate(over="raise"):
        actual = maker.day_inference(ts, values, 1.0)
    assert actual == pytest.approx(maker.day_inference(ts.astype(np.int64), values, 1.0))


@pytest.mark.parametrize("net", [[math.nan], [math.inf], [-math.inf], [1.0, 2.0]])
def test_nonfinite_or_mismatched_samples_are_rejected(net):
    with pytest.raises(ValueError, match="matching timestamps and finite"):
        maker.day_inference(np.array([NOON]), net, 1.0)


def test_fixed_dependent_null_calibration_does_not_exceed_five_percent():
    rng = np.random.default_rng(20261029)
    days, per_day, replications = 40, 20, 400
    ts = np.repeat(np.arange(days, dtype=np.int64) * DAY_NS + NOON, per_day)
    ts += np.tile(np.arange(per_day, dtype=np.int64) * SECOND, days)
    false_positives = 0
    for _ in range(replications):
        effects = rng.normal(size=days)
        for day in range(1, days):
            effects[day] = 0.5 * effects[day - 1] + math.sqrt(0.75) * effects[day]
        values = np.repeat(effects, per_day) + rng.normal(size=days * per_day)
        false_positives += maker.day_inference(ts, values, 1.0)["t"] >= 3.0
    assert false_positives <= 0.05 * replications
    print(f"dependent null: {false_positives}/{replications} exceeded t >= 3")


def test_each_imbalance_group_has_its_own_inference_sample():
    ts = np.arange(30, dtype=np.int64) * DAY_NS + NOON
    mids = (np.sort(np.r_[ts, ts + SECOND]), np.full(60, 100.0))
    fills = pd.DataFrame(
        {
            "ts": np.repeat(ts, 3),
            "side": 1,
            "price": np.tile([99.98, 99.99, 100.01], 30),
            "mid": 100.0,
            "favour": np.tile([0.5, 0.0, -0.5], 30),
        }
    )
    table = maker.evaluate(fills, mids, maker.MakerSettings(horizons_s=(1.0,), maker_fee_bps=0)).set_index("group")
    assert table.loc["all", "n_infer"] == 90
    for name in ("favours", "neutral", "against"):
        assert table.loc[name, "n_infer"] == 30
        assert table.loc[name, "inference_days"] == 30
    assert table.loc["favours", "inference_net_bps"] > 0 > table.loc["against", "inference_net_bps"]


def test_unknown_imbalance_cannot_supply_evidence_for_the_neutral_group():
    ts = np.arange(30, dtype=np.int64) * DAY_NS + NOON
    mids = (np.sort(np.r_[ts, ts + SECOND]), np.full(60, 100.0))
    fills = pd.DataFrame({"ts": ts, "side": 1, "price": 99.99, "mid": 100.0, "favour": math.nan})
    table = maker.evaluate(fills, mids, maker.MakerSettings(horizons_s=(1.0,))).set_index("group")
    assert table.loc["all", "fills"] == 30
    assert table.loc["neutral", "fills"] == table.loc["neutral", "n_infer"] == 0
