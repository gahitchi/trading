"""Guarded historical marks value existing quantities; intrahour envelopes do not invent liquidations."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lab import carry_collateral as cc
from lab import carry_marks as cm
from lab import carry_study as cs
from lab.trials import InputEvidence
from tests.test_carry import T0, market
from tests.test_carry_collateral import scenario
from tests.test_carry_ledger import path


def settings(**changes):
    return scenario(collateral_valuation="mark-ohlc", **changes)


def attach(frame, *, short=None, long=None):
    for leg, values in (("short", short), ("long", long)):
        values = [[100, 100, 100, 100]] * len(frame) if values is None else values
        for i, name in enumerate(cm.COLUMNS):
            frame[f"mark_{name}_{leg}"] = np.array(values, dtype=float)[:, i]
    frame.attrs["mark_model"] = cm.MODEL
    return frame


def ledger(s, weights=(0, 1, 1, 0), prices=None, **rates):
    prices = prices if prices is not None else [100] * len(weights)
    h = path(prices, prices, **rates)
    return attach(cs.backtest(h, s, w=pd.Series(weights, index=h.index)))


def write_marks(root, index, *, venues=cm.cd.VENUES, prices=None):
    values = np.full((len(index), 4), 100.0) if prices is None else np.asarray(prices, dtype=float)
    frame = pd.DataFrame(values, columns=cm.COLUMNS, index=pd.DatetimeIndex(index, name="ts"))
    for venue in venues:
        cm._publish(cm.path_of(root, venue, "BTCUSDT"), frame, venue, "BTCUSDT")


def poison_after(root, venue, boundary):
    file = cm.path_of(root, venue, "BTCUSDT")
    table = pq.read_table(file)
    frame = table.to_pandas()
    frame.loc[frame.ts >= boundary, "high"] = np.nan
    table = pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata(table.schema.metadata)
    pq.write_table(table, file)


def test_trade_price_flatness_does_not_hide_an_adverse_mark_close_before_exit():
    s = settings()
    frame = ledger(s)
    frame.loc[frame.index[3], ["mark_high_short", "mark_close_short"]] = 130
    report = cc.collateral_report({"X": frame}, s)
    short = report["per_symbol"]["X"]["short"]
    assert frame.net.sum() == 0
    assert short["first_breach"]["stage"] == "before_decision"
    assert short["first_breach"]["equity"] == pytest.approx(-0.1)
    assert short["first_breach"]["requirement"] == pytest.approx(0.13)
    assert short["terminal_hypothetical_equity"] == pytest.approx(0.2)  # exit realizes at TRADE price 100
    assert report["model"] == cc.MARK_MODEL and not report["qualified"]


def test_an_intrahour_rally_can_exhaust_the_short_even_when_all_hour_end_balances_are_healthy():
    s = settings()
    frame = ledger(s)
    frame.loc[frame.index[2], "mark_high_short"] = 140
    short = cc.collateral_report({"X": frame}, s)["per_symbol"]["X"]["short"]
    assert short["first_breach"] is None
    envelope = short["interval_stress"]
    assert envelope["first_envelope_breach"]["from"] == frame.index[1].isoformat()
    assert envelope["first_envelope_breach"]["through"] == frame.index[2].isoformat()
    assert envelope["minimum_headroom"] == pytest.approx(-0.34)
    assert envelope["ex_post_initial_collateral_boundary"] == pytest.approx(0.54)
    assert short["ex_post_stress_collateral_boundary"] == pytest.approx(0.54)


def test_a_long_perpetual_uses_its_low_mark_even_if_the_close_recovers():
    s = settings(mode="cross", collateral_long=0.2, maintenance_long=0.1)
    frame = ledger(s)
    frame.loc[frame.index[2], "mark_low_long"] = 60
    detail = cc.collateral_report({"X": frame}, s)["per_symbol"]["X"]["long"]
    assert detail["first_breach"] is None
    assert detail["interval_stress"]["minimum_headroom"] == pytest.approx(-0.26)


def test_new_entry_cannot_inherit_the_previous_hours_extreme_or_funding():
    s = settings()
    frame = ledger(s, weights=(0, 0, 1, 1))
    frame.loc[frame.index[2], "mark_high_short"] = 1e6
    intervals = cc.mark_interval_path(frame, s)
    assert not intervals.loc[frame.index[2], "exposed_short"]
    assert intervals.loc[frame.index[2], "pnl_short"] == 0
    assert not intervals.envelope_breach_short.any()


@pytest.mark.parametrize("direction,losing_leg", [(1, "short"), (-1, "long")])
def test_reversal_diagnostics_use_old_quantities_for_the_interval_and_new_quantities_after_trade(direction, losing_leg):
    s = settings(mode="cross", collateral_long=0.2, maintenance_long=0.1)
    frame = ledger(s, weights=(0, direction, -direction, -direction))
    frame.loc[frame.index[2], f"mark_high_{losing_leg}"] = 140
    intervals = cc.mark_interval_path(frame, s)
    assert intervals.loc[frame.index[2], f"envelope_breach_{losing_leg}"]
    assert intervals.loc[frame.index[3], f"headroom_{losing_leg}"] == pytest.approx(0.1)


def test_an_entry_trade_mark_gap_changes_equity_and_requirement_immediately():
    s = settings()
    frame = ledger(s)
    frame.loc[frame.index[1], ["mark_high_short", "mark_close_short"]] = 120
    checks = cc.collateral_path(frame, s)
    assert checks.loc[(frame.index[1], "before_decision"), "equity_short"] == 0.2
    assert checks.loc[(frame.index[1], "after_trade"), "equity_short"] == pytest.approx(0)
    assert checks.loc[(frame.index[1], "after_trade"), "requirement_short"] == pytest.approx(0.12)


@pytest.mark.parametrize("mode", ["cash", "cross"])
def test_mark_equity_reconciles_to_independent_cash_transactions_and_marked_inventory(mode):
    s = settings(mode=mode, maintenance_long=0 if mode == "cash" else 0.1, perp_fee_bps=7, spot_fee_bps=11)
    prices = [100, 103, 109, 121, 108, 112, 95, 101]
    weights = [0, 1, 1, 0, -1 if mode == "cross" else 1, -1 if mode == "cross" else 1, 1, 0]
    frame = ledger(s, weights=weights, prices=prices)
    marks = np.array(prices) + np.array([0, -2, 5, -3, 6, -4, 2, -1])
    for leg in ("long", "short"):
        for name in cm.COLUMNS:
            frame[f"mark_{name}_{leg}"] = marks
    checks = cc.collateral_path(frame, s)
    for leg, sign in (("long", 1), ("short", -1)):
        cash, qty, previous = getattr(s, f"collateral_{leg}"), 0, 0
        fee = 11 if mode == "cash" and leg == "long" else 7
        reference = []
        for price, mark, weight in zip(prices, marks, weights, strict=True):
            value = price if mode == "cash" and leg == "long" else mark
            reference.append(cash + qty * value)
            if weight != previous:
                new = sign * weight / price
                cash -= (new - qty) * price + abs(new - qty) * price * fee / 10000
                qty, previous = new, weight
            reference.append(cash + qty * value)
        assert checks[f"equity_{leg}"].to_numpy() == pytest.approx(reference, abs=1e-12)


@pytest.mark.parametrize("direction", [1, -1])
def test_offsetting_funding_events_cannot_hide_the_gross_debit_envelope(direction):
    s = settings(mode="cross", collateral_long=0.2, maintenance_long=0.1)
    index = path([100] * 4, [100] * 4).index
    rates = pd.Series([0.15, -0.15], index=pd.DatetimeIndex([index[2] - pd.Timedelta(minutes=30), index[2]]))
    frame = ledger(s, weights=(0, direction, direction, direction), long_rates=rates, short_rates=rates)
    assert frame.funding.sum() == pytest.approx(0)
    for leg in ("long", "short"):
        assert frame.loc[index[2], f"funding_debit_{leg}"] == pytest.approx(-0.15)
        detail = cc.collateral_report({"X": frame}, s)["per_symbol"]["X"][leg]
        assert detail["first_breach"] is None
        assert detail["interval_stress"]["first_envelope_breach"]["headroom"] == pytest.approx(-0.05)


@pytest.mark.parametrize("damage", ["missing", "nan", "negative", "bounds", "identity", "debit", "grid"])
def test_unverified_or_invalid_exposed_marks_make_the_diagnostic_unavailable(damage):
    s = settings()
    frame = ledger(s)
    if damage == "missing":
        frame = frame.drop(columns="mark_open_short")
    elif damage == "identity":
        frame.attrs.pop("mark_model")
    elif damage == "grid":
        frame.index = pd.DatetimeIndex(
            [
                frame.index[0],
                frame.index[1],
                frame.index[2] + pd.Timedelta(hours=2),
                frame.index[3] + pd.Timedelta(hours=2),
            ]
        )
    elif damage == "debit":
        frame.loc[frame.index[2], "funding_debit_short"] = 0.01
    else:
        frame.loc[frame.index[2], "mark_high_short"] = {"nan": np.nan, "negative": -1, "bounds": 1}[damage]
    with pytest.raises(ValueError, match=r"mark|OHLC|grid"):
        cc.collateral_report({"X": frame}, s)


def test_missing_marks_while_flat_do_not_create_a_solvency_pass_or_block_later_positions():
    s = settings()
    frame = ledger(s, weights=(0, 0, 1, 1))
    frame.loc[frame.index[:2], [f"mark_{c}_short" for c in cm.COLUMNS]] = np.nan
    report = cc.collateral_report({"X": frame}, s)
    assert report["available"] and not report["qualified"]
    assert report["per_symbol"]["X"]["short"]["interval_stress"]["exposed_hours"] == 1


def test_an_entirely_flat_run_without_marks_has_no_observed_exposure_and_cannot_qualify():
    s = settings()
    frame = ledger(s, weights=(0, 0, 0, 0))
    frame.loc[:, [f"mark_{c}_short" for c in cm.COLUMNS]] = np.nan
    report = cc.collateral_report({"X": frame}, s)
    interval = report["per_symbol"]["X"]["short"]["interval_stress"]
    assert interval["exposed_hours"] == 0 and interval["minimum_headroom"] is None
    assert not report["qualified"]


def test_valuation_changes_do_not_change_orders_quantities_or_unfunded_pnl():
    mark_settings = settings()
    old_settings = replace(mark_settings, collateral_valuation="trade-close")
    h = path([100, 100, 110, 100], [100, 100, 110, 100])
    w = pd.Series([0, 1, 1, 0], index=h.index)
    pd.testing.assert_frame_equal(cs.backtest(h, mark_settings, w=w), cs.backtest(h, old_settings, w=w))
    assert cs.params_of(mark_settings, ["X"]) != cs.params_of(old_settings, ["X"])


def test_mark_valuation_requires_explicit_budgets_and_supported_mode():
    with pytest.raises(ValueError, match="all four"):
        cs.CarrySettings(collateral_valuation="mark-ohlc")
    with pytest.raises(ValueError, match="collateral_valuation"):
        cs.CarrySettings(collateral_valuation="unknown")


def test_bounded_loader_filters_future_values_before_validation_and_preserves_nanosecond_inclusive_cutoffs(tmp_path):
    index = pd.date_range(T0, periods=5, freq="h")
    write_marks(tmp_path, index)
    poison_after(tmp_path, "binance", index[3])
    loaded = cm.load(tmp_path, "binance", "BTCUSDT", start=index[1], end=index[2] + pd.Timedelta(nanoseconds=1))
    assert loaded.index.tolist() == list(index[1:3])
    with pytest.raises(ValueError, match="mark history"):
        cm.load(tmp_path, "binance", "BTCUSDT")


def test_research_never_reads_held_out_mark_values_or_changes_their_input_digest(tmp_path):
    market(tmp_path, days=12)
    index = pd.date_range(T0, periods=12 * 24 + 1, freq="h")
    write_marks(tmp_path, index)
    s, boundary = settings(lookback_h=24, entry_apr=0), T0 + pd.Timedelta(days=8)
    first_inputs = InputEvidence()
    first, _ = cs.run(tmp_path, ["BTCUSDT"], s, boundary, evidence=first_inputs)
    poison_after(tmp_path, "binance", boundary)
    second_inputs = InputEvidence()
    second, _ = cs.run(tmp_path, ["BTCUSDT"], s, boundary, evidence=second_inputs)
    pd.testing.assert_frame_equal(first["BTCUSDT"], second["BTCUSDT"])
    assert first_inputs.snapshot() == second_inputs.snapshot()
    streams = {s["name"]: s for s in first_inputs.snapshot()["streams"]}
    assert streams["BTCUSDT/mark_short/high"]["last_ns"] < boundary.value


def test_a_consumed_mark_change_alters_provenance_and_diagnostics(tmp_path):
    market(tmp_path, days=8)
    index = pd.date_range(T0, periods=8 * 24 + 1, freq="h")
    write_marks(tmp_path, index)
    s, boundary = settings(lookback_h=24, entry_apr=0), T0 + pd.Timedelta(days=9)
    first_inputs = InputEvidence()
    first, _ = cs.run(tmp_path, ["BTCUSDT"], s, boundary, evidence=first_inputs)
    values = np.full((len(index), 4), 100.0)
    values[48, 1] = 200
    write_marks(tmp_path, index, prices=values)
    second_inputs = InputEvidence()
    second, _ = cs.run(tmp_path, ["BTCUSDT"], s, boundary, evidence=second_inputs)
    assert first_inputs.snapshot() != second_inputs.snapshot()
    assert cc.collateral_report(first, s) != cc.collateral_report(second, s)
    pd.testing.assert_series_equal(first["BTCUSDT"].net, second["BTCUSDT"].net)


def test_fresh_holdout_ignores_earlier_marks_and_the_reserved_future_cutoff(tmp_path):
    market(tmp_path, days=12)
    index = pd.date_range(T0, periods=12 * 24 + 1, freq="h")
    write_marks(tmp_path, index)
    s = settings(lookback_h=24, entry_apr=0)
    boundary, end = T0 + pd.Timedelta(days=8), T0 + pd.Timedelta(days=10)
    first_inputs = InputEvidence()
    first, _ = cs.run(tmp_path, ["BTCUSDT"], s, boundary, holdout=True, end=end, evidence=first_inputs)
    poison_after(tmp_path, "binance", end + pd.Timedelta(hours=1))
    file = cm.path_of(tmp_path, "binance", "BTCUSDT")
    table = pq.read_table(file)
    raw = table.to_pandas()
    raw.loc[raw.ts < boundary, "low"] = -1
    pq.write_table(pa.Table.from_pandas(raw, preserve_index=False).replace_schema_metadata(table.schema.metadata), file)
    second_inputs = InputEvidence()
    second, _ = cs.run(tmp_path, ["BTCUSDT"], s, boundary, holdout=True, end=end, evidence=second_inputs)
    pd.testing.assert_frame_equal(first["BTCUSDT"], second["BTCUSDT"])
    assert first_inputs.snapshot() == second_inputs.snapshot()
    stream = next(s for s in first_inputs.snapshot()["streams"] if s["name"] == "BTCUSDT/mark_short/high")
    assert stream["first_ns"] == boundary.value and stream["last_ns"] == end.value
    intervals = cc.mark_interval_path(second["BTCUSDT"], s)
    assert not intervals.iloc[0].exposed_short


@pytest.mark.parametrize("mode,missing_venue", [("cash", "binance"), ("cross", "binance"), ("cross", "bybit")])
def test_missing_required_venue_marks_cannot_leave_a_successfully_evaluated_run(mode, missing_venue, tmp_path):
    market(tmp_path, days=8)
    index = pd.date_range(T0, periods=8 * 24 + 1, freq="h")
    write_marks(tmp_path, index, venues=[v for v in cm.cd.VENUES if v != missing_venue])
    with pytest.raises(ValueError, match="historical mark"):
        cs.run(tmp_path, ["BTCUSDT"], settings(mode=mode, lookback_h=24, entry_apr=0), T0 + pd.Timedelta(days=9))


def test_default_trade_close_study_does_not_read_auxiliary_marks(tmp_path, monkeypatch):
    market(tmp_path, days=8)
    monkeypatch.setattr(cm, "load", lambda *args, **kwargs: pytest.fail("default valuation must not load marks"))
    assert cs.run(tmp_path, ["BTCUSDT"], cs.CarrySettings(lookback_h=24), T0 + pd.Timedelta(days=9))[0]


def test_cli_persists_mark_assumptions_provenance_and_envelopes_for_both_counted_runs(tmp_path, capsys):
    from lab.cli import main
    from lab.trials import history
    from tests.conftest import REPO

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    root = tmp_path / "data" / "carry"
    market(root, days=400)
    write_marks(root, pd.date_range(T0, periods=400 * 24, freq="h"))
    assert (
        main(
            [
                "--config",
                str(tmp_path / "lab.toml"),
                "carry",
                "study",
                "--symbols",
                "BTCUSDT",
                "--collateral-valuation",
                "mark-ohlc",
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
    assert len(rows) == 2
    for row in rows:
        report = row["result"]["collateral_scenario"]
        assert row["params"]["collateral_model"] == report["model"] == cc.MARK_MODEL
        assert report["valuation"] == "mark-ohlc" and report["available"] and not report["qualified"]
        assert "interval_stress" in report["per_symbol"]["BTCUSDT"]["short"]
        assert len([s for s in row["inputs"]["streams"] if "/mark_short/" in s["name"]]) == 4
    out = capsys.readouterr().out
    assert "mark-ohlc valuation" in out and "not an observed liquidation" in out


def test_missing_marks_remain_a_durable_failed_holdout_and_cannot_be_retried_without_a_reason(tmp_path):
    from lab.cli import main
    from lab.trials import history
    from tests.conftest import REPO

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    market(tmp_path / "data" / "carry", days=400)
    args = [
        "--config",
        str(tmp_path / "lab.toml"),
        "carry",
        "study",
        "--symbols",
        "BTCUSDT",
        "--holdout",
        "--collateral-valuation",
        "mark-ohlc",
        "--collateral-long",
        "1.1",
        "--collateral-short",
        "0.2",
        "--maintenance-long",
        "0",
        "--maintenance-short",
        "0.1",
    ]
    with pytest.raises(SystemExit, match="historical mark"):
        main(args)
    attempts = history(tmp_path / "results")
    assert len(attempts) == 1 and attempts[0]["status"] == "failed" and attempts[0]["holdout"]
    assert any(s["name"] == "BTCUSDT/mark_short/close" and s["records"] == 0 for s in attempts[0]["inputs"]["streams"])
    with pytest.raises(SystemExit, match="again"):
        main(args)
    assert history(tmp_path / "results") == attempts


def test_mark_currency_overflow_is_unavailable_instead_of_a_successful_margin_report():
    s = settings()
    frame = ledger(s, prices=[1e-100] * 4)
    for column in cm.COLUMNS:
        frame[f"mark_{column}_short"] = 1e308
    with np.errstate(over="ignore", invalid="ignore"), pytest.raises(ValueError, match="nonfinite collateral"):
        cc.collateral_report({"X": frame}, s)


@pytest.mark.parametrize("start,end", [(T0.tz_localize(None), None), (None, pd.NaT), (T0 + pd.Timedelta(hours=1), T0)])
def test_invalid_loader_bounds_fail_even_when_the_file_does_not_exist(tmp_path, start, end):
    with pytest.raises(ValueError, match=r"bounds|start <= end"):
        cm.load(tmp_path, "binance", "BTCUSDT", start=start, end=end)
