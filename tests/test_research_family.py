"""Whole-search evidence cannot be reduced to a successful symbol or retry."""

import copy
import json
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from lab import fast_research as fr
from lab import research_family as rf
from lab import trials
from lab.persistence import atomic_json, read_json
# Test-only identifier. Importing tests.test_fast would also import NautilusTrader,
# which is unrelated to research-family persistence and unavailable in Termux.
IID = "BTCUSDT-PERP.BINANCE"


def member(**changes):
    return {"kind": "fast-backtest", "instrument": IID, "params": {"threshold": 0.6}, "window": [0, 100], **changes}


def plan(*members):
    return {
        "name": "prospective search",
        "rationale": "Compare the full frozen grid.",
        "members": list(members or [member()]),
    }


def evaluate(root, spec=None, *, result=None, value=1.0, holdout=False):
    spec = spec or member()
    with trials.trial_run(
        root, spec["kind"], spec["instrument"], spec["params"], spec["window"], holdout=holdout
    ) as attempt:
        attempt.inputs.series("prices", pd.Series([value], index=pd.date_range("2024-01-01", periods=1, tz="UTC")))
        attempt.result = {"pnl": -1} if result is None else result
    return attempt.attempt_id


def die(root):
    with trials.trial_run(root, "fast-backtest", IID, member()["params"], (0, 100)):
        os._exit(23)


def test_frozen_plan_covers_all_study_kinds_and_symbols_without_pooling_returns(tmp_path):
    members = [member(kind=kind, instrument=f"symbol-{i}") for i, kind in enumerate(sorted(rf.KINDS))]
    original = plan(*members)
    registered = rf.register_family(tmp_path, original)
    original["members"].clear()
    assert len(read_json(tmp_path / rf.FAMILY)["plan"]["members"]) == len(rf.KINDS)
    initial = rf.family_report(tmp_path)
    assert len(initial["missing_members"]) == len(rf.KINDS) and not initial["prospective_coverage_complete"]
    for spec in members:
        evaluate(tmp_path, spec)
    report = rf.family_report(tmp_path)
    assert report["prospective_coverage_complete"] and not report["qualified"]
    assert report["research_entries"] == report["configurations"] == len(rf.KINDS)
    assert report["plan_sha256"] == registered["plan_sha256"] and not report["reasons"]
    assert read_json(tmp_path / rf.REPORT) == report
    assert all(r["family_id"] == registered["family_id"] for r in trials.history(tmp_path))
    # An attractive holdout cannot change the research plan, coverage or history digest.
    evaluate(tmp_path, member(instrument="OTHER"), result={"pnl": 999}, holdout=True)
    assert rf.family_report(tmp_path) == report
    assert "family_id" not in trials.history(tmp_path)[-1]


@pytest.mark.parametrize(
    "change", [{"instrument": "OTHER"}, {"kind": "maker-study"}, {"params": {"threshold": 0.9}}, {"window": [0, 101]}]
)
def test_unplanned_evaluations_remain_linked_and_block_coverage(tmp_path, change):
    family = rf.register_family(tmp_path, plan())
    evaluate(tmp_path)
    ident = evaluate(tmp_path, member(**change))
    report = rf.family_report(tmp_path)
    assert report["unplanned_attempt_ids"] == [ident]
    assert not report["prospective_coverage_complete"] and report["configurations"] >= 1
    assert trials.history(tmp_path)[-1]["family_id"] == family["family_id"]


def test_new_legacy_evidence_cannot_hide_after_a_complete_plan(tmp_path):
    rf.register_family(tmp_path, plan())
    evaluate(tmp_path)
    fr.register_trial(tmp_path, "maker-study", "OTHER", {}, (0, 100), {"pnl": -10})
    report = rf.family_report(tmp_path)
    assert not report["prospective_coverage_complete"]
    assert report["legacy_entries"] == 1 and report["research_entries"] == 2


@pytest.mark.parametrize("mode", ["failed", "unavailable", "no-inputs", "partial-inputs"])
def test_successful_retry_never_clears_an_unsuccessful_attempt(tmp_path, mode):
    rf.register_family(tmp_path, plan())
    if mode == "unavailable":
        bad = evaluate(tmp_path, result={"status": "unavailable", "reason": "no history"})
    elif mode == "failed":
        with (
            pytest.raises(RuntimeError),
            trials.trial_run(tmp_path, "fast-backtest", IID, member()["params"], (0, 100)) as failure,
        ):
            raise RuntimeError("failed to load")
        bad = failure.attempt_id
    else:
        with trials.trial_run(tmp_path, "fast-backtest", IID, member()["params"], (0, 100)) as attempt:
            if mode == "partial-inputs":
                with pytest.raises(RuntimeError):
                    attempt.inputs._stream("prices", broken_input())
            attempt.result = {"pnl": -1}
        bad = attempt.attempt_id
    evaluate(tmp_path)
    report = rf.family_report(tmp_path)
    assert bad in report["incomplete_attempt_ids"] and not report["prospective_coverage_complete"]


def broken_input():
    yield None, {"price": 1}
    raise RuntimeError("truncated input")


def test_hard_exit_retains_a_linked_reservation_that_retry_cannot_erase(tmp_path):
    family = rf.register_family(tmp_path, plan())
    process = multiprocessing.get_context("spawn").Process(target=die, args=(tmp_path,))
    process.start()
    try:
        process.join(timeout=20)
        assert process.exitcode == 23
        row = trials.history(tmp_path)[0]
        assert row["status"] == "reserved" and row["family_id"] == family["family_id"]
        evaluate(tmp_path)
        report = rf.family_report(tmp_path)
        assert report["incomplete_attempt_ids"] == [row["attempt_id"]]
        assert not report["prospective_coverage_complete"]
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


@pytest.mark.parametrize("legacy", [False, True])
def test_registration_retains_earlier_outcomes_and_cannot_make_them_prospective(tmp_path, legacy):
    if legacy:
        fr.register_trial(tmp_path, "fast-backtest", IID, member()["params"], (0, 100), {"pnl": -1})
    else:
        evaluate(tmp_path)
    before = trials.history(tmp_path)
    registered = rf.register_family(tmp_path, plan())
    assert registered["prior_history"] == before
    evaluate(tmp_path)
    report = rf.family_report(tmp_path)
    assert report["prior_entries"] == 1 and not report["prospective_coverage_complete"]
    assert any("retrospective" in reason for reason in report["reasons"])
    initial = read_json(tmp_path / rf.FAMILY)
    successor = rf.register_family(tmp_path, plan(member(params={"threshold": 0.9})))
    assert read_json(tmp_path / rf.FAMILY) == initial  # immutable original
    assert successor["previous_registration_sha256"] == registered["sha256"]
    assert successor["prior_history"] == trials.history(tmp_path)
    report_after = rf.family_report(tmp_path)
    assert report_after["prior_entries"] == 2
    assert not report_after["prospective_coverage_complete"]
    assert any("retrospective" in reason for reason in report_after["reasons"])


def test_prior_evidence_deletion_is_detected_even_after_both_original_copies_are_removed(tmp_path):
    evaluate(tmp_path)
    rf.register_family(tmp_path, plan())
    (tmp_path / fr.TRIALS).unlink()
    next((tmp_path / trials.ATTEMPTS).glob("*.json")).unlink()
    with pytest.raises(ValueError, match="missing or changed"):
        rf.family_report(tmp_path)


def test_identical_retries_are_counted_without_extra_configurations_but_changed_results_block(tmp_path):
    rf.register_family(tmp_path, plan())
    evaluate(tmp_path)
    evaluate(tmp_path)
    report = rf.family_report(tmp_path)
    assert report["prospective_coverage_complete"] and report["configurations"] == 1
    assert len(report["attempt_ids"]) == 2
    third = evaluate(tmp_path, result={"pnl": 100})
    report = rf.family_report(tmp_path)
    assert report["conflicting_attempt_ids"] == [third]
    assert not report["prospective_coverage_complete"]


def test_changed_inputs_on_retry_block_coverage(tmp_path):
    rf.register_family(tmp_path, plan())
    evaluate(tmp_path)
    ident = evaluate(tmp_path, value=2.0)
    report = rf.family_report(tmp_path)
    assert report["changed_input_attempt_ids"] == [ident]
    assert not report["prospective_coverage_complete"]


def test_runtime_change_is_retained_and_blocks_the_registered_identity(tmp_path, monkeypatch):
    rf.register_family(tmp_path, plan())
    original = trials.code_provenance

    def changed():
        code = original()
        code["python"] += " changed-runtime"
        return code

    monkeypatch.setattr(trials, "code_provenance", changed)
    ident = evaluate(tmp_path)
    report = rf.family_report(tmp_path)
    assert report["changed_code_attempt_ids"] == [ident]
    assert not report["prospective_coverage_complete"]


def test_competing_registrations_cannot_replace_the_first_plan(tmp_path):
    def register(i):
        try:
            return rf.register_family(tmp_path, plan(member(params={"threshold": i})))
        except ValueError as e:
            assert "unattempted planned members" in str(e)
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(register, range(4)))
    assert sum(value is not None for value in outcomes) == 1
    assert rf.load_family(tmp_path) == next(value for value in outcomes if value is not None)


def test_failed_registration_write_does_not_publish_a_partial_plan(tmp_path, monkeypatch):
    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(rf, "atomic_json", fail)
    with pytest.raises(OSError, match="disk full"):
        rf.register_family(tmp_path, plan())
    assert not (tmp_path / rf.FAMILY).exists()


def test_report_write_failure_cannot_change_registration_or_evidence(tmp_path, monkeypatch):
    family = rf.register_family(tmp_path, plan())
    evaluate(tmp_path)
    rows = trials.history(tmp_path)

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(rf, "atomic_json", fail)
    with pytest.raises(OSError, match="disk full"):
        rf.family_report(tmp_path)
    assert rf.load_family(tmp_path) == family and trials.history(tmp_path) == rows


@pytest.mark.parametrize("damage", ["missing", "invalid", "holdout"])
def test_missing_or_invalid_links_are_not_silently_accepted(tmp_path, damage):
    rf.register_family(tmp_path, plan())
    evaluate(tmp_path)
    path = next((tmp_path / trials.ATTEMPTS).glob("*.json"))
    row = read_json(path)
    if damage == "missing":
        del row["family_id"], row["family_plan_sha256"]
    elif damage == "holdout":
        row["holdout"] = True
    else:
        row["family_id"] = "broken"
    atomic_json(path, row)
    (tmp_path / fr.TRIALS).write_text(json.dumps(row) + "\n")
    if damage == "missing":
        report = rf.family_report(tmp_path)
        assert report["unlinked_attempt_ids"] == [row["attempt_id"]]
        assert not report["prospective_coverage_complete"]
    else:
        with pytest.raises(ValueError, match="family"):
            rf.family_report(tmp_path)


def test_registration_lock_serializes_competing_evaluations(tmp_path, monkeypatch):
    writing, release, entered = threading.Event(), threading.Event(), threading.Event()
    original = rf.atomic_json

    def blocked_write(path, value):
        if path.name == rf.FAMILY:
            writing.set()
            assert release.wait(10)
        return original(path, value)

    monkeypatch.setattr(rf, "atomic_json", blocked_write)

    def run():
        with trials.trial_run(tmp_path, "fast-backtest", IID, member()["params"], (0, 100)) as attempt:
            entered.set()
            attempt.result = {"pnl": -1}

    with ThreadPoolExecutor(max_workers=2) as pool:
        registered = pool.submit(rf.register_family, tmp_path, plan())
        assert writing.wait(10)
        evaluation = pool.submit(run)
        assert not entered.wait(0.1)
        release.set()
        family = registered.result(timeout=10)
        evaluation.result(timeout=10)
    assert trials.history(tmp_path)[0]["family_id"] == family["family_id"]


def test_registration_during_an_earlier_running_evaluation_retains_its_reservation(tmp_path):
    with trials.trial_run(tmp_path, "fast-backtest", IID, member()["params"], (0, 100)) as attempt:
        family = rf.register_family(tmp_path, plan())
        assert family["prior_history"][0]["status"] == "reserved"
        attempt.result = {"pnl": -1}
    assert not rf.family_report(tmp_path)["prospective_coverage_complete"]


def test_missing_registration_with_linked_attempts_cannot_be_replaced_or_bypassed(tmp_path):
    rf.register_family(tmp_path, plan())
    evaluate(tmp_path)
    (tmp_path / rf.FAMILY).unlink()
    with pytest.raises(ValueError, match="registration is missing"):
        rf.register_family(tmp_path, plan())
    with (
        pytest.raises(ValueError, match="registration is missing"),
        trials.trial_run(tmp_path, "fast-backtest", IID, {}, (0, 100)),
    ):
        pytest.fail("evaluation began without its registration")


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":NaN}', "{", '{"version":99}'])
def test_corrupt_registration_stops_evaluations_before_input_access(tmp_path, raw):
    (tmp_path / rf.FAMILY).write_text(raw)
    with pytest.raises(ValueError), trials.trial_run(tmp_path, "fast-backtest", IID, {}, (0, 100)):
        pytest.fail("evaluation began with corrupt registration")
    assert not list((tmp_path / trials.ATTEMPTS).glob("*.json"))


def test_plan_mutation_is_detected(tmp_path):
    rf.register_family(tmp_path, plan())
    family = read_json(tmp_path / rf.FAMILY)
    family["plan"]["members"][0]["params"]["threshold"] = 0.9
    atomic_json(tmp_path / rf.FAMILY, family)
    with pytest.raises(ValueError, match="digest"):
        rf.family_report(tmp_path)


@pytest.mark.parametrize(
    "damage", ["empty", "duplicate", "unbounded", "boolean-time", "unknown-kind", "unknown-field", "nan"]
)
def test_invalid_or_ambiguous_plans_do_not_create_registration(tmp_path, damage):
    candidate = copy.deepcopy(plan())
    m = candidate["members"][0]
    if damage == "empty":
        candidate["members"] = []
    elif damage == "duplicate":
        candidate["members"].append(copy.deepcopy(m))
    elif damage == "unbounded":
        m["window"][1] = None
    elif damage == "boolean-time":
        m["window"][0] = True
    elif damage == "unknown-kind":
        m["kind"] = "hidden-research"
    elif damage == "unknown-field":
        candidate["ignore_failures"] = True
    else:
        m["params"]["threshold"] = float("nan")
    with pytest.raises(ValueError):
        rf.register_family(tmp_path, candidate)
    assert not (tmp_path / rf.FAMILY).exists()


def test_cli_register_and_report_exit_status_disclose_blocked_coverage(tmp_path, capsys):
    from lab.cli import main
    from tests.conftest import REPO

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan()))
    args = ["--config", str(tmp_path / "lab.toml"), "research"]
    assert main([*args, "register", "--plan", str(path)]) == 0
    assert main([*args, "report"]) == 1
    assert "planned evaluations are missing" in capsys.readouterr().out
    evaluate(tmp_path / "results")
    assert main([*args, "report"]) == 0
    out = capsys.readouterr().out
    assert "evidence only" in out and "qualified=false" in out


def test_actual_carry_command_links_and_counts_automatic_comparison(tmp_path, capsys):
    from lab import carry_study as cs
    from lab.cli import main
    from tests.conftest import REPO
    from tests.test_carry import market

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    root = tmp_path / "data" / "carry"
    market(root, days=400)
    results = tmp_path / "results"
    b = cs.boundary(results, root)
    settings = cs.CarrySettings()
    always = cs.CarrySettings(**{**cs.asdict(settings), "entry_apr": -1e9, "exit_apr": -1e9})
    members = [
        member(
            kind="carry-study",
            instrument="carry-cash",
            params=cs.params_of(s, ["BTCUSDT"]),
            window=[None, int(b.value) - 1],
        )
        for s in (settings, always)
    ]
    rf.register_family(results, plan(*members))
    assert main(["--config", str(tmp_path / "lab.toml"), "carry", "study", "--symbols", "BTCUSDT"]) == 0
    report = rf.family_report(results)
    assert report["prospective_coverage_complete"] and report["configurations"] == 2
    assert len(report["attempt_ids"]) == 2 and not report["qualified"]
    diagnostic = read_json(next((results / "inference").glob("*.json")))
    assert diagnostic["selection_scope"] == {
        "kind": "carry-study",
        "instrument": "carry-cash",
        "whole_search_adjusted": False,
    }
    assert "independent validation failed" in capsys.readouterr().out


def test_append_only_successor_family_keeps_old_registration_and_trial_evidence(tmp_path):
    first = rf.register_family(tmp_path, plan())
    evaluate(tmp_path, result={"pnl": -15})
    old_rows = trials.history(tmp_path)
    original_bytes = (tmp_path / rf.FAMILY).read_bytes()
    new_plan = plan(member(instrument="NEXT", params={"threshold": 0.9}))
    second = rf.register_family(tmp_path, new_plan)
    assert (tmp_path / rf.FAMILY).read_bytes() == original_bytes
    assert (tmp_path / rf.EPOCHS / "000001.json").is_file()
    assert second["previous_registration_sha256"] == first["sha256"]
    assert second["prior_history"] == old_rows
    assert rf.load_family(tmp_path) == second
    spec = new_plan["members"][0]
    newer_attempt = evaluate(tmp_path, spec)
    rows = trials.history(tmp_path)
    assert rows[0] == old_rows[0]
    assert rows[1]["attempt_id"] == newer_attempt
    assert rows[1]["family_id"] == second["family_id"]
    blocked = rf.family_report(tmp_path)
    assert blocked["prior_entries"] == len(old_rows)
    assert not blocked["prospective_coverage_complete"]
    assert not blocked["qualified"]
    third_plan = plan(member(instrument="THIRD", params={"threshold": 0.5}))
    third = rf.register_family(tmp_path, third_plan)
    assert third["previous_registration_sha256"] == second["sha256"]
    assert (tmp_path / rf.EPOCHS / "000002.json").is_file()
    assert rf.load_family(tmp_path) == third


def test_successor_registration_denied_for_unattempted_member(tmp_path):
    rf.register_family(tmp_path, plan())
    with pytest.raises(ValueError, match="unattempted planned members"):
        rf.register_family(tmp_path, plan(member(instrument="NEW")))
    assert not (tmp_path / rf.EPOCHS / "000001.json").exists()


def test_successor_registration_denied_for_identical_plan(tmp_path):
    rf.register_family(tmp_path, plan())
    evaluate(tmp_path)
    with pytest.raises(ValueError, match="already registered"):
        rf.register_family(tmp_path, plan())
    assert not (tmp_path / rf.EPOCHS / "000001.json").exists()


def test_successor_registration_denied_while_any_attempt_reserved(tmp_path):
    rf.register_family(tmp_path, plan())
    evaluate(tmp_path)
    with trials.trial_run(tmp_path, "fast-backtest", "SLOW", {}, (0, 100)) as waiting:
        with pytest.raises(ValueError, match="reserved research attempts"):
            rf.register_family(tmp_path, plan(member(instrument="NEW")))
        waiting.result = {"not_completed_at_registration": True}
    assert not (tmp_path / rf.EPOCHS / "000001.json").exists()


def test_corrupted_successor_chain_blocks_read_and_new_attempts(tmp_path):
    rf.register_family(tmp_path, plan())
    evaluate(tmp_path)
    rf.register_family(tmp_path, plan(member(instrument="NEW")))
    record = tmp_path / rf.EPOCHS / "000001.json"
    prior = read_json(record)
    modified = {**prior, "previous_registration_sha256": "f" * 64}
    modified["sha256"] = rf._digest({k: v for k, v in modified.items() if k != "sha256"})
    atomic_json(record, modified)
    with pytest.raises(ValueError, match="predecessor chain"):
        rf.load_family(tmp_path)
    with pytest.raises(ValueError, match="predecessor chain"):
        with trials.trial_run(tmp_path, "fast-backtest", "NEW", {}, (0, 100)):
            pytest.fail("attempt should not be reserved with corrupted registration")


def test_missing_first_registration_blocks_even_with_successor_present(tmp_path):
    rf.register_family(tmp_path, plan())
    evaluate(tmp_path)
    rf.register_family(tmp_path, plan(member(instrument="NEXT")))
    (tmp_path / rf.FAMILY).unlink()
    with pytest.raises(ValueError, match="original research family registration is missing"):
        rf.load_family(tmp_path)
    with pytest.raises(ValueError, match="original research family registration is missing"):
        rf.register_family(tmp_path, plan(member(instrument="THIRD")))


def test_missing_successor_sequence_blocks_registration_and_attempts(tmp_path):
    rf.register_family(tmp_path, plan())
    evaluate(tmp_path)
    rf.register_family(tmp_path, plan(member(instrument="NEXT")))
    evaluate(tmp_path, member(instrument="NEXT"))
    rf.register_family(tmp_path, plan(member(instrument="THIRD")))
    (tmp_path / rf.EPOCHS / "000001.json").unlink()
    with pytest.raises(ValueError, match="sequence is missing"):
        rf.load_family(tmp_path)
    with pytest.raises(ValueError, match="sequence is missing"):
        with trials.trial_run(tmp_path, "fast-backtest", "THIRD", {}, (0, 100)):
            pytest.fail("cannot issue after deleting middle generation")


def test_successor_cannot_reclassify_missing_prior_attempt_as_retrospective(tmp_path):
    rf.register_family(tmp_path, plan())
    evaluate(tmp_path)
    previous = trials.history(tmp_path)[0]
    (tmp_path / fr.TRIALS).unlink()
    (tmp_path / trials.ATTEMPTS / f"{previous['attempt_id']}.json").unlink()
    with pytest.raises(ValueError, match="unattempted planned members"):
        rf.register_family(tmp_path, plan(member(instrument="NEXT")))
    assert not (tmp_path / rf.EPOCHS / "000001.json").exists()


def test_missing_final_generation_cannot_relabel_linked_trials_to_older_plan(tmp_path):
    rf.register_family(tmp_path, plan())
    evaluate(tmp_path)
    new_plan = plan(member(instrument="NEW"))
    rf.register_family(tmp_path, new_plan)
    evaluate(tmp_path, new_plan["members"][0])
    (tmp_path / rf.EPOCHS / "000001.json").unlink()
    # A removed *last* generation has no sequence gap, so the
    # recorded links are the second independent proof of its existence.
    with pytest.raises(ValueError, match="registration is missing"):
        with trials.trial_run(tmp_path, "fast-backtest", "THIRD", {}, (0, 100)):
            pytest.fail("missing last generation must not silently revert active plan")
    with pytest.raises(ValueError, match="registration is missing"):
        rf.family_report(tmp_path)


def test_prior_reserved_identity_mutation_blocks_later_family(tmp_path):
    with trials.trial_run(tmp_path, "fast-backtest", IID, member()["params"], (0, 100)) as running:
        rf.register_family(tmp_path, plan())
        running.result = {"pnl": -1}
    evaluate(tmp_path)
    before = trials.history(tmp_path)
    attempt = before[0]
    path = tmp_path / trials.ATTEMPTS / f"{attempt['attempt_id']}.json"
    tampered = read_json(path)
    tampered["params"]["threshold"] = 0.9
    tampered["fingerprint"] = fr.fingerprint(tampered["params"])
    atomic_json(path, tampered)
    (tmp_path / fr.TRIALS).write_text(
        "\n".join(json.dumps(row) for row in [tampered, before[1]])
        + "\n"
    )
    with pytest.raises(ValueError, match="reserved trial identity"):
        rf.register_family(tmp_path, plan(member(instrument="NEXT")))
