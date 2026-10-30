"""Carry cash flows must be supported by positions with explicit, unchanged base quantities."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from lab import carry_study as cs
from tests.test_carry import flat_legs


def path(long, short, *, funding=None, long_rates=None, short_rates=None):
    lg = flat_legs(hours=len(long), rate=0)
    return cs.hourly(
        replace(
            lg,
            long=pd.Series(long, index=lg.long.index, dtype=float),
            short=pd.Series(short, index=lg.short.index, dtype=float),
            funding=lg.funding if funding is None else funding,
            funding_long=long_rates,
            funding_short=short_rates,
        )
    )


def test_a_held_leg_keeps_its_quantity_instead_of_resetting_notional_each_hour():
    h = path([100, 100, 110, 121, 121], [100] * 5)
    result = cs.backtest(h, cs.CarrySettings(), w=pd.Series([0, 1, 1, 1, 0], index=h.index))
    assert result["basis"].sum() == pytest.approx(0.21)  # 0.01 units * (121 - 100), not 10% + 10%


def test_exit_fees_use_the_actual_notional_of_the_held_quantities():
    h = path([100, 100, 110, 121, 121], [100] * 5)
    settings = cs.CarrySettings()
    result = cs.backtest(h, settings, w=pd.Series([0, 1, 1, 1, 0], index=h.index))
    assert result.iloc[-1]["cost"] == pytest.approx(1.21 * 11 / 10000 + 1 * 6 / 10000)


def test_funding_scales_with_the_current_value_of_the_held_short_quantity():
    lg = flat_legs(hours=5, rate=0)
    funding = pd.Series(0.01, index=lg.long.index[2:4])
    h = path([100] * 5, [100, 100, 110, 121, 121], funding=funding)
    result = cs.backtest(h, cs.CarrySettings(), w=pd.Series([0, 1, 1, 1, 0], index=h.index))
    assert result["funding"].sum() == pytest.approx(0.01 * (1.1 + 1.21))


def test_quantities_do_not_trade_on_price_changes_and_terminal_inventory_is_retained():
    h = path([100, 100, 110, 121], [100] * 4)
    result = cs.backtest(h, cs.CarrySettings(), w=pd.Series([0, 1, 1, 1], index=h.index))
    assert result["qty_long"].tolist() == pytest.approx([0, 0.01, 0.01, 0.01])
    assert result["qty_short"].tolist() == pytest.approx([0, -0.01, -0.01, -0.01])
    assert result["turnover_long"].tolist() == pytest.approx([0, 1, 0, 0])
    assert result["turnover_short"].tolist() == pytest.approx([0, 1, 0, 0])
    ledger = cs.leg_ledger_summary({"X": result})
    assert ledger["available"] and not ledger["qualified"]
    assert ledger["funding_valuation"] == cs.FUNDING_VALUATION
    detail = ledger["per_symbol"]["X"]
    assert detail["terminal"]["qty_long"] == pytest.approx(0.01)
    assert detail["terminal"]["notional_long"] == pytest.approx(1.21)
    assert detail["peak_gross_notional"] == pytest.approx(2.21)
    assert detail["peak_abs_net_notional"] == pytest.approx(0.21)
    assert result["cost"].sum() == pytest.approx(cs.CarrySettings().cost)  # no shutdown sale


def test_equal_entry_notionals_do_not_pretend_that_unequal_prices_have_equal_base_quantities():
    h = path([100, 100, 110, 110], [125, 125, 150, 150])
    result = cs.backtest(h, cs.CarrySettings(), w=pd.Series([0, 1, 1, 0], index=h.index))
    assert result.iloc[1]["qty_long"] == pytest.approx(0.01)
    assert result.iloc[1]["qty_short"] == pytest.approx(-0.008)
    assert result["basis_long"].sum() == pytest.approx(0.10)
    assert result["basis_short"].sum() == pytest.approx(-0.20)
    detail = cs.leg_ledger_summary({"X": result})["per_symbol"]["X"]
    assert detail["peak_abs_net_base_qty"] == pytest.approx(0.002)


@pytest.mark.parametrize("direction", [-1, 1])
def test_cross_funding_keeps_both_venue_cash_flows_instead_of_only_the_rate_difference(direction):
    lg = flat_legs(hours=4)
    rates = pd.Series(0.01, index=lg.long.index[2:])
    h = path([100, 100, 110, 130], [125, 125, 150, 200], long_rates=rates, short_rates=rates)
    assert (h["funding"] == 0).all()  # equal rates, different held position values
    result = cs.backtest(
        h, cs.CarrySettings(mode="cross"), w=pd.Series([0, direction, direction, direction], index=h.index)
    )
    assert result["funding_long"].sum() == pytest.approx(-direction * (1.1 + 1.3) * 0.01)
    assert result["funding_short"].sum() == pytest.approx(direction * (1.2 + 1.6) * 0.01)
    assert result["funding"].sum() == pytest.approx(direction * 0.004)


def test_reversal_closes_old_quantities_and_opens_new_equal_notionals_after_settlement():
    h = path([100, 100, 110, 110], [100, 100, 120, 120])
    result = cs.backtest(h, cs.CarrySettings(mode="cross"), w=pd.Series([0, 1, -1, 0], index=h.index))
    flip = result.iloc[2]
    assert flip["qty_long"] == pytest.approx(-1 / 110)
    assert flip["qty_short"] == pytest.approx(1 / 120)
    assert flip["turnover_long"] == pytest.approx(2.1)
    assert flip["turnover_short"] == pytest.approx(2.2)
    assert flip["cost"] == pytest.approx((2.1 + 2.2) * 6 / 10000)
    assert flip["basis"] == pytest.approx(-0.1)  # old direction earns the move into the reversal


@pytest.mark.parametrize("mode", ["cash", "cross"])
def test_incremental_pnl_matches_an_independent_cash_transaction_and_inventory_reference(mode):
    prices = [(100, 110), (101, 112), (108, 109), (111, 120), (98, 113), (105, 110), (109, 125), (115, 121)]
    weights = [0, 1, 1, 0, -1 if mode == "cross" else 1, -1 if mode == "cross" else 1, 1, 0]
    index = flat_legs(hours=len(prices)).long.index
    long_rates = (
        pd.Series([0, 0.01, 0.02, -0.01, 0.01, 0.03, -0.01, 0.01], index=index)
        if mode == "cross"
        else pd.Series(0.0, index=index)
    )
    short_rates = pd.Series([0, -0.01, 0.03, -0.02, 0.01, 0.02, 0.01, -0.01], index=index)
    h = path(*zip(*prices, strict=True), long_rates=long_rates, short_rates=short_rates)
    settings = cs.CarrySettings(mode=mode, perp_fee_bps=7, spot_fee_bps=11, slippage_bps=3)
    result = cs.backtest(h, settings, w=pd.Series(weights, index=index))
    cash, q_long, q_short, old_direction = 0.0, 0.0, 0.0, 0
    reference = []
    for i, ((p_long, p_short), direction) in enumerate(zip(prices, weights, strict=True)):
        cash -= q_long * p_long * long_rates.iloc[i] + q_short * p_short * short_rates.iloc[i]
        if direction != old_direction:
            new_long = direction / p_long
            new_short = -direction / p_short
            d_long, d_short = new_long - q_long, new_short - q_short
            cash -= d_long * p_long + d_short * p_short
            cash -= abs(d_long) * p_long * ((11 if mode == "cash" else 7) + 3) / 10000
            cash -= abs(d_short) * p_short * (7 + 3) / 10000
            q_long, q_short, old_direction = new_long, new_short, direction
        reference.append(cash + q_long * p_long + q_short * p_short)
    assert result["net"].cumsum().to_numpy() == pytest.approx(reference, abs=1e-12)


def test_holdout_restarts_with_fresh_quantities_instead_of_inheriting_a_research_position():
    h = path([100, 100, 110, 121, 130], [100, 100, 120, 140, 150])
    w = pd.Series([0, 1, 1, 1, 1], index=h.index)
    result = cs.backtest(h, cs.CarrySettings(), eval_from=h.index[3], w=w)
    first = result.iloc[0]
    assert first["qty_long"] == pytest.approx(1 / 121)
    assert first["qty_short"] == pytest.approx(-1 / 140)
    assert first["basis"] == first["funding"] == 0
    assert first["cost"] == pytest.approx(cs.CarrySettings().cost)


def test_quote_price_unit_rescaling_changes_quantities_but_not_normalized_cash_flows():
    def evaluate(scale):
        h = path(np.array([100, 100, 110, 121, 121]) * scale, np.array([100, 100, 105, 115, 115]) * scale)
        return cs.backtest(h, cs.CarrySettings(), w=pd.Series([0, 1, 1, 1, 0], index=h.index))

    original, scaled = evaluate(1), evaluate(10)
    for column in ("basis", "funding", "cost", "net", "notional_long", "notional_short"):
        assert original[column].to_numpy() == pytest.approx(scaled[column].to_numpy())
    assert scaled["qty_long"].to_numpy() == pytest.approx(original["qty_long"].to_numpy() / 10)


@pytest.mark.parametrize("fee", ["perp_fee_bps", "spot_fee_bps", "slippage_bps"])
@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf")])
def test_invalid_cost_assumptions_cannot_create_a_valid_ledger(fee, value):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        cs.CarrySettings(**{fee: value})


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_an_active_leg_with_invalid_prices_is_unavailable(value):
    h = path([100] * 4, [100] * 4)
    h.loc[h.index[2], "price_long"] = value
    with pytest.raises(ValueError, match="finite and positive"):
        cs.backtest(h, cs.CarrySettings(), w=pd.Series([0, 1, 1, 0], index=h.index))


@pytest.mark.parametrize("damage", ["negative-cash", "fractional", "missing", "different-grid"])
def test_invalid_external_position_paths_are_rejected(damage):
    h = path([100] * 4, [100] * 4)
    w = pd.Series([0, 1, 1, 0], index=h.index, dtype=float)
    if damage == "different-grid":
        w.index += pd.Timedelta(hours=1)
    else:
        w.iloc[1] = {"negative-cash": -1, "fractional": 0.5, "missing": float("nan")}[damage]
    with pytest.raises(ValueError, match="position path"):
        cs.backtest(h, cs.CarrySettings(), w=w)


def test_a_legacy_summary_cannot_claim_identified_leg_evidence():
    index = pd.date_range("2024-01-01", periods=3, freq="h", tz="UTC")
    frame = pd.DataFrame({"w": 0, "basis": 0, "funding": 0, "cost": 0, "net": 0}, index=index)
    summary = cs.summary({"X": frame})["leg_ledger"]
    assert not summary["available"] and not summary["qualified"]
    assert "missing identified leg ledger" in summary["per_symbol"]["X"]["reason"]


def test_cli_saves_per_leg_cash_flows_and_discloses_unqualified_close_proxy(tmp_path, capsys):
    from lab.cli import main
    from lab.trials import history
    from tests.conftest import REPO
    from tests.test_carry import market

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    market(tmp_path / "data" / "carry", days=400)
    assert (
        main(["--config", str(tmp_path / "lab.toml"), "carry", "study", "--mode", "cross", "--symbols", "BTCUSDT"]) == 0
    )
    row = history(tmp_path / "results")[0]
    assert row["params"]["leg_model"] == cs.LEG_MODEL
    assert row["params"]["funding_valuation"] == cs.FUNDING_VALUATION
    ledger = row["result"]["leg_ledger"]
    assert ledger["available"] and not ledger["qualified"]
    assert {"funding_long", "funding_short", "cost_long", "cost_short"} <= ledger["per_symbol"]["BTCUSDT"][
        "cashflows"
    ].keys()
    assert {"BTCUSDT/funding_long", "BTCUSDT/funding_short"} <= {s["name"] for s in row["inputs"]["streams"]}
    out = capsys.readouterr().out
    assert "hour-end close proxy" in out and "economics remain unqualified" in out


def test_common_funding_rate_changes_cannot_hide_behind_an_unchanged_rate_difference(tmp_path):
    from lab import carry_data as cd
    from lab.trials import InputEvidence
    from tests.test_carry import T0, market, write

    market(tmp_path, days=10, rate=0, bybit_rate=0)
    settings = cs.CarrySettings(mode="cross", lookback_h=24, entry_apr=-1, exit_apr=-2)
    boundary = T0 + pd.Timedelta(days=11)
    evidence = InputEvidence()
    first, _ = cs.run(tmp_path, ["BTCUSDT"], settings, boundary, evidence=evidence)
    for venue in ("binance", "bybit"):
        rates = cd.load(tmp_path, venue, "perp", "funding", "BTCUSDT")
        write(tmp_path, venue, "perp", "funding", "BTCUSDT", rates + 0.0001)
    updated = InputEvidence()
    second, _ = cs.run(tmp_path, ["BTCUSDT"], settings, boundary, evidence=updated)
    old_streams = {s["name"]: s for s in evidence.snapshot()["streams"]}
    new_streams = {s["name"]: s for s in updated.snapshot()["streams"]}
    assert old_streams["BTCUSDT/funding"] == new_streams["BTCUSDT/funding"]
    assert old_streams["BTCUSDT/funding_long"] != new_streams["BTCUSDT/funding_long"]
    assert old_streams["BTCUSDT/funding_short"] != new_streams["BTCUSDT/funding_short"]
    assert evidence.snapshot() != updated.snapshot()
    assert not np.allclose(first["BTCUSDT"]["funding"], second["BTCUSDT"]["funding"], rtol=0, atol=1e-12)
