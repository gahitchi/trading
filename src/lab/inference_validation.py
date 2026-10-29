"""Independent, resumable synthetic validation of the frozen daily diagnostic.

Run python -m lab.inference_validation --output DIRECTORY --workers 6.
The protocol fixes data seeds and production bootstrap settings; no report qualifies a trading strategy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from lab import daily_inference as di
from lab.persistence import atomic_json, file_lock, read_json
from lab.trials import code_provenance

PROTOCOL = "daily-v1-independent-null-v1"
PROTOCOL_COMMIT = "66129e942753db17fc6d9c7de5633ff87c842b86"
PROTOCOL_SHA256 = "abefdc79e6ad3e2495f660a8af433f2869d7f770340f2d91d5e7233c17289f6a"
FROZEN_METHOD_SHA256 = "048c8136891340f2a92b8a50eecb8bff7e118e3cf815ec3c0ac1c57d6d2aa94d"
MASTER_SEED = 20261030
REPETITIONS = 1000
RESAMPLES = 1999
BURN_IN = 512
THRESHOLD = 0.05


@dataclass(frozen=True)
class Case:
    id: int
    name: str
    n: int
    phi: float
    distribution: str = "gaussian"
    primary: bool = True
    variance_shift: bool = False


REGISTERED_CASES = (
    Case(0, "iid_minimum", 120, 0.0),
    Case(1, "ar05", 240, 0.5),
    Case(2, "ar08", 240, 0.8),
    Case(3, "ar05_t5", 240, 0.5, "t5"),
    Case(4, "ar_negative", 240, -0.5),
    Case(5, "variance_shift", 240, 0.5, primary=False, variance_shift=True),
)
CASES = REGISTERED_CASES


def _now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def binomial_upper(events: int, repetitions: int, alpha: float) -> float:
    """One-sided Clopper-Pearson upper limit, inverted in log space; alpha is the excluded upper tail."""
    if (
        type(events) is not int
        or type(repetitions) is not int
        or repetitions < 1
        or not 0 <= events <= repetitions
        or not isinstance(alpha, (int, float))
        or not math.isfinite(alpha)
        or not 0 < alpha < 1
    ):
        raise ValueError("invalid binomial bound arguments")
    if events == repetitions:
        return 1.0
    if events == 0:
        return -math.expm1(math.log(alpha) / repetitions)

    def cdf(p):
        log_odds = math.log(p) - math.log1p(-p)
        term = repetitions * math.log1p(-p)
        terms = [term]
        for k in range(1, events + 1):
            term += math.log(repetitions - k + 1) - math.log(k) + log_odds
            terms.append(term)
        peak = max(terms)
        return min(1.0, math.exp(peak) * math.fsum(math.exp(v - peak) for v in terms))

    low, high = 0.0, 1.0
    for _ in range(80):
        mid = (low + high) / 2
        if mid in (low, high):
            break
        if cdf(mid) > alpha:
            low = mid
        else:
            high = mid
    return high  # conservative end of the numerical bracket


def generate_sample(case: Case, repetition: int):
    rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([MASTER_SEED, case.id, repetition])))
    shape = (case.n + BURN_IN, 5)
    innovations = (
        rng.standard_t(5, size=shape) * math.sqrt(3 / 5) if case.distribution == "t5" else rng.normal(size=shape)
    )
    factors = np.empty(shape)
    previous = np.zeros(5)
    scale = math.sqrt(1 - case.phi**2)
    for i, innovation in enumerate(innovations):
        previous = case.phi * previous + scale * innovation
        factors[i] = previous
    factors = factors[BURN_IN:]
    values = 0.8 * factors[:, :1] + 0.6 * factors[:, 1:]
    if case.variance_shift:
        values[case.n // 2 :] *= 3
    return values


def _source_identity(code):
    return {k: code[k] for k in ("source_sha256", "python", "implementation", "dependencies", "project_files")}


def _identity():
    method_hash = hashlib.sha256(Path(di.__file__).read_bytes()).hexdigest()
    if method_hash != FROZEN_METHOD_SHA256:
        raise ValueError("daily diagnostic source differs from the frozen preregistered method")
    code = code_provenance()
    core = {
        "protocol": PROTOCOL,
        "protocol_commit": PROTOCOL_COMMIT,
        "protocol_sha256": PROTOCOL_SHA256,
        "method_sha256": method_hash,
        "source": _source_identity(code),
        "repetitions": REPETITIONS,
        "resamples": RESAMPLES,
        "master_seed": MASTER_SEED,
        "burn_in": BURN_IN,
        "threshold": THRESHOLD,
        "cases": [asdict(case) for case in CASES],
        "bootstrap_seed": di.SEED,
        "block_days": list(di.BLOCKS),
        "hac_lags": list(di.LAGS),
    }
    return core, code


def _read(path):
    try:
        return read_json(path)
    except (OSError, ValueError) as e:
        raise ValueError(f"unreadable validation evidence {path.name}: {e}") from e


def _checkpoint(root, case, identity, core):
    path = root / f"{case.name}.json"
    if not path.exists():
        return None
    row = _read(path)
    if (
        not isinstance(row, dict)
        or row.get("version") != 1
        or row.get("identity") != identity
        or row.get("case") != asdict(case)
        or row.get("status") not in ("running", "completed", "interrupted")
    ):
        raise ValueError(f"invalid validation checkpoint identity: {case.name}")
    values = row.get("minimum_p")
    if (
        not isinstance(values, list)
        or len(values) > core["repetitions"]
        or row.get("outcomes_sha256") != _digest(values)
    ):
        raise ValueError(f"invalid validation checkpoint prefix: {case.name}")
    if any(
        type(v) not in (float, int)
        or not math.isfinite(v)
        or not 0 <= v <= 1
        or abs(v * (core["resamples"] + 1) - round(v * (core["resamples"] + 1))) > 1e-10
        for v in values
    ):
        raise ValueError(f"invalid validation checkpoint p-values: {case.name}")
    if row["status"] == "completed" and len(values) != core["repetitions"]:
        raise ValueError(f"incomplete completed validation checkpoint: {case.name}")
    if not isinstance(row.get("interruptions"), list):
        raise ValueError(f"invalid validation checkpoint interruption history: {case.name}")
    return row


def _run_case(root: Path, case: Case, identity: str, core: dict):
    # Case locks also protect against orphan workers after a controller is killed.
    with file_lock(root / f"{case.name}.lock"):
        if (
            _source_identity(code_provenance()) != core["source"]
            or hashlib.sha256(Path(di.__file__).read_bytes()).hexdigest() != core["method_sha256"]
        ):
            raise ValueError("validation worker source/runtime identity changed")
        row = _checkpoint(root, case, identity, core)
        if row is not None and row["status"] == "completed":
            return case.name
        row = row or {"version": 1, "identity": identity, "case": asdict(case), "minimum_p": [], "interruptions": []}
        path = root / f"{case.name}.json"

        def save(status):
            row.update(status=status, outcomes_sha256=_digest(row["minimum_p"]))
            atomic_json(path, row)

        save("running")
        try:
            for repetition in range(len(row["minimum_p"]), core["repetitions"]):
                values = generate_sample(case, repetition)
                report = di.joint_bootstrap(values, resamples=core["resamples"])
                row["minimum_p"].append(float(min(report["p_joint"])))
                if len(row["minimum_p"]) % 10 == 0:
                    save("running")
            save("completed")
        except BaseException as e:
            row["interruptions"].append({"at": _now(), "error": f"{type(e).__name__}: {e}"})
            with suppress(OSError):  # existing deterministic prefixes remain resumable
                save("interrupted")
            raise
        return case.name


def _summary(root, identity, core):
    allowed = {"manifest.json", "summary.json", *(f"{case.name}.json" for case in CASES)}
    if any(path.name not in allowed for path in root.glob("*.json")):
        raise ValueError("unknown validation case or evidence file")
    assessments = []
    for case in CASES:
        row = _checkpoint(root, case, identity, core)
        count = len(row["minimum_p"]) if row is not None else 0
        complete = row is not None and row["status"] == "completed"
        assessment = {
            "case": case.name,
            "primary": case.primary,
            "completed": count,
            "complete": complete,
            "events": None,
            "rate": None,
            "upper_bound": None,
            "bound_confidence": 0.99 if case.primary else 0.95,
            "meets_bound": None,
        }
        if complete:
            events = sum(p <= core["threshold"] for p in row["minimum_p"])
            upper = binomial_upper(events, count, 0.01 if case.primary else 0.05)
            assessment.update(
                events=events,
                rate=events / count,
                upper_bound=upper,
                meets_bound=upper <= 0.05,
                checkpoint_sha256=hashlib.sha256((root / f"{case.name}.json").read_bytes()).hexdigest(),
            )
        assessments.append(assessment)
    complete = all(c["complete"] for c in assessments)
    registered = (
        core["repetitions"] == 1000
        and core["resamples"] == 1999
        and core["master_seed"] == 20261030
        and core["burn_in"] == 512
        and core["cases"] == [asdict(case) for case in REGISTERED_CASES]
        and core["bootstrap_seed"] == 20261029
        and core["block_days"] == [7, 14, 28]
        and core["hac_lags"] == [7, 14, 28]
        and core["threshold"] == 0.05
    )
    return {
        "version": 1,
        "identity": identity,
        "protocol": PROTOCOL,
        "qualified": False,
        "complete": complete,
        "registered_parameters": registered,
        "cases": assessments,
        "primary_requirement_met": all(c["meets_bound"] for c in assessments if c["primary"])
        if complete and registered
        else None,
        "simultaneous_primary_confidence": 0.95,
        "scope": "fixed synthetic laws only; no trading gate or universal error-control qualification",
    }


def run_validation(root: Path, *, workers: int = 1):
    if type(workers) is not int or not 1 <= workers <= len(CASES):
        raise ValueError(f"workers must be between 1 and {len(CASES)}")
    root = Path(root)
    with file_lock(root / "validation.lock"):
        core, code = _identity()
        identity = _digest(core)
        manifest_path = root / "manifest.json"
        if manifest_path.exists():
            manifest = _read(manifest_path)
            if (
                not isinstance(manifest, dict)
                or manifest.get("version") != 1
                or manifest.get("identity") != identity
                or manifest.get("execution") != core
                or not isinstance(manifest.get("runs"), list)
            ):
                raise ValueError("validation manifest version or identity differs")
            if (root / "summary.json").exists():
                previous = _read(root / "summary.json")
                if (
                    not isinstance(previous, dict)
                    or previous.get("identity") != identity
                    or previous.get("qualified") is not False
                ):
                    raise ValueError("invalid validation summary identity")
        else:
            if list(root.glob("*.json")):
                raise ValueError("orphan validation evidence without a manifest")
            manifest = {"version": 1, "identity": identity, "execution": core, "code": code, "runs": []}
        initial = _summary(root, identity, core)  # validate every existing prefix before starting new work
        run = {"at": _now(), "status": "running"}
        manifest["runs"].append(run)
        atomic_json(manifest_path, manifest)  # reservation before simulations
        try:
            atomic_json(root / "summary.json", initial)
            if workers == 1:
                for case in CASES:
                    _run_case(root, case, identity, core)
                    atomic_json(root / "summary.json", _summary(root, identity, core))
            else:
                with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
                    futures = [pool.submit(_run_case, root, case, identity, core) for case in CASES]
                    for future in as_completed(futures):
                        future.result()
                        atomic_json(root / "summary.json", _summary(root, identity, core))
            summary = _summary(root, identity, core)
            atomic_json(root / "summary.json", summary)
        except BaseException as e:
            run.update(status="failed", finished_at=_now(), error=f"{type(e).__name__}: {e}")
            with suppress(OSError):
                atomic_json(manifest_path, manifest)
            raise
        run.update(status="completed", finished_at=_now())
        atomic_json(manifest_path, manifest)
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="durable validation/checkpoint directory")
    parser.add_argument("--workers", type=int, default=1, help="case processes, 1..6; does not change samples")
    args = parser.parse_args(argv)
    try:
        result = run_validation(args.output, workers=args.workers)
    except (OSError, ValueError) as e:
        parser.exit(2, f"validation stopped: {e}\n")
    for case in result["cases"]:
        print(
            f"{case['case']}: {case['events']}/{case['completed']}, upper {case['upper_bound']:.6f}, primary={case['primary']}"
        )
    print(f"Registered primary requirement met: {result['primary_requirement_met']}; qualified: False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
