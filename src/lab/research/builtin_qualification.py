"""Versioned, operator-approved research evidence for an unattended built-in PAPER policy.

Diagnostic/supervised paper trading is intentionally separate. A successful
backtest is not evidence of an executable autonomous policy. Qualification
requires a point-in-time, adjusted source, bound train/test, an explicit review,
and the *exact* paper risk/capital/context and built-in source artifact.

This records evidence rather than asserting profitable future performance.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from lab import data, experiments
from lab.config import validate_effective
from lab.persistence import atomic_json, read_json
from lab.provenance import frame_digest
from lab.research.approval import ApprovalError, deployment_context
from lab.signals.registry import SignalSpec

SCHEMA = 1


def artifact_digest(spec: SignalSpec) -> str:
    """Pin the built-in implementation, beyond its public strategy name."""
    if spec.path is not None:
        raise ApprovalError("built-in policy evidence cannot approve generated code")
    from lab.signals import builtin

    path = Path(builtin.__file__)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bound_context(cfg, tickers):
    if cfg.trading.speed != "daily":
        raise ApprovalError("built-in autonomous qualification currently supports daily policies only")
    return deployment_context(cfg.snapshot(), live=True, tickers=tickers)


def _identity(cfg, spec: SignalSpec, tickers: list[str], frames: dict) -> dict:
    if spec.path is not None:
        raise ApprovalError("expected a built-in strategy")
    if len(tickers) != len(set(tickers)) or not tickers:
        raise ApprovalError("qualification needs a nonempty unique trading universe")
    return {
        "schema": SCHEMA,
        "spec": {"name": spec.name, "params": spec.params, "source_sha256": artifact_digest(spec)},
        "tickers": sorted(tickers),
        "deployment": _bound_context(cfg, tickers),
        "data_sha256": frame_digest(frames),
        "quality": data.quality(cfg, tickers),
        "research_fingerprint": experiments.config_fingerprint(
            replace(cfg, capital=cfg.paper.capital), len(tickers), bound_risk=True
        ),
        "periods": {
            part: {
                "start": str(cfg.period(part).start),
                "end": str(cfg.period(part).end),
            }
            for part in ("train", "test")
        },
        "thresholds": {
            "min_sharpe_edge": cfg.research.min_sharpe_edge,
            "max_p_value": cfg.research.max_p_value,
            "min_dsr": cfg.research.min_dsr,
            "null_samples": cfg.research.null_samples,
        },
    }


def evidence_path(cfg, spec: SignalSpec, tickers: list[str]) -> Path:
    # All variants are retained rather than overwriting earlier decisions.
    raw = json.dumps({"name": spec.name, "params": spec.params, "tickers": sorted(tickers)}, sort_keys=True)
    key = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return cfg.research_dir / "builtin_qualifications" / f"{key}.json"


def _quality_gate(quality: dict) -> None:
    if not quality.get("survivorship_free") or not quality.get("point_in_time_universe"):
        raise ApprovalError("autonomous research requires independently recorded point-in-time universe membership")
    if not quality.get("adjusted") or not quality.get("split_history"):
        raise ApprovalError("autonomous research requires adjusted prices with verified split history")
    if quality.get("dropped_incomplete"):
        raise ApprovalError("autonomous research rejects incomplete vendor observations")
    if set(quality.get("sources", [])) & {"yahoo_finance", "backtesting.py sample (real GOOG prices)"}:
        raise ApprovalError("unofficial sample/Yahoo prices cannot authorize autonomous paper execution")
    if quality.get("shorting") and quality.get("borrow_model") != "historical_close_book":
        raise ApprovalError("short-capable research needs recorded historical borrow evidence")


def _passes_train(candidate: dict, null: dict, cfg) -> bool:
    return (
        candidate.get("sharpe_edge", float("-inf")) >= cfg.research.min_sharpe_edge
        and null.get("p_value", float("inf")) <= cfg.research.max_p_value
        and candidate.get("dsr", float("-inf")) >= cfg.research.min_dsr
        and candidate.get("signal_errors") == 0
        and candidate.get("fills", 0) > 0
    )


def evaluate_builtin(cfg, spec: SignalSpec, tickers: list[str]) -> dict:
    """Evaluate a policy under its actual paper capital and risk, never exploratory caps."""
    validate_effective(cfg)
    if spec.path is not None:
        raise ApprovalError("qualify-builtin expects a built-in SignalSpec")
    research_cfg = replace(cfg, capital=cfg.paper.capital)
    frames = data.load_many(research_cfg, tickers)
    identity = _identity(cfg, spec, tickers, frames)
    _quality_gate(identity["quality"])
    bounds = {"bound_risk": True}
    train, _ = experiments.compare(
        research_cfg, [spec], tickers, research_cfg.period("train"), frames, **bounds
    )
    candidate = train.rows[1]
    null = experiments.null_test(
        research_cfg, spec, tickers, research_cfg.period("train"), cfg.research.null_samples,
        frames, observed_edge=candidate["sharpe_edge"], **bounds
    )
    train_pass = _passes_train(candidate, null, cfg) and not any(
        row.get("delistings_settled") for row in train.rows
    )
    # Do not expose the test period to a candidate that did not pass training.
    if not train_pass:
        return {
            "identity": identity,
            "train": {"candidate": candidate, "null": null, "passed": False},
            "test": None,
            "qualified": False,
        }
    test, _ = experiments.compare(research_cfg, [spec], tickers, research_cfg.period("test"), frames, **bounds)
    test_row = test.rows[1]
    survived = (
        test_row.get("sharpe_edge", 0) > 0
        and test_row.get("signal_errors") == 0
        and test_row.get("fills", 0) > 0
        and not any(row.get("delistings_settled") for row in test.rows)
    )
    return {
        "identity": identity,
        "train": {"candidate": candidate, "null": null, "passed": True},
        "test": {"candidate": test_row, "survived": survived},
        "qualified": bool(survived),
    }


def approve_builtin(cfg, spec: SignalSpec, tickers: list[str], evaluation: dict) -> Path:
    """Operator action: persist only independently passing train/test evidence."""
    if evaluation.get("qualified") is not True or evaluation["train"]["passed"] is not True:
        raise ApprovalError("built-in strategy did not pass research qualification")
    if evaluation["test"]["survived"] is not True:
        raise ApprovalError("built-in strategy did not survive out-of-sample testing")
    frames = data.load_many(cfg, tickers)
    if evaluation.get("identity") != _identity(cfg, spec, tickers, frames):
        raise ApprovalError("data, implementation or configured deployment changed since evaluation")
    path = evidence_path(cfg, spec, tickers)
    # A changed reviewed result must not overwrite historical approval: save as new
    # report only after its prior identity has been explicitly invalidated.
    if path.exists():
        raise ApprovalError("an approval already exists for this strategy/universe; use a fresh reviewed protocol")
    record = {**evaluation, "approved_at": datetime.now(UTC).isoformat(timespec="seconds"), "reviewed": True}
    atomic_json(path, record)
    return path


def verify_builtin(cfg, spec: SignalSpec, tickers: list[str]) -> str:
    """Recheck recorded evidence before granting an unattended paper mandate."""
    path = evidence_path(cfg, spec, tickers)
    if not path.exists():
        raise ApprovalError(f"{spec.name}: missing built-in research approval; use supervised paper trading")
    record = read_json(path)
    if (
        record.get("reviewed") is not True
        or record.get("qualified") is not True
        or record.get("train", {}).get("passed") is not True
        or record.get("test", {}).get("survived") is not True
    ):
        raise ApprovalError("built-in strategy lacks passing reviewed train/test evidence")
    identity = record["identity"]
    train = record["train"]
    test = record["test"]
    if not _passes_train(train["candidate"], train["null"], cfg):
        raise ApprovalError("built-in training metrics no longer satisfy configured research thresholds")
    candidate = test["candidate"]
    if candidate.get("sharpe_edge", 0) <= 0 or candidate.get("signal_errors") != 0 or candidate.get("fills", 0) <= 0:
        raise ApprovalError("built-in out-of-sample evidence is not qualified")
    if identity.get("schema") != SCHEMA or identity.get("spec") != {
        "name": spec.name, "params": spec.params, "source_sha256": artifact_digest(spec)
    }:
        raise ApprovalError("built-in strategy source/parameter digest changed")
    if identity["tickers"] != sorted(tickers) or identity.get("deployment") != _bound_context(cfg, tickers):
        raise ApprovalError("built-in approval no longer matches the deployment context")
    current = data.load_many(cfg, tickers)
    actual = _identity(cfg, spec, tickers, current)
    if identity != actual:
        raise ApprovalError("research data, evaluation periods, gates or provenance changed; re-validate builtin")
    _quality_gate(identity["quality"])
    # Return an immutable identity digest, not a claim of absolute predictive certainty.
    return hashlib.sha256(path.read_bytes()).hexdigest()
