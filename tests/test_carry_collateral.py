"""Collateral is a separate budget constraint, never a division of the paired P&L by leverage."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from lab import carry_study as cs
from lab.carry_collateral import MODEL, collateral_path, collateral_report
from tests.test_carry_ledger import path


def scenario(**changes):
    return cs.CarrySettings(
        **{
            "collateral_long": 1.1,
            "collateral_short": 0.2,
            "maintenance_long": 0.0,
            "maintenance_short": 0.1,
            "perp_fee_bps": 0,
            "spot_fee_bps": 0,
            "slippage_bps": 0,
            **changes,
        }
    )


def ledger(settings, prices, weights, **rates):
    h = path(prices, prices, **rates)
    return cs.backtest(h, settings, w=pd.Series(weights, index=h.index))


def test_a_fully_hedged_rally_still_exhausts_the_separate_short_budget_before_exit():
    settings = scenario()
    frame = ledger(settings, [100, 100, 130, 130], [0, 1, 0, 0])
    assert frame.net.sum() == pytest.approx(0)  # spot profit cannot rescue the perpetual
    report = collateral_report({"X": frame}, settings)
    assert report["available"] and not report["qualified"]
    detail = report["per_symbol"]["X"]
    assert detail["long"]["first_breach"] is None
    assert detail["long"]["ex_post_initial_collateral_boundary"] == pytest.approx(1)
    short = detail["short"]
    assert short["first_breach"]["stage"] == "before_decision"
    assert short["first_breach"]["time"] == frame.index[2].isoformat()
    assert short["first_breach"]["equity"] == pytest.approx(-0.1)
    assert short["first_breach"]["requirement"] == pytest.approx(0.13)
    assert short["ex_post_initial_collateral_boundary"] == pytest.approx(0.43)
    assert short["terminal_hypothetical_equity"] == pytest.approx(-0.1)


def test_insufficient_spot_cash_is_flagged_on_entry_and_fees_cannot_be_borrowed():
    settings = scenario(collateral_long=1, spot_fee_bps=10)
    frame = ledger(settings, [100, 100, 100], [0, 1, 1])
    long = collateral_report({"X": frame}, settings)["per_symbol"]["X"]["long"]
    assert long["first_breach"]["stage"] == "after_trade"
    assert long["minimum_headroom"] == pytest.approx(-0.001)
    assert long["ex_post_initial_collateral_boundary"] == pytest.approx(1.001)


def test_exactly_funded_spot_cash_is_allowed_but_perpetual_threshold_equality_breaches():
    settings = scenario(collateral_long=1, collateral_short=0.125, maintenance_short=0.125)
    frame = ledger(settings, [100, 100, 100], [0, 1, 1])
    result = collateral_report({"X": frame}, settings)["per_symbol"]["X"]
    assert result["long"]["first_breach"] is None
    assert result["short"]["first_breach"]["stage"] == "after_trade"
    assert result["short"]["first_breach"]["headroom"] == 0


def test_negative_funding_can_exhaust_margin_without_any_price_change():
    settings = scenario()
    index = path([100] * 4, [100] * 4).index
    frame = ledger(settings, [100] * 4, [0, 1, 1, 1], funding=pd.Series([-0.15], index=index[2:3]))
    result = collateral_report({"X": frame}, settings)["per_symbol"]["X"]["short"]
    assert result["first_breach"]["time"] == index[2].isoformat()
    assert result["first_breach"]["pnl"] == pytest.approx(-0.15)
    assert result["ex_post_initial_collateral_boundary"] == pytest.approx(0.25)


@pytest.mark.parametrize("direction", [-1, 1])
def test_cross_direction_reversal_does_not_move_cash_between_reference_venues(direction):
    settings = scenario(mode="cross", collateral_long=0.2, maintenance_long=0.1)
    frame = ledger(settings, [100, 100, 130, 130], [0, direction, -direction, -direction])
    report = collateral_report({"X": frame}, settings)["per_symbol"]["X"]
    loser, winner = ("short", "long") if direction == 1 else ("long", "short")
    assert report[loser]["first_breach"]["stage"] == "before_decision"
    assert report[loser]["terminal_hypothetical_equity"] == pytest.approx(-0.1)
    assert report[winner]["first_breach"] is None
    assert report[winner]["terminal_hypothetical_equity"] == pytest.approx(0.5)


def test_losses_and_costs_survive_flat_intervals_and_reentries_without_new_deposits():
    settings = scenario(perp_fee_bps=10)
    frame = ledger(settings, [100, 100, 105, 105, 105, 105], [0, 1, 0, 0, 1, 1])
    result = collateral_path(frame, settings)
    assert result.loc[(frame.index[4], "before_decision"), "equity_short"] == pytest.approx(0.14795)
    assert result.loc[(frame.index[4], "after_trade"), "equity_short"] == pytest.approx(0.14695)


@pytest.mark.parametrize("mode", ["cash", "cross"])
def test_balances_match_independent_separate_cash_and_marked_inventory_accounts(mode):
    settings = scenario(mode=mode, maintenance_long=0 if mode == "cash" else 0.07, perp_fee_bps=7, spot_fee_bps=11)
    prices = [100, 103, 109, 121, 108, 112, 95, 101]
    weights = [0, 1, 1, 0, -1 if mode == "cross" else 1, -1 if mode == "cross" else 1, 1, 0]
    index = path(prices, prices).index
    rates = pd.Series([0, 0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.01], index=index)
    long_rates = rates if mode == "cross" else rates * 0
    frame = ledger(settings, prices, weights, long_rates=long_rates, short_rates=-rates)
    result = collateral_path(frame, settings)
    for leg, sign, funding in (("long", 1, long_rates), ("short", -1, -rates)):
        cash, quantity, previous = getattr(settings, f"collateral_{leg}"), 0.0, 0
        fee = 11 if leg == "long" and mode == "cash" else 7
        reference = []
        for i, (price, weight) in enumerate(zip(prices, weights, strict=True)):
            cash -= quantity * price * funding.iloc[i]
            reference.append(cash + quantity * price)
            if weight != previous:
                new = sign * weight / price
                cash -= (new - quantity) * price + abs(new - quantity) * price * fee / 10000
                quantity, previous = new, weight
            reference.append(cash + quantity * price)
        assert result[f"equity_{leg}"].to_numpy() == pytest.approx(reference, abs=1e-12)
    assert result.index.tolist() == [(t, s) for t in index for s in ("before_decision", "after_trade")]


def test_holdout_starts_with_fresh_budgets_and_quantities_and_has_no_research_losses():
    settings = scenario()
    h = path([100, 100, 150, 150, 150], [100, 100, 150, 150, 150])
    frame = cs.backtest(h, settings, eval_from=h.index[3], w=pd.Series([0, 1, 1, 1, 1], index=h.index))
    report = collateral_report({"X": frame}, settings)["per_symbol"]["X"]
    assert report["short"]["first_breach"] is None
    assert report["short"]["terminal_hypothetical_equity"] == 0.2


def test_other_symbols_and_top_k_slots_never_cover_a_symbol_loss():
    settings = scenario(top=1)
    loser = ledger(settings, [100, 100, 130], [0, 1, 1])
    winner = ledger(settings, [100, 100, 80], [0, 1, 1])
    single = collateral_report({"X": loser}, settings)["per_symbol"]["X"]
    combined = cs.summary({"X": loser, "Y": winner}, slots=1, settings=settings)["collateral_scenario"]
    assert combined["per_symbol"]["X"] == single
    assert "no pooling or top-K scaling" in combined["unit"]


@pytest.mark.parametrize("field", ["collateral_long", "collateral_short", "maintenance_long", "maintenance_short"])
@pytest.mark.parametrize("value", [None, -1, float("nan"), float("inf")])
def test_partial_and_invalid_scenarios_fail_before_evaluation(field, value):
    with pytest.raises(ValueError, match="all four"):
        scenario(**{field: value})


@pytest.mark.parametrize("field", ["maintenance_long", "maintenance_short"])
def test_maintenance_rates_are_fractions_below_one(field):
    with pytest.raises(ValueError, match="less than one"):
        scenario(mode="cross", **{field: 1})


def test_cash_spot_cannot_be_given_a_fictitious_maintenance_discount():
    with pytest.raises(ValueError, match="fully funded"):
        scenario(maintenance_long=0.01)


def test_no_implicit_margin_defaults_and_scenario_changes_are_counted_configurations():
    settings = cs.CarrySettings()
    report = collateral_report({}, settings)
    assert not report["configured"] and not report["available"] and not report["qualified"]
    assert report["assumptions"] is None
    assert cs.params_of(scenario(), ["X"]) != cs.params_of(scenario(collateral_short=0.3), ["X"])
    assert cs.params_of(scenario(), ["X"])["collateral_model"] == MODEL


@pytest.mark.parametrize("damage", ["identity", "mode", "missing", "nonfinite", "duplicate", "negative-cost"])
def test_missing_or_invalid_ledger_cannot_claim_collateral_evidence(damage):
    settings = scenario()
    frame = ledger(settings, [100, 100, 130], [0, 1, 1])
    if damage == "identity":
        frame.attrs.clear()
    elif damage == "mode":
        frame.attrs["mode"] = "cross"
    elif damage == "missing":
        frame = frame.drop(columns="basis_long")
    elif damage == "nonfinite":
        frame.iloc[2, frame.columns.get_loc("funding_long")] = np.inf
    elif damage == "duplicate":
        frame.index = pd.DatetimeIndex([frame.index[0]] * len(frame))
    else:
        frame.loc[frame.index[1], "cost_long"] = -1
    with pytest.raises(ValueError, match="ledger"):
        collateral_report({"X": frame}, settings)


def test_cli_persists_explicit_scenarios_in_both_counted_cash_evaluations(tmp_path, capsys):
    from lab.cli import main
    from lab.trials import history
    from tests.conftest import REPO
    from tests.test_carry import market

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    market(tmp_path / "data" / "carry", days=400)
    assert (
        main(
            [
                "--config",
                str(tmp_path / "lab.toml"),
                "carry",
                "study",
                "--symbols",
                "BTCUSDT",
                "--collateral-long",
                "1.1",
                "--collateral-short",
                "0.2",
                "--maintenance-long",
                "0",
                "--maintenance-short",
                "0.1",
            ]
        )
        == 0
    )
    rows = history(tmp_path / "results")
    assert len(rows) == 2  # candidate plus the automatically counted always-in comparison
    for row in rows:
        report = row["result"]["collateral_scenario"]
        assert report["configured"] and report["available"] and not report["qualified"]
        assert report["assumptions"]["collateral_short"] == row["params"]["collateral_short"] == 0.2
        assert report["per_symbol"]["BTCUSDT"]["long"]["market"] == "spot"
    out = capsys.readouterr().out
    assert "counterfactual" in out and "first budget breach" in out and "ex-post collateral boundary" in out


def test_price_rescaling_keeps_collateral_boundaries_and_breach_times_unchanged():
    settings = scenario()
    first = ledger(settings, [100, 100, 130], [0, 1, 1])
    second = ledger(settings, [1000, 1000, 1300], [0, 1, 1])
    assert collateral_report({"X": first}, settings) == collateral_report({"X": second}, settings)


def test_no_breach_is_never_reported_as_a_liquidation_or_profitability_pass():
    settings = replace(scenario(), collateral_short=10)
    frame = ledger(settings, [100, 100, 130], [0, 1, 1])
    report = collateral_report({"X": frame}, settings)
    assert report["per_symbol"]["X"]["short"]["first_breach"] is None
    assert not report["qualified"]
    assert "not funded returns" in report["limitation"]


def test_later_prices_cannot_rewrite_earlier_balances_or_retroactively_fund_a_position():
    settings = scenario()
    first = ledger(settings, [100, 100, 105, 110, 120], [0, 1, 1, 1, 1])
    second = ledger(settings, [100, 100, 105, 90, 80], [0, 1, 1, 1, 1])
    pd.testing.assert_frame_equal(collateral_path(first, settings).iloc[:6], collateral_path(second, settings).iloc[:6])


def test_nonfinite_cumulative_arithmetic_is_unavailable_even_when_each_observation_is_finite():
    settings = scenario()
    frame = ledger(settings, [100] * 4, [0, 1, 1, 1])
    frame["funding_short"] = 1e308
    with np.errstate(over="ignore"), pytest.raises(ValueError, match="nonfinite collateral"):
        collateral_report({"X": frame}, settings)


def test_partial_cli_scenario_does_not_read_data_or_reserve_a_trial(tmp_path, monkeypatch):
    from lab.cli import main
    from tests.conftest import REPO

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())

    def unexpected(*args, **kwargs):
        pytest.fail("partial scenario must fail before boundary discovery or data loading")

    monkeypatch.setattr(cs, "boundary", unexpected)
    with pytest.raises(SystemExit, match="all four"):
        main(["--config", str(tmp_path / "lab.toml"), "carry", "study", "--collateral-short", "0.2"])
    assert not list((tmp_path / "results").glob("research_attempts/*.json"))
