"""Independent null validation: exact bounds, deterministic checkpoints and no automatic qualification."""

import json
import math
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from lab import inference_validation as iv


def test_exact_binomial_upper_matches_closed_form_examples():
    assert iv.binomial_upper(0, 10, 0.05) == pytest.approx(1 - 0.05**0.1, abs=1e-12)
    assert iv.binomial_upper(1, 2, 0.05) == pytest.approx(math.sqrt(0.95), abs=1e-12)
    assert iv.binomial_upper(10, 10, 0.05) == 1
    assert iv.binomial_upper(0, 1000, 0.01) == pytest.approx(1 - 0.01**0.001, abs=1e-12)
    bounds = [iv.binomial_upper(k, 20, 0.01) for k in range(21)]
    assert bounds == sorted(bounds)


@pytest.mark.parametrize(
    "args", [(-1, 10, 0.05), (11, 10, 0.05), (0, 0, 0.05), (0, 10, 0), (0, 10, 1), (True, 10, 0.05)]
)
def test_invalid_binomial_arguments_are_rejected(args):
    with pytest.raises(ValueError):
        iv.binomial_upper(*args)


def test_samples_are_deterministic_and_do_not_depend_on_worker_schedule():
    a = iv.generate_sample(iv.CASES[1], 4)
    assert a.shape == (240, 4) and np.isfinite(a).all()
    np.testing.assert_array_equal(a, iv.generate_sample(iv.CASES[1], 4))
    assert not np.array_equal(a, iv.generate_sample(iv.CASES[1], 5))
    assert not np.array_equal(a, iv.generate_sample(iv.CASES[2], 4))
    assert iv.generate_sample(iv.CASES[0], 0).shape == (120, 4)


@pytest.fixture
def small_plan(monkeypatch):
    monkeypatch.setattr(iv, "REPETITIONS", 3)
    monkeypatch.setattr(iv, "RESAMPLES", 7)


def test_small_run_and_resume_reuse_all_checkpoints_without_recomputing(tmp_path, monkeypatch, small_plan):
    first = iv.run_validation(tmp_path)
    assert first["complete"] and not first["registered_parameters"] and not first["qualified"]
    assert first["primary_requirement_met"] is None
    assert len(first["cases"]) == 6
    for case in iv.CASES:
        saved = iv.read_json(tmp_path / f"{case.name}.json")
        assert saved["status"] == "completed" and len(saved["minimum_p"]) == 3

    def fail(*_args):
        pytest.fail("completed deterministic simulations were recomputed")

    monkeypatch.setattr(iv, "generate_sample", fail)
    again = iv.run_validation(tmp_path)
    assert again["cases"] == first["cases"] and again["qualified"] is False


def test_interruption_retains_the_prefix_and_resumes_the_same_repetition(tmp_path, monkeypatch, small_plan):
    original = iv.generate_sample
    seen = []

    def interrupt(case, repetition):
        seen.append((case.id, repetition))
        if case.id == 0 and repetition == 2:
            raise RuntimeError("injected interruption")
        return original(case, repetition)

    monkeypatch.setattr(iv, "generate_sample", interrupt)
    with pytest.raises(RuntimeError, match="injected interruption"):
        iv.run_validation(tmp_path)
    checkpoint = iv.read_json(tmp_path / f"{iv.CASES[0].name}.json")
    assert len(checkpoint["minimum_p"]) == 2 and checkpoint["status"] == "interrupted"
    assert iv.read_json(tmp_path / "manifest.json")["runs"][0]["status"] == "failed"
    seen.clear()

    def resume(case, repetition):
        seen.append((case.id, repetition))
        return original(case, repetition)

    monkeypatch.setattr(iv, "generate_sample", resume)
    result = iv.run_validation(tmp_path)
    assert result["complete"] and seen[0] == (0, 2)
    assert iv.read_json(tmp_path / f"{iv.CASES[0].name}.json")["interruptions"]


def test_new_directories_cannot_reroll_the_samples(tmp_path, small_plan):
    iv.run_validation(tmp_path / "a")
    iv.run_validation(tmp_path / "b")
    for case in iv.CASES:
        a = iv.read_json(tmp_path / "a" / f"{case.name}.json")
        b = iv.read_json(tmp_path / "b" / f"{case.name}.json")
        assert a["minimum_p"] == b["minimum_p"]


@pytest.mark.parametrize("damage", ["prefix", "identity", "unknown", "manifest"])
def test_invalid_evidence_fails_closed(tmp_path, small_plan, damage):
    iv.run_validation(tmp_path)
    path = tmp_path / f"{iv.CASES[0].name}.json"
    if damage == "unknown":
        (tmp_path / "unregistered_case.json").write_text("{}")
    elif damage == "manifest":
        (tmp_path / "manifest.json").write_text('{"version":999}')
    else:
        row = iv.read_json(path)
        if damage == "prefix":
            row["minimum_p"][0] = 0.01
        else:
            row["identity"] = "wrong"
        path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match=r"validation|checkpoint"):
        iv.run_validation(tmp_path)


def test_changed_plan_cannot_mix_with_existing_validation(tmp_path, monkeypatch, small_plan):
    iv.run_validation(tmp_path)
    monkeypatch.setattr(iv, "REPETITIONS", 4)
    with pytest.raises(ValueError, match="identity"):
        iv.run_validation(tmp_path)


def test_changed_frozen_method_prevents_simulation(tmp_path, monkeypatch, small_plan):
    monkeypatch.setattr(iv, "FROZEN_METHOD_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="frozen"):
        iv.run_validation(tmp_path)


def test_reservation_failure_prevents_simulation(tmp_path, monkeypatch, small_plan):
    def fail(*_args):
        raise OSError("manifest disk failure")

    monkeypatch.setattr(iv, "atomic_json", fail)
    with pytest.raises(OSError, match="manifest disk failure"):
        iv.run_validation(tmp_path)
    assert not list(tmp_path.glob("ar*.json"))


def test_concurrent_invocations_do_not_duplicate_work(tmp_path, monkeypatch, small_plan):
    original = iv.generate_sample
    seen = []

    def sample(case, repetition):
        seen.append((case.id, repetition))
        return original(case, repetition)

    monkeypatch.setattr(iv, "generate_sample", sample)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: iv.run_validation(tmp_path), range(2)))
    assert all(r["complete"] for r in results)
    assert len(seen) == len(set(seen)) == 18


def test_final_report_write_failure_keeps_completed_cases(tmp_path, monkeypatch, small_plan):
    original = iv.atomic_json

    def fail_final(path, value):
        if str(path).endswith("summary.json") and value["complete"]:
            raise OSError("summary disk failure")
        original(path, value)

    monkeypatch.setattr(iv, "atomic_json", fail_final)
    with pytest.raises(OSError, match="summary disk failure"):
        iv.run_validation(tmp_path)
    assert all(iv.read_json(tmp_path / f"{case.name}.json")["status"] == "completed" for case in iv.CASES)
    monkeypatch.setattr(iv, "atomic_json", original)

    def no_compute(*_args):
        pytest.fail("completed case lost after derived-report failure")

    monkeypatch.setattr(iv, "generate_sample", no_compute)
    assert iv.run_validation(tmp_path)["complete"]


def test_process_workers_match_serial_checkpoints(tmp_path, small_plan):
    serial = tmp_path / "serial"
    parallel = tmp_path / "parallel"
    iv.run_validation(serial)
    iv.run_validation(parallel, workers=2)
    for case in iv.CASES:
        a = iv.read_json(serial / f"{case.name}.json")
        b = iv.read_json(parallel / f"{case.name}.json")
        assert a["minimum_p"] == b["minimum_p"]


def test_incomplete_summary_never_claims_a_primary_pass(tmp_path, monkeypatch, small_plan):
    def stop(*_args):
        raise RuntimeError("stop before first simulation")

    monkeypatch.setattr(iv, "generate_sample", stop)
    with pytest.raises(RuntimeError):
        iv.run_validation(tmp_path)
    summary = iv.read_json(tmp_path / "summary.json")
    assert not summary["complete"] and summary["primary_requirement_met"] is None
    assert all(c["events"] is None and c["upper_bound"] is None for c in summary["cases"])
    assert summary["qualified"] is False


def test_a_high_error_rate_cannot_meet_the_registered_bound(tmp_path, small_plan):
    iv.run_validation(tmp_path)
    core, _ = iv._identity()
    # Use a complete registered-size prefix to exercise assessment independently of the expensive simulations.
    core.update(repetitions=1000, resamples=1999)
    identity = iv._digest(core)
    for case in iv.CASES:
        p_values = [0.01] * 80 + [0.5] * 920
        iv.atomic_json(
            tmp_path / f"{case.name}.json",
            {
                "version": 1,
                "identity": identity,
                "case": iv.asdict(case),
                "minimum_p": p_values,
                "status": "completed",
                "interruptions": [],
                "outcomes_sha256": iv._digest(p_values),
            },
        )
    report = iv._summary(tmp_path, identity, core)
    assert report["registered_parameters"] and report["complete"]
    assert report["primary_requirement_met"] is False and report["qualified"] is False
    assert all(c["upper_bound"] > 0.08 for c in report["cases"])
