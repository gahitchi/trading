"""Pre-registered daily dependence diagnostics, never an automatic trading gate."""

import json
import math

import numpy as np
import pandas as pd
import pytest

from lab import daily_inference as di
from lab import fast_research as fr
from lab import trials
from tests.test_fast import IID


def stats(values, *, basis=fr.RETURN_BASIS):
    dates = pd.date_range("2024-01-01", periods=len(values), tz="UTC")
    return {
        "basis": basis,
        "days": len(values),
        "daily_returns": {str(d.date()): float(v) for d, v in zip(dates, values, strict=True)},
    }


def test_hac_mean_matches_the_registered_numerical_example():
    r = di.hac_mean(np.arange(30, dtype=float), 7)
    assert r["mean"] == 14.5
    assert r["se"] == pytest.approx(3.913320899155871)
    assert r["t"] == pytest.approx(3.7052928634418265)


def test_negative_autocorrelation_cannot_reduce_se_below_iid():
    x = np.tile([-1.0, 1.0], 80)
    assert di.hac_mean(x, 28)["se"] >= x.std(ddof=1) / math.sqrt(len(x))


def test_daily_sample_always_drops_boundaries_and_requires_120_interior_dates():
    x = np.random.default_rng(12).normal(size=122)
    report = di.daily_inference(stats(x))
    assert report["available"] is True and report["qualified"] is False
    assert report["days"] == 120 and report["start"] == "2024-01-02"
    assert report["mean"] == pytest.approx(x[1:-1].mean())
    short = di.daily_inference(stats(x[:-1]))
    assert short["available"] is False and "120" in short["reason"]


@pytest.mark.parametrize("damage", ["gap", "missing", "nonfinite", "basis", "constant", "bad-date"])
def test_invalid_daily_evidence_is_unavailable(damage):
    s = stats(np.random.default_rng(1).normal(size=130))
    keys = list(s["daily_returns"])
    if damage == "gap":
        del s["daily_returns"][keys[10]]
        s["days"] -= 1
    elif damage == "missing":
        s["daily_returns"][keys[10]] = None
    elif damage == "nonfinite":
        s["daily_returns"][keys[10]] = math.inf
    elif damage == "basis":
        s["basis"] = "hourly"
    elif damage == "constant":
        s = stats([0.0] * 130)
    else:
        s["daily_returns"]["2024-1-1"] = s["daily_returns"].pop(keys[0])
    r = di.daily_inference(s)
    assert not r["available"] and r["reason"]
    json.dumps(r, allow_nan=False)


def reference(x, resamples, seed):
    n, m = x.shape
    centered = x - x.mean(axis=0)
    se = np.array([max(di.hac_mean(x[:, j], lag)["se"] for lag in di.LAGS) for j in range(m)])
    observed = x.mean(axis=0) / se
    sensitivities = []
    for block in di.BLOCKS:
        rng = np.random.Generator(np.random.PCG64(seed + block))
        maxima = []
        for _ in range(resamples):
            starts = rng.integers(0, n, size=math.ceil(n / block))
            indices = [(int(start) + k) % n for start in starts for k in range(block)][:n]
            maxima.append(max(centered[indices].mean(axis=0) / se))
        sensitivities.append([(1 + sum(v >= t for v in maxima)) / (resamples + 1) for t in observed])
    return np.max(sensitivities, axis=0)


def test_joint_resampling_matches_an_independent_reference_loop():
    x = np.random.default_rng(24).normal(size=(120, 3)) + np.array([0.1, 0, -0.1])
    report = di.joint_bootstrap(x, resamples=131, seed=7)
    assert report["p_joint"] == pytest.approx(reference(x, 131, 7))
    assert report["resolution"] == 1 / 132


def test_scale_column_order_duplicates_and_family_growth():
    x = np.random.default_rng(44).normal(size=(150, 3)) + np.array([0.15, -0.1, 0.2])
    original = di.joint_bootstrap(x, resamples=199)
    assert di.joint_bootstrap(x * [10, 0.1, 3], resamples=199)["p_joint"] == original["p_joint"]
    assert di.joint_bootstrap(x[:, ::-1], resamples=199)["p_joint"] == original["p_joint"][::-1]
    duplicate = di.joint_bootstrap(np.column_stack([x, x[:, 0]]), resamples=199)
    assert duplicate["p_joint"][:3] == original["p_joint"]
    smaller = di.joint_bootstrap(x[:, :2], resamples=199)
    assert np.all(np.array(original["p_joint"][:2]) >= smaller["p_joint"])


def test_fixed_dependent_null_calibration():
    rng = np.random.Generator(np.random.PCG64(20261029))
    exceed = 0
    for _ in range(200):
        factors = np.empty((240, 5))
        factors[0] = rng.normal(size=5)
        for i in range(1, 240):
            factors[i] = 0.5 * factors[i - 1] + math.sqrt(0.75) * rng.normal(size=5)
        x = 0.8 * factors[:, :1] + 0.6 * factors[:, 1:]
        report = di.joint_bootstrap(x, resamples=499)
        exceed += min(report["p_joint"]) <= 0.05
    print(f"registered dependent null: {exceed}/200")
    assert exceed <= 16, f"registered null calibration failed: {exceed}/200 > 16/200"


def add_trial(root, params, values, *, holdout=False, window=(0, 100)):
    with trials.trial_run(root, "fast-backtest", IID, params, window, holdout=holdout) as attempt:
        attempt.inputs.series("common-input", pd.Series([1.0], index=pd.date_range("2024-01-01", periods=1, tz="UTC")))
        attempt.result = {"return_stats": stats(values)}
    return attempt


def test_research_family_uses_all_identified_configurations_and_excludes_holdout(tmp_path):
    x = np.random.default_rng(99).normal(size=(130, 2))
    add_trial(tmp_path, {"threshold": 0.6}, x[:, 0])
    selected = {"threshold": 0.7}
    add_trial(tmp_path, selected, x[:, 1])
    r = di.research_inference(tmp_path, "fast-backtest", IID, fr.fingerprint(selected))
    assert r["available"] and not r["qualified"] and r["configurations"] == 2
    assert r["days"] == 128 and len(r["attempt_ids"]) == 2
    add_trial(tmp_path, {"threshold": 0.9}, x[:, 0] + 10, holdout=True)
    after = di.research_inference(tmp_path, "fast-backtest", IID, fr.fingerprint(selected))
    assert after == r
    # An identical rerun is retained without pretending it is an additional configuration.
    add_trial(tmp_path, selected, x[:, 1])
    again = di.research_inference(tmp_path, "fast-backtest", IID, fr.fingerprint(selected))
    assert again["configurations"] == 2 and len(again["attempt_ids"]) == 3
    assert again["p_joint"] == r["p_joint"]


@pytest.mark.parametrize("damage", ["failed", "reserved", "legacy", "window", "series", "basis"])
def test_invalid_family_is_not_qualified_by_dropping_an_attempt(tmp_path, damage):
    x = np.random.default_rng(33).normal(size=(125, 2))
    params = {"threshold": 0.6}
    add_trial(tmp_path, params, x[:, 0])
    add_trial(tmp_path, {"threshold": 0.7}, x[:, 1])
    if damage == "failed":
        with pytest.raises(ValueError), trials.trial_run(tmp_path, "fast-backtest", IID, {}, (0, 100)):
            raise ValueError("missing prices")
    elif damage == "reserved":
        # A valid reservation snapshot, as a hard-exit would leave it.
        add_trial(tmp_path, {"threshold": 0.8}, x[:, 0])
        path = max((tmp_path / trials.ATTEMPTS).glob("*.json"), key=lambda p: json.loads(p.read_text())["at"])
        entry = json.loads(path.read_text())
        entry.update(status="reserved", result={})
        fr.atomic_json(path, entry)
        lines = (tmp_path / fr.TRIALS).read_text().splitlines()
        (tmp_path / fr.TRIALS).write_text("\n".join(lines[:-1]) + "\n")
    elif damage == "legacy":
        fr.register_trial(tmp_path, "fast-backtest", IID, {}, (0, 100), {"return_stats": stats(x[:, 0])})
    elif damage == "window":
        add_trial(tmp_path, {"threshold": 0.8}, x[:, 0], window=(0, 101))
    elif damage == "series":
        add_trial(tmp_path, params, x[:, 0] + 1)
    else:
        with trials.trial_run(tmp_path, "fast-backtest", IID, {}, (0, 100)) as attempt:
            attempt.inputs.series(
                "common-input", pd.Series([1.0], index=pd.date_range("2024-01-01", periods=1, tz="UTC"))
            )
            attempt.result = {"return_stats": stats(x[:, 0], basis=di.CARRY_BASIS)}
    r = di.research_inference(tmp_path, "fast-backtest", IID, fr.fingerprint(params))
    assert not r["available"] and not r["qualified"] and r["reason"]


def test_cli_carry_saves_auditable_unqualified_family_report(tmp_path, capsys):
    from lab.cli import main
    from tests.conftest import REPO
    from tests.test_carry import market

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    market(tmp_path / "data" / "carry", days=400)
    args = ["--config", str(tmp_path / "lab.toml"), "carry", "study", "--symbols", "BTCUSDT"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "daily HAC t" in out and "joint block p" in out
    assert "unqualified" in out and "not a probability of an edge" in out
    files = list((tmp_path / "results" / "inference").glob("*.json"))
    assert len(files) == 1
    r = fr.read_json(files[0])
    assert r["available"] and not r["qualified"] and r["configurations"] == 2
    assert len(r["attempt_ids"]) == 2 and r["history_sha256"]
    assert r["resamples"] == 1999 and r["block_days"] == [7, 14, 28]
    assert r["independent_validation"]["primary_requirement_met"] is False
    assert "independent validation failed" in out
    rows = trials.history(tmp_path / "results")
    assert all(row["result"]["return_stats"]["basis"] == di.CARRY_BASIS for row in rows)
    assert all(row["result"]["daily_inference"]["available"] for row in rows)
    # A holdout run records only its own daily inference, without re-running the research family.
    assert main([*args, "--holdout"]) == 0
    assert len(list((tmp_path / "results" / "inference").glob("*.json"))) == 1
    assert trials.history(tmp_path / "results")[-1]["result"]["daily_inference"]["qualified"] is False


def test_cli_fast_logs_explicit_unavailable_reasons_for_short_recordings(tmp_path, capsys):
    from lab.cli import main
    from tests.conftest import REPO
    from tests.test_maker import catalog, ranging_market

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    path = catalog(tmp_path / "catalog", *ranging_market(steps=10))
    args = [
        "--config",
        str(tmp_path / "lab.toml"),
        "fast",
        "backtest",
        "BTCUSDT",
        "--catalog",
        path,
        "--strategy",
        "maker",
    ]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "daily HAC unavailable: requires 120" in out and "joint block diagnostic unavailable" in out
    row = trials.history(tmp_path / "results")[0]
    assert row["result"]["daily_inference"]["method"] == di.METHOD
    assert row["result"]["daily_inference"]["available"] is False
    report = fr.read_json(tmp_path / "results" / "inference" / f"{row['attempt_id']}.json")
    assert not report["available"] and report["attempt_ids"] == [row["attempt_id"]]
    assert report["qualified"] is False


def test_derived_report_publication_failure_cannot_erase_the_evaluation(tmp_path, monkeypatch, capsys):
    from lab import persistence
    from lab.cli import main
    from tests.conftest import REPO
    from tests.test_maker import catalog, ranging_market

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    path = catalog(tmp_path / "catalog", *ranging_market(steps=10))

    def fail(*_args):
        raise OSError("derived report disk failure")

    # Existing evaluation/reservation modules retain their imported persistence function; fail the new writer.
    monkeypatch.setattr(persistence, "atomic_json", fail)
    with pytest.raises(OSError, match="derived report disk failure"):
        main(["--config", str(tmp_path / "lab.toml"), "fast", "backtest", "BTCUSDT", "--catalog", path])
    rows = trials.history(tmp_path / "results")
    assert len(rows) == 1 and rows[0]["status"] == "completed"
    assert "daily_inference" in rows[0]["result"]
    assert "joint block p" not in capsys.readouterr().out
