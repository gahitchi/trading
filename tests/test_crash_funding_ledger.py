"""Synthetic funding-adjusted crash trade and durable CLI integration tests.

No live network or protected holdout market files are accessed.
"""
import json

import pandas as pd
import pytest

from lab import crash_execution, crash_funding
from lab import crash_funding_ledger as ledger
from lab import crash_study as study
from lab.cli import main as lab_main
from lab.trials import history
from tests.test_crash_funding import events, fake_get, setup


def evidence():
    source = events(hours=48)
    return {
        "plan": {
            "start": "2024-01-01",
            "holdout_start": "2024-01-03",
        },
        "events": [
            {key: value for key, value in row.items() if key != "symbol"}
            for row in source
        ],
    }


def one_trade(*, entry="2024-01-01T04:00:00Z", exit_="2024-01-01T20:00:00Z", reason="time"):
    entry_ref, exit_ref = 100.0, 95.0
    account = crash_execution.fill_accounting(-1, entry_ref, exit_ref, 5.0, 5.0)
    return pd.DataFrame({
        "signal": [pd.Timestamp(entry) - pd.Timedelta(minutes=2)],
        "entry": [pd.Timestamp(entry)],
        "exit": [pd.Timestamp(exit_)],
        "gross": [account["reference_gross"]],
        "net": [account["net"]],
        "weight": [1.0],
        "reason": [reason],
        "entry_reference": [entry_ref],
        "exit_reference": [exit_ref],
        "fee_cost": [account["fee_cost"]],
        "slippage_cost": [account["slippage_cost"]],
    })


def test_funded_short_receives_and_pays_at_mark_then_stress_preserves_cashflows():
    t = one_trade()
    paid = ledger.funded_trades(t, -1, evidence())
    assert paid["funding_events"].iloc[0] == 2
    assert paid["funding_return"].iloc[0] == pytest.approx(0.00005)
    assert paid["net"].iloc[0] == pytest.approx(
        t["net"].iloc[0] + 0.00005
    )
    stressed = crash_execution.rescore(paid, -1, 5.0, 20.0)
    assert stressed["funding_return"].iloc[0] == pytest.approx(0.00005)
    assert stressed["net"].iloc[0] == pytest.approx(
        crash_execution.fill_accounting(-1, 100.0, 95.0, 5.0, 20.0)["net"] + 0.00005
    )
    assert t["net"].iloc[0] != paid["net"].iloc[0]


def test_funding_model_bounds_ambiguous_exit_minute_event_without_trade_selection_bias():
    # The exit might be before or after the positive settlement at 16:00.
    original = one_trade(exit_="2024-01-01T16:00:30Z", reason="stop")
    bounded = ledger.funded_trades(original, -1, evidence())
    assert len(bounded) == len(original) == 1
    assert bounded["funding_events"].iloc[0] == 1  # 08:00: negative rate is certain
    assert bounded["funding_ambiguous_events"].iloc[0] == 1  # 16:00
    assert bounded["funding_return"].iloc[0] == pytest.approx(-0.00005)
    assert bounded["funding_return_upper"].iloc[0] == pytest.approx(+0.00005)
    assert bounded["funding_bound_width"].iloc[0] == pytest.approx(0.0001)
    assert bounded["net"].iloc[0] == pytest.approx(
        original["net"].iloc[0] - 0.00005
    )
    assert bounded["net_upper"].iloc[0] == pytest.approx(
        original["net"].iloc[0] + 0.00005
    )


def test_funding_model_rejects_uncovered_holdout_trade():
    with pytest.raises(ValueError, match="outside audited"):
        ledger.funded_trades(
            one_trade(exit_="2024-01-03T00:00:00Z"), -1, evidence()
        )


def test_funded_run_values_daily_baseline_as_well_as_strategy():
    t = one_trade()
    run = study.Run(
        trades={"BTCUSDT": t},
        baseline={"BTCUSDT": t.copy(deep=True)},
        forward={},
        start=pd.Timestamp("2024-01-01T00:00:00Z"),
        end=pd.Timestamp("2024-01-02T00:00:00Z"),
        missing=[],
    )
    adjusted = ledger.funded_run(run, -1, evidence())
    assert adjusted.trades["BTCUSDT"]["funding_events"].iloc[0] == 2
    assert adjusted.baseline["BTCUSDT"]["funding_events"].iloc[0] == 2
    assert "funding_return" not in run.trades["BTCUSDT"]
    assert ledger.funding_totals(adjusted.trades)["funded_settlements"] == 2


def test_cli_funding_adjusted_attempt_references_source_and_records_real_cashflows(tmp_path, monkeypatch):
    cfg = setup(tmp_path)
    remote = events()
    getter = fake_get(remote, [])
    plan_path = tmp_path / "funding-plan.json"
    plan_path.write_text(json.dumps(crash_funding.survey(cfg, get=getter)))
    assert crash_funding.apply(cfg, plan_path, get=getter)["status"] == "committed"

    frame = one_trade()
    baseline = frame.copy()
    def fake_run(_root, _symbols, _settings, _boundary, *, holdout=False, evidence=None):
        assert not holdout
        assert evidence is not None
        return study.Run(
            trades={"BTCUSDT": frame.copy()},
            baseline={"BTCUSDT": baseline.copy()},
            forward={m: [] for m in study.FORWARD_MIN},
            start=pd.Timestamp("2024-01-01T00:00:00Z"),
            end=pd.Timestamp("2024-01-02T00:00:00Z"),
            missing=[],
        )

    monkeypatch.setattr(study, "run", fake_run)
    rc = lab_main([
        "--config", str(cfg), "crash", "study", "--mode", "breakdown",
        "--symbols", "BTCUSDT", "--funding-plan", str(plan_path),
    ])
    assert rc == 0
    records = history(tmp_path / "results")
    assert len(records) == 1
    recorded = records[0]
    assert recorded["status"] == "completed"
    assert recorded["params"]["funding_model"] == ledger.MODEL
    assert recorded["result"]["funding_status"] == "audited_pessimistic_minute_fill_bound"
    assert recorded["result"]["funding_upper_scenario"]["mean_bps"] >= recorded["result"]["mean_bps"]
    assert recorded["result"]["funding_totals"]["funded_settlements"] == 2
    assert recorded["result"]["funding_evidence_sha256"] == recorded["params"]["funding_source"]["events_sha256"]
    assert recorded["params"]["funding_source"]["plan_sha256"] == crash_funding._digest(
        json.loads(plan_path.read_text())
    )
    streams = recorded["inputs"]["streams"]
    assert any(
        s["name"] == "funding:BTCUSDT" and s["records"] == 7 and s["complete"]
        for s in streams
    )
    assert len(recorded["result"]["fixed_execution_scenarios"]) == 3
    assert recorded["holdout"] is False


def test_tampered_local_funding_source_blocks_attempt_before_market_study(tmp_path, monkeypatch):
    cfg = setup(tmp_path)
    remote = events()
    getter = fake_get(remote, [])
    plan_path = tmp_path / "funding-plan.json"
    plan_path.write_text(json.dumps(crash_funding.survey(cfg, get=getter)))
    crash_funding.apply(cfg, plan_path, get=getter)
    cache = next((tmp_path / "data" / "crash" / "funding-pinned" / "BTCUSDT").glob("*.json"))
    modified = json.loads(cache.read_text())
    modified["events"][0]["fundingRate"] = "0.5"
    cache.write_text(json.dumps(modified))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("market study read data after failed funding audit")

    monkeypatch.setattr(study, "run", forbidden)
    with pytest.raises(SystemExit, match="funding source audit failed"):
        lab_main([
            "--config", str(cfg), "crash", "study", "--mode", "breakdown",
            "--symbols", "BTCUSDT", "--funding-plan", str(plan_path),
        ])
    assert len(history(tmp_path / "results")) == 1
    assert history(tmp_path / "results")[0]["status"] == "failed"


def test_ambiguous_negative_short_funding_is_charged_in_lower_bound():
    original = one_trade(exit_="2024-01-01T08:00:20Z", reason="stop")
    result = ledger.funded_trades(original, -1, evidence())
    assert len(result) == 1
    assert result["funding_events"].iloc[0] == 0
    assert result["funding_ambiguous_events"].iloc[0] == 1
    assert result["funding_return"].iloc[0] == pytest.approx(-0.00005)
    assert result["funding_return_upper"].iloc[0] == pytest.approx(0.0)


def test_ambiguous_positive_short_funding_is_never_booked_in_lower_bound():
    original = one_trade(
        entry="2024-01-01T15:00:00Z",
        exit_="2024-01-01T16:00:10Z",
        reason="target",
    )
    result = ledger.funded_trades(original, -1, evidence())
    assert result["funding_events"].iloc[0] == 0
    assert result["funding_ambiguous_events"].iloc[0] == 1
    assert result["funding_return"].iloc[0] == pytest.approx(0.0)
    assert result["funding_return_upper"].iloc[0] == pytest.approx(0.0001)


def test_ambiguous_entry_funding_respects_conservative_bounds():
    original = one_trade(
        entry="2024-01-01T08:00:05Z",
        exit_="2024-01-01T10:00:00Z",
    )
    result = ledger.funded_trades(original, -1, evidence())
    assert result["funding_ambiguous_events"].iloc[0] == 1
    assert result["funding_return"].iloc[0] == pytest.approx(-0.00005)
    assert result["funding_return_upper"].iloc[0] == pytest.approx(0.0)


def test_cost_stress_preserves_both_funding_endpoints_and_trade_count():
    original = one_trade(exit_="2024-01-01T16:00:30Z")
    funded = ledger.funded_trades(original, -1, evidence())
    cheap = crash_execution.rescore(funded, -1, 5.0, 5.0)
    harsh = crash_execution.rescore(funded, -1, 5.0, 20.0)
    assert len(cheap) == len(harsh) == len(original)
    assert cheap["funding_return"].equals(harsh["funding_return"])
    assert cheap["funding_return_upper"].equals(harsh["funding_return_upper"])
    assert cheap["net_upper"].iloc[0] >= cheap["net"].iloc[0]
    assert harsh["net_upper"].iloc[0] >= harsh["net"].iloc[0]
    assert harsh["net"].iloc[0] < cheap["net"].iloc[0]
    assert harsh["net_upper"].iloc[0] < cheap["net_upper"].iloc[0]
    totals = ledger.funding_totals({"BTCUSDT": funded})
    assert totals["trades_with_ambiguous_funding"] == 1
    assert totals["ambiguous_settlements"] == 1
    assert totals["uncertainty_width_sum_bps"] == pytest.approx(1.0)


def test_no_settlements_during_short_trade_validated_zero_without_imputation():
    original = one_trade(
        entry="2024-01-01T01:00:00Z", exit_="2024-01-01T02:00:00Z",
    )
    funded = ledger.funded_trades(original, -1, evidence())
    assert funded["funding_events"].iloc[0] == 0
    assert funded["funding_ambiguous_events"].iloc[0] == 0
    assert funded["funding_return"].iloc[0] == 0
    assert funded["funding_return_upper"].iloc[0] == 0
    assert funded["net"].iloc[0] == pytest.approx(original["net"].iloc[0])
