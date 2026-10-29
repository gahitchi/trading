"""Durable evaluation attempts and provenance, independent of successful result publication.

One atomic file per attempt is authoritative. The existing JSONL log is a compatibility mirror;
legacy rows remain evidence. A process killed before completion leaves a counted reserved attempt.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import uuid4

from lab.fast_research import TRIALS, fingerprint
from lab.persistence import append_json, atomic_json, file_lock, parse_json, read_json

ATTEMPTS = "research_attempts"


def _now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _frozen(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _code_identity(code):
    return _encoded(
        {k: code.get(k) for k in ("source_sha256", "python", "implementation", "dependencies", "project_files")}
    )


def code_provenance(package: Path | None = None) -> dict:
    """Hash actual package source even outside Git or with local edits. Do not inspect credentials or env."""
    package = Path(package) if package is not None else Path(__file__).resolve().parent
    sources = {
        p.relative_to(package).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(package.rglob("*.py"))
    }
    if not sources:
        raise ValueError("cannot identify research source code")
    commit = None
    dirty = None
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=package, capture_output=True, text=True, timeout=5, check=True
        )
        commit = head.stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=package, capture_output=True, text=True, timeout=5, check=True
        )
        dirty = bool(status.stdout)
    except (OSError, subprocess.SubprocessError):
        pass  # wheels and source archives still have a content identity
    dependencies = {}
    for name in ("nautilus_trader", "numpy", "pandas", "pyarrow"):
        try:
            dependencies[name] = version(name)
        except PackageNotFoundError:
            dependencies[name] = None
    project = package.parents[1]
    project_files = {
        name: hashlib.sha256((project / name).read_bytes()).hexdigest()
        for name in ("pyproject.toml", "uv.lock")
        if (project / name).is_file()
    }
    return {
        "source_sha256": hashlib.sha256(_encoded(sources)).hexdigest(),
        "sources": sources,
        "git_commit": commit,
        "git_dirty": dirty,
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "dependencies": dependencies,
        "project_files": project_files,
    }


class InputEvidence:
    """SHA-256 over length-delimited canonical records actually loaded for an evaluation, not file mtimes.

    Native market objects use their lossless to_dict representation. Carry series encode UTC nanoseconds and
    float.hex values (including explicit nonfinite values), so even a one-ULP price change changes the digest.
    """

    def __init__(self):
        self.streams = []

    def _stream(self, name, records):
        if any(s["name"] == name for s in self.streams):
            raise ValueError(f"duplicate input stream: {name}")
        entry = {"name": name, "records": 0, "first_ns": None, "last_ns": None, "sha256": None, "complete": False}
        self.streams.append(entry)
        digest = hashlib.sha256()
        try:
            for ts, record in records:
                raw = _encoded(record)
                digest.update(len(raw).to_bytes(8, "big"))
                digest.update(raw)
                entry["records"] += 1
                if ts is not None:
                    entry["first_ns"] = ts if entry["first_ns"] is None else min(entry["first_ns"], ts)
                    entry["last_ns"] = ts if entry["last_ns"] is None else max(entry["last_ns"], ts)
        except BaseException:
            entry["sha256"] = digest.hexdigest()
            raise
        entry.update(sha256=digest.hexdigest(), complete=True)

    def objects(self, name, values):
        self._stream(name, ((int(v.ts_init), v.to_dict()) for v in values))

    def series(self, name, values):
        if values is None:
            self._stream(name, [(None, {"absent": True})])
        else:
            self._stream(name, ((int(ts.value), [int(ts.value), float(v).hex()]) for ts, v in values.items()))

    def snapshot(self):
        streams = _frozen(self.streams)
        return {
            "method": "sha256-canonical-records-v1",
            "streams": streams,
            "sha256": hashlib.sha256(_encoded(streams)).hexdigest(),
        }


@dataclass
class TrialEvaluation:
    window: tuple
    result: dict | None = None
    inputs: InputEvidence = field(default_factory=InputEvidence)
    attempt_id: str | None = None


def _validate(row, *, attempt=False):
    required = {"at", "kind", "instrument", "holdout", "fingerprint", "params", "window", "result"}
    if not isinstance(row, dict) or not required <= row.keys():
        raise ValueError("invalid trial entry")
    if any(not isinstance(row[k], str) or not row[k] for k in ("at", "kind", "instrument", "fingerprint")):
        raise ValueError("invalid trial identifiers")
    if type(row["holdout"]) is not bool or not isinstance(row["params"], dict) or not isinstance(row["result"], dict):
        raise ValueError("invalid trial configuration or result")
    if row["fingerprint"] != fingerprint(row["params"]):
        raise ValueError("trial fingerprint does not match configuration")
    if (
        not isinstance(row["window"], list)
        or len(row["window"]) != 2
        or any(v is not None and type(v) is not int for v in row["window"])
    ):
        raise ValueError("invalid trial window")
    if attempt or "attempt_id" in row:
        if row.get("version") != 1 or row.get("status") not in ("reserved", "completed", "failed"):
            raise ValueError("invalid attempt version or status")
        ident = row.get("attempt_id", "")
        if not isinstance(ident, str) or len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
            raise ValueError("invalid attempt identifier")
        if (
            not isinstance(row.get("code"), dict)
            or not isinstance(row.get("inputs"), dict)
            or not isinstance(row.get("requested_window"), list)
        ):
            raise ValueError("missing attempt provenance")
        code, inputs = row["code"], row["inputs"]
        if (
            not isinstance(code.get("sources"), dict)
            or not code["sources"]
            or code.get("source_sha256") != hashlib.sha256(_encoded(code["sources"])).hexdigest()
        ):
            raise ValueError("invalid source manifest digest")
        if not {"python", "implementation", "dependencies", "project_files"} <= code.keys():
            raise ValueError("missing runtime provenance")
        streams = inputs.get("streams")
        if (
            inputs.get("method") != "sha256-canonical-records-v1"
            or not isinstance(streams, list)
            or inputs.get("sha256") != hashlib.sha256(_encoded(streams)).hexdigest()
        ):
            raise ValueError("invalid input manifest digest")
        names = set()
        for stream in streams:
            if (
                not isinstance(stream, dict)
                or not {"name", "records", "first_ns", "last_ns", "complete", "sha256"} <= stream.keys()
            ):
                raise ValueError("invalid input stream")
            digest = stream["sha256"]
            if (
                not isinstance(stream["name"], str)
                or stream["name"] in names
                or type(stream["records"]) is not int
                or stream["records"] < 0
                or type(stream["complete"]) is not bool
            ):
                raise ValueError("invalid input stream identity or count")
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("invalid input content digest")
            names.add(stream["name"])
    if "family_id" in row or "family_plan_sha256" in row:
        for name, size in (("family_id", 32), ("family_plan_sha256", 64)):
            value = row.get(name)
            if not isinstance(value, str) or len(value) != size or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("invalid prospective research family link")
        if row["holdout"] or "attempt_id" not in row:
            raise ValueError("only identified research attempts may link to a family")
    return row


def history(results_dir: Path) -> list[dict]:
    """Merge authoritative attempts and legacy/mirrored rows. Corrupt or incomplete files never get skipped."""
    root = Path(results_dir)
    path = root / TRIALS
    try:
        with file_lock(path.with_name(path.name + ".lock")):
            rows = (
                [_validate(parse_json(line)) for line in path.read_text().splitlines() if line.strip()]
                if path.exists()
                else []
            )
        legacy, attempts = [], {}
        for row in rows:
            ident = row.get("attempt_id")
            if ident is None:
                legacy.append(row)
            elif ident in attempts:
                raise ValueError("duplicate attempt in JSONL mirror")
            else:
                attempts[ident] = row
        for source in sorted((root / ATTEMPTS).glob("*.json")):
            row = _validate(read_json(source), attempt=True)
            ident = row["attempt_id"]
            if source.stem != ident:
                raise ValueError("attempt filename does not match its identifier")
            if ident in attempts and row != attempts[ident]:
                raise ValueError("attempt and JSONL mirror disagree")
            attempts[ident] = row
        return legacy + sorted(attempts.values(), key=lambda r: (r["at"], r["attempt_id"]))
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(f"unreadable trial history at {root}: {e}") from e


def count_configurations(results_dir: Path, kind: str, instrument: str) -> int:
    return len(
        {
            (r["fingerprint"], _code_identity(r["code"]) if "code" in r else None)
            for r in history(results_dir)
            if r["kind"] == kind and r["instrument"] == instrument and not r["holdout"]
        }
    )


def incomparable_provenance(rows: list[dict]) -> str:
    """Legacy-only IID diagnostics remain unverified; do not mix them with identified inputs and code."""
    if not rows or not any("code" in r for r in rows):
        return ""
    if any("code" not in r or "inputs" not in r for r in rows):
        return "missing code or input provenance in the research trial history"
    inputs, codes = set(), set()
    for row in rows:
        evidence = row["inputs"]
        if not evidence.get("streams") or any(not s.get("complete") for s in evidence["streams"]):
            return "incomplete input provenance in the research trial history"
        inputs.add(evidence["sha256"])
        codes.add(_code_identity(row["code"]))
    if len(inputs) > 1:
        return "research input contents differ despite their recorded windows"
    if len(codes) > 1:
        return "research code or runtime provenance differs"
    return ""


@contextmanager
def trial_run(results_dir: Path, kind: str, instrument: str, params: dict, window: tuple, *, holdout=False):
    """Reserve before data access; preserve exceptions, hard exits and publication failures as attempts."""
    from lab.research_family import LOCK, bind_family

    params, requested = _frozen(params), _frozen(window)
    entry = {
        "version": 1,
        "attempt_id": uuid4().hex,
        "at": _now(),
        "kind": kind,
        "instrument": instrument,
        "holdout": holdout,
        "fingerprint": fingerprint(params),
        "params": params,
        "requested_window": requested,
        "window": requested,
        "status": "reserved",
        "result": {},
        "code": code_provenance(),
        "inputs": InputEvidence().snapshot(),
    }
    path = Path(results_dir) / ATTEMPTS / f"{entry['attempt_id']}.json"
    # Registration and reservation are serialized, but long-running evaluators remain independent.
    with file_lock(Path(results_dir) / LOCK):
        rows = history(results_dir)  # don't add evidence to a corrupt history
        bind_family(results_dir, entry, rows)
        _validate(entry, attempt=True)
        atomic_json(path, entry)
    evaluation = TrialEvaluation(window=tuple(requested), attempt_id=entry["attempt_id"])
    try:
        yield evaluation
        if not isinstance(evaluation.result, dict):
            raise ValueError("trial evaluation ended without a result")
        result = _frozen(evaluation.result)
        actual_window = _frozen(evaluation.window)
        _validate({**entry, "window": actual_window}, attempt=True)
    except BaseException as e:
        entry.update(
            status="failed",
            finished_at=_now(),
            inputs=evaluation.inputs.snapshot(),
            result={"status": "unavailable", "error": f"{type(e).__name__}: {e}"},
        )
        try:
            atomic_json(path, entry)
            append_json(Path(results_dir) / TRIALS, entry)
        except OSError:
            pass  # a durable reserved attempt remains counted; preserve the original exception
        raise
    else:
        entry.update(
            status="completed",
            finished_at=_now(),
            window=actual_window,
            inputs=evaluation.inputs.snapshot(),
            result=result,
        )
        atomic_json(path, entry)
        append_json(Path(results_dir) / TRIALS, entry)
