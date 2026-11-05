"""Append-only research-family generations, whole-history coverage, no inference gate.

Every registered plan and all past trial attempts remain immutable evidence.
A successor plan may be frozen only after all members of its predecessor
have been attempted and no evaluation is still reserved. Earlier attempts
remain retrospective; a new family never resets the search count.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from uuid import uuid4

from lab.persistence import atomic_json, file_lock, read_json
from lab.trials import _code_identity, _encoded, _frozen, _now, code_provenance, history

FAMILY = "research_family.json"  # first, permanently frozen generation
EPOCHS = "research_family_epochs"  # numbered immutable successor plans
LOCK = "research_family.lock"
REPORT = "research_family_report.json"
KINDS = {"micro-study", "maker-study", "fast-backtest", "carry-study", "crash-study"}


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _member(row):
    return {
        "kind": row["kind"],
        "instrument": row["instrument"],
        "params": row["params"],
        "window": row.get("requested_window", row["window"]),
    }


def _plan(value):
    value = _frozen(value)
    if not isinstance(value, dict) or set(value) != {"name", "rationale", "members"}:
        raise ValueError("family plan requires exactly name, rationale and members")
    if any(not isinstance(value[k], str) or not value[k].strip() for k in ("name", "rationale")):
        raise ValueError("family plan name and rationale must be nonempty strings")
    if not isinstance(value["members"], list) or not value["members"]:
        raise ValueError("family plan requires at least one member")
    seen = set()
    for member in value["members"]:
        if not isinstance(member, dict) or set(member) != {"kind", "instrument", "params", "window"}:
            raise ValueError("family members require exactly kind, instrument, params and window")
        if (
            not isinstance(member["kind"], str)
            or member["kind"] not in KINDS
            or not isinstance(member["instrument"], str)
            or not member["instrument"].strip()
            or not isinstance(member["params"], dict)
        ):
            raise ValueError("invalid family member kind, instrument or params")
        window = member["window"]
        if (
            not isinstance(window, list)
            or len(window) != 2
            or (window[0] is not None and (type(window[0]) is not int or window[0] < 0))
            or type(window[1]) is not int
            or window[1] < 0
            or (window[0] is not None and window[0] > window[1])
        ):
            raise ValueError("family member window requires [start-or-null, finite-end] UTC nanoseconds")
        key = _digest(member)
        if key in seen:
            raise ValueError("duplicate planned family member")
        seen.add(key)
    return value


def _hex(value, size):
    return isinstance(value, str) and len(value) == size and all(c in "0123456789abcdef" for c in value)


def _validate_registration(value: dict, *, prior_sha: str | None) -> dict:
    """Validate a registration's own digest and its predecessor chain."""
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not _hex(value.get("family_id"), 32)
        or not isinstance(value.get("registered_at"), str)
        or value.get("sha256") != _digest({k: v for k, v in value.items() if k != "sha256"})
    ):
        raise ValueError("invalid research family registration or digest")
    if prior_sha is None:
        if "previous_registration_sha256" in value:
            raise ValueError("initial research family has an unexpected predecessor")
    elif value.get("previous_registration_sha256") != prior_sha:
        raise ValueError("research family registration predecessor chain changed")
    if _digest(_plan(value["plan"])) != value.get("plan_sha256"):
        raise ValueError("invalid research family plan digest")
    code = value.get("code", {})
    if not isinstance(code, dict) or not code.get("sources") or code.get("source_sha256") != _digest(code["sources"]):
        raise ValueError("invalid research family code provenance")
    if not isinstance(value.get("prior_history"), list) or _digest(value["prior_history"]) != value.get(
        "prior_history_sha256"
    ):
        raise ValueError("invalid research family prior history")
    return value


def _generations(results_dir: Path) -> list[dict]:
    """Read every immutable plan in order; reject gaps, edits and orphan generations."""
    root = Path(results_dir)
    first = root / FAMILY
    directory = root / EPOCHS
    if directory.is_symlink() or first.is_symlink():
        raise ValueError("research family registration storage must not be a symlink")
    epochs = sorted(directory.glob("*.json")) if directory.exists() else []
    if any(not path.is_file() or path.is_symlink() for path in epochs):
        raise ValueError("invalid research family generation record")
    if not first.exists():
        if epochs:
            raise ValueError("original research family registration is missing")
        return []
    out = [_validate_registration(read_json(first), prior_sha=None)]
    for number, path in enumerate(epochs, start=1):
        if path.name != f"{number:06d}.json":
            raise ValueError("research family generation sequence is missing or reordered")
        value = _validate_registration(read_json(path), prior_sha=out[-1]["sha256"])
        if value["family_id"] in {row["family_id"] for row in out}:
            raise ValueError("repeated research family ID in generation chain")
        out.append(value)
    return out


def load_family(results_dir: Path):
    """Return the current plan after verifying the entire immutable generation chain."""
    generations = _generations(results_dir)
    return generations[-1] if generations else None


def _persist_new_generation(root: Path, generation: int, value: dict) -> None:
    """Exclusive, fsynced publication: never replace an older frozen plan."""
    directory = root / EPOCHS
    if directory.is_symlink():
        raise ValueError("unsafe research family generation directory")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{generation:06d}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(value, sort_keys=True, allow_nan=False, indent=1))
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        # A partial creation must never be mistaken for a completed
        # registration. Attempt removal; filesystem write failure still
        # leaves a blocking visible artifact for manual recovery.
        path.unlink(missing_ok=True)
        raise


def register_family(results_dir: Path, plan: dict) -> dict:
    """Freeze a single successor plan, preserving prior plans and all attempts.

    A new family does not make old search evidence prospective. Its
    prior_history snapshot includes all failures, legacy rows and past
    generation attempts. Frozen earlier registration files stay untouched.
    """
    root = Path(results_dir)
    plan = _plan(plan)
    with file_lock(root / LOCK):
        chain = _generations(root)
        previous = chain[-1] if chain else None
        prior = [r for r in history(root) if not r["holdout"]]
        if previous is not None:
            if _digest(plan) == previous["plan_sha256"]:
                raise ValueError("research family plan already registered; identical replacement prohibited")
            # Never open a new family while a predecessor's listed
            # planned members have not even been attempted. Otherwise
            # repeatedly switching the family could silently abandon
            # losing strategies without an initial trial.
            attempts_since = [
                row for row in prior
                if row.get("family_id") == previous["family_id"]
            ]
            planned = {_digest(m) for m in previous["plan"]["members"]}
            attempted = {_digest(_member(row)) for row in attempts_since}
            if not planned <= attempted:
                raise ValueError("previous research family has unattempted planned members")
            if any(row.get("status") == "reserved" for row in prior):
                raise ValueError("reserved research attempts block a successor family")
            # All linked prior trials must be covered by an existing
            # immutable generation; unknown links signal a missing
            # archived registration, never a fresh starting point.
            known_ids = {row["family_id"] for row in chain}
            if any("family_id" in row and row["family_id"] not in known_ids for row in prior):
                raise ValueError("research history links to a missing family registration")
            # Verify the current historical evidence still contains
            # everything the predecessor froze before advancing.
            prior_rows = previous["prior_history"]
            ids = {row.get("attempt_id") for row in prior}
            if any(row.get("attempt_id") not in ids for row in prior_rows):
                raise ValueError("prior research evidence is missing or changed")
            fixed = Counter(_digest(row) for row in prior_rows if row.get("status") != "reserved")
            if fixed - Counter(_digest(row) for row in prior):
                raise ValueError("prior research evidence is missing or changed")
        elif any("family_id" in row for row in prior):
            raise ValueError("linked attempts exist but their research family registration is missing")
        value = {
            "version": 1,
            "family_id": uuid4().hex,
            "registered_at": _now(),
            "plan": plan,
            "plan_sha256": _digest(plan),
            "code": code_provenance(),
            "prior_history": prior,
            "prior_history_sha256": _digest(prior),
        }
        if previous is not None:
            value["previous_registration_sha256"] = previous["sha256"]
        value["sha256"] = _digest(value)
        if previous is None:
            atomic_json(root / FAMILY, value)
        else:
            _persist_new_generation(root, len(chain), value)
    return value


def bind_family(results_dir: Path, entry: dict, rows: list[dict]):
    """Bind even unplanned research attempts before data access; holdout never enters the family."""
    family = load_family(results_dir)
    if family is None:
        if any("family_id" in r for r in rows):
            raise ValueError("linked attempts exist but their research family registration is missing")
    elif not entry["holdout"]:
        entry.update(family_id=family["family_id"], family_plan_sha256=family["plan_sha256"])


def family_report(results_dir: Path) -> dict:
    """Audit every research kind/symbol, including old evidence. Never pool incompatible returns."""
    root = Path(results_dir)
    with file_lock(root / LOCK):
        family = load_family(root)
        if family is None:
            raise ValueError("no research family registered")
        rows = [r for r in history(root) if not r["holdout"]]
        prior = family["prior_history"]
        prior_ids = {r["attempt_id"] for r in prior if "attempt_id" in r}
        current_ids = {r["attempt_id"] for r in rows if "attempt_id" in r}
        # Prior terminal outcomes/legacy rows cannot disappear or be edited after registration.
        fixed_prior = Counter(_digest(r) for r in prior if r.get("status") != "reserved")
        current = Counter(_digest(r) for r in rows)
        if prior_ids - current_ids or fixed_prior - current:
            raise ValueError("research history recorded at family registration is missing or changed")
        by_id = {r["attempt_id"]: r for r in rows if "attempt_id" in r}
        mutable = {"status", "finished_at", "result", "inputs", "window"}
        for row in prior:
            if row.get("status") == "reserved" and any(
                by_id[row["attempt_id"]].get(k) != v for k, v in row.items() if k not in mutable
            ):
                raise ValueError("reserved research evidence changed after family registration")
        planned = {_digest(m): {"member": m, "attempt_ids": []} for m in family["plan"]["members"]}
        reasons = []
        if prior:
            reasons.append("research evidence predates registration; this plan is retrospective")
        legacy = sum("attempt_id" not in r for r in rows)
        unplanned, unlinked, incomplete, changed_code, changed_inputs, conflicts = [], [], [], [], [], []
        outputs, input_sets = {}, {}
        for row in rows:
            ident = row.get("attempt_id")
            if ident in prior_ids or ident is None:
                continue  # retained and blocking as prior evidence, never relabelled prospectively
            key = _digest(_member(row))
            if key in planned:
                planned[key]["attempt_ids"].append(ident)
            else:
                unplanned.append(ident)
            if row.get("family_id") != family["family_id"] or row.get("family_plan_sha256") != family["plan_sha256"]:
                unlinked.append(ident)
            if _code_identity(row["code"]) != _code_identity(family["code"]):
                changed_code.append(ident)
            streams = row["inputs"]["streams"]
            if (
                row["status"] != "completed"
                or not row["result"]
                or row["result"].get("status") == "unavailable"
                or not streams
                or any(not s["complete"] for s in streams)
            ):
                incomplete.append(ident)
            else:
                inputs = (row["inputs"]["sha256"], tuple(row["window"]))
                if key in input_sets and input_sets[key] != inputs:
                    changed_inputs.append(ident)
                input_sets[key] = inputs
                # Repeated, exactly identified evaluations must agree, even when the first one lost money.
                signature = (key, _code_identity(row["code"]), row["inputs"]["sha256"], tuple(row["window"]))
                outcome = _digest(row["result"])
                if signature in outputs and outputs[signature] != outcome:
                    conflicts.append(ident)
                outputs[signature] = outcome
        missing = [key for key, value in planned.items() if not value["attempt_ids"]]
        for values, message in (
            (legacy, "legacy or unidentified research entries remain"),
            (missing, "planned evaluations are missing"),
            (unplanned, "unplanned settings, instruments, study kinds or windows were evaluated"),
            (unlinked, "research attempts are missing their prospective family link"),
            (incomplete, "failed, reserved, unavailable or incompletely identified attempts remain"),
            (changed_code, "source or runtime changed after registration"),
            (changed_inputs, "repeated planned settings used different input content or result windows"),
            (conflicts, "identical identified evaluations have conflicting outcomes"),
        ):
            if values:
                reasons.append(message)
        report = {
            "version": 1,
            "family_id": family["family_id"],
            "plan_sha256": family["plan_sha256"],
            "registration_sha256": family["sha256"],
            "history_sha256": _digest(rows),
            "prior_history_sha256": family["prior_history_sha256"],
            "prior_entries": len(prior),
            "research_entries": len(rows),
            "attempt_ids": [r["attempt_id"] for r in rows if "attempt_id" in r],
            "legacy_entries": legacy,
            "configurations": len(
                {
                    (r["kind"], r["instrument"], r["fingerprint"], _code_identity(r["code"]) if "code" in r else None)
                    for r in rows
                }
            ),
            "planned": planned,
            "missing_members": missing,
            "unplanned_attempt_ids": unplanned,
            "unlinked_attempt_ids": unlinked,
            "incomplete_attempt_ids": incomplete,
            "changed_code_attempt_ids": changed_code,
            "changed_input_attempt_ids": changed_inputs,
            "conflicting_attempt_ids": conflicts,
            "prospective_coverage_complete": not reasons,
            "reasons": reasons,
            "qualified": False,
            "limitation": "Coverage audit only; no joint inference across symbols/studies, qualified Sharpe, recovery rule or trading gate. Retain external research evidence too.",
        }
        atomic_json(root / REPORT, report)
    return report
