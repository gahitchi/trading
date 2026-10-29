"""Pre-registered daily HAC and joint block diagnostics; no automatic gate qualification.

See docs/daily-inference.md. Daily return bases stay separate. A complete prospective selection family and
candidate stability checks are still needed; bootstrap tail areas are not probabilities of an edge.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from itertools import pairwise
from pathlib import Path

import numpy as np

METHOD = "daily-hac-joint-block-v1"
CARRY_BASIS = "daily-net-notional-v1"
BASES = {"daily-net-equity-v1", CARRY_BASIS}
LAGS = (7, 14, 28)
BLOCKS = (7, 14, 28)
MIN_DAYS = 120
RESAMPLES = 1999
SEED = 20261029


def _settings():
    return {
        "method": METHOD,
        "qualified": False,
        "minimum_days": MIN_DAYS,
        "hac_lags": list(LAGS),
        "block_days": list(BLOCKS),
        "resamples": RESAMPLES,
        "seed": SEED,
        "boundary_rule": "drop-first-and-last-date",
    }


def _daily_matrix(statistics):
    if not statistics or any(not isinstance(s, dict) or s.get("basis") not in BASES for s in statistics):
        raise ValueError("missing or unsupported daily return basis")
    if len({s["basis"] for s in statistics}) != 1:
        raise ValueError("daily return bases differ")
    maps = [s.get("daily_returns") for s in statistics]
    if any(not isinstance(m, dict) for m in maps):
        raise ValueError("missing daily return series")
    keys = sorted(maps[0])
    if any(set(m) != set(keys) for m in maps):
        raise ValueError("research observation dates differ")
    if any(s.get("days") != len(keys) for s in statistics):
        raise ValueError("daily observation count does not match the series")
    try:
        dates = [date.fromisoformat(k) for k in keys]
    except (ValueError, TypeError) as e:
        raise ValueError("invalid daily ISO date") from e
    if any(str(d) != k for d, k in zip(dates, keys, strict=True)):
        raise ValueError("daily dates must be canonical ISO dates")
    if any((b - a).days != 1 for a, b in pairwise(dates)):
        raise ValueError("daily calendar has gaps")
    keys = keys[1:-1]
    if len(keys) < MIN_DAYS:
        raise ValueError(f"requires {MIN_DAYS} interior daily observations; got {len(keys)}")
    values = [[m[k] for k in keys] for m in maps]
    if any(type(v) not in (int, float) or not math.isfinite(v) for column in values for v in column):
        raise ValueError("missing or nonfinite daily observations")
    return keys, np.asarray(values, dtype=float).T


def hac_mean(values, lags: int) -> dict:
    x = np.asarray(values, dtype=float)
    if x.ndim != 1 or len(x) < 2 or not np.isfinite(x).all():
        raise ValueError("HAC mean requires at least two finite observations")
    if type(lags) is not int or lags < 0:
        raise ValueError("HAC lags must be a nonnegative integer")
    n = len(x)
    with np.errstate(over="ignore", invalid="ignore"):
        mean = float(x.mean())
        e = x - mean
        raw = float(e @ e)
        adjusted = raw
        for k in range(1, min(lags, n - 1) + 1):
            adjusted += 2 * (1 - k / (lags + 1)) * float(e[k:] @ e[:-k])
        se = math.sqrt(max(raw, adjusted) / ((n - 1) * n))
    if not math.isfinite(mean) or not math.isfinite(se) or se <= np.finfo(float).eps * float(np.max(np.abs(x))):
        raise ValueError("daily variance is zero or numerically degenerate")
    return {"mean": mean, "se": se, "t": mean / se}


def _mean_summary(values):
    sensitivities = {str(lag): hac_mean(values, lag) for lag in LAGS}
    se = max(r["se"] for r in sensitivities.values())
    mean = sensitivities[str(LAGS[0])]["mean"]
    return {"mean": mean, "se": se, "t": mean / se, "by_lag": sensitivities}


def daily_inference(statistics: dict) -> dict:
    result = {**_settings(), "available": False}
    try:
        dates, x = _daily_matrix([statistics])
        summary = _mean_summary(x[:, 0])
    except (ValueError, TypeError) as e:
        return {**result, "reason": str(e)}
    return {
        **result,
        "available": True,
        "basis": statistics["basis"],
        "start": dates[0],
        "end": dates[-1],
        "days": len(dates),
        **summary,
    }


def joint_bootstrap(values, *, resamples: int = RESAMPLES, seed: int = SEED) -> dict:
    """Fixed-SE centered circular-block maximum; shared row indices preserve joint observed dependence."""
    x = np.asarray(values, dtype=float)
    if x.ndim != 2 or x.shape[0] < MIN_DAYS or x.shape[1] < 1 or not np.isfinite(x).all():
        raise ValueError(f"joint bootstrap requires a finite matrix with at least {MIN_DAYS} daily rows")
    if type(resamples) is not int or resamples < 1 or type(seed) is not int or seed < 0:
        raise ValueError("bootstrap resamples must be positive and seed nonnegative integers")
    n, m = x.shape
    summaries = [_mean_summary(x[:, j]) for j in range(m)]
    means = np.array([r["mean"] for r in summaries])
    se = np.array([r["se"] for r in summaries])
    observed = means / se
    centered = x - means
    sensitivities = {}
    for block in BLOCKS:
        rng = np.random.Generator(np.random.PCG64(seed + block))
        count = np.zeros(m, dtype=np.int64)
        for first in range(0, resamples, 128):
            size = min(128, resamples - first)
            starts = rng.integers(0, n, size=(size, math.ceil(n / block)))
            indices = ((starts[:, :, None] + np.arange(block)) % n).reshape(size, -1)[:, :n]
            maxima = (centered[indices].mean(axis=1) / se).max(axis=1)
            count += (maxima[:, None] >= observed).sum(axis=0)
        sensitivities[str(block)] = ((1 + count) / (resamples + 1)).tolist()
    return {
        **_settings(),
        "resamples": resamples,
        "seed": seed,
        "days": n,
        "configurations": m,
        "means": means.tolist(),
        "se": se.tolist(),
        "t": observed.tolist(),
        "p_joint": np.max(list(sensitivities.values()), axis=0).tolist(),
        "by_block": sensitivities,
        "resolution": 1 / (resamples + 1),
    }


def research_inference(results_dir: Path, kind: str, instrument: str, selected_fingerprint: str) -> dict:
    """Snapshot all research attempts in the existing scope. No family/cohort exclusions are introduced."""
    from lab.trials import history, incomparable_provenance

    result = {
        **_settings(),
        "available": False,
        "kind": kind,
        "instrument": instrument,
        "selected_fingerprint": selected_fingerprint,
    }
    try:
        rows = [
            r for r in history(results_dir) if r["kind"] == kind and r["instrument"] == instrument and not r["holdout"]
        ]
        result.update(
            attempt_ids=[r.get("attempt_id") for r in rows],
            history_sha256=hashlib.sha256(
                json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest(),
        )
        if not rows or any(
            r.get("status") != "completed"
            or "attempt_id" not in r
            or "code" not in r
            or "inputs" not in r
            or r["result"].get("status") == "unavailable"
            for r in rows
        ):
            raise ValueError("incomplete, unavailable or unidentified research attempts in the family")
        if reason := incomparable_provenance(rows):
            raise ValueError(reason)
        if len({tuple(r["window"]) for r in rows}) != 1:
            raise ValueError("research result windows differ")
        latest = {}
        for row in rows:
            stats = row["result"].get("return_stats")
            if not isinstance(stats, dict):
                raise ValueError("missing logged daily return series")
            fingerprint = row["fingerprint"]
            if fingerprint in latest:
                old = latest[fingerprint]["result"]["return_stats"]
                if any(old.get(k) != stats.get(k) for k in ("basis", "days", "daily_returns")):
                    raise ValueError("repeated configuration disagrees on its daily returns")
            latest[fingerprint] = row
        fingerprints = sorted(latest)
        if len(fingerprints) < 2:
            raise ValueError("requires at least two comparable research configurations")
        if selected_fingerprint not in latest:
            raise ValueError("selected configuration is absent from research history")
        statistics = [latest[k]["result"]["return_stats"] for k in fingerprints]
        dates, x = _daily_matrix(statistics)
        bootstrap = joint_bootstrap(x)
        selected = fingerprints.index(selected_fingerprint)
    except (ValueError, TypeError, KeyError) as e:
        return {**result, "reason": str(e)}
    return {
        **result,
        "available": True,
        "basis": statistics[0]["basis"],
        "days": len(dates),
        "start": dates[0],
        "end": dates[-1],
        "configurations": len(fingerprints),
        "t_daily": bootstrap["t"][selected],
        "p_joint": bootstrap["p_joint"][selected],
        "by_block": {block: ps[selected] for block, ps in bootstrap["by_block"].items()},
        "resolution": bootstrap["resolution"],
        "family": [
            {
                "fingerprint": key,
                "mean": bootstrap["means"][j],
                "se": bootstrap["se"][j],
                "t_daily": bootstrap["t"][j],
                "p_joint": bootstrap["p_joint"][j],
            }
            for j, key in enumerate(fingerprints)
        ],
    }
