"""Prospective search plans and whole-directory coverage, never a statistical gate.

One immutable plan covers every research attempt in a results directory. There is no filter,
replacement plan or recovery option that could quietly discard an unsuccessful search.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from uuid import uuid4

from lab.persistence import atomic_json, file_lock, read_json
from lab.trials import _code_identity, _encoded, _frozen, _now, code_provenance, history

FAMILY = "research_family.json"
LOCK = "research_family.lock"
REPORT = "research_family_report.json"
KINDS = {"micro-study", "maker-study", "fast-backtest", "carry-study"}


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


def load_family(results_dir: Path):
    """Read the authoritative registration. Callers serialize registration/reservation with LOCK."""
    path = Path(results_dir) / FAMILY
    if not path.exists():
        return None
    value = read_json(path)
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or not _hex(value.get("family_id"), 32)
        or not isinstance(value.get("registered_at"), str)
        or "sha256" not in value
        or value["sha256"] != _digest({k: v for k, v in value.items() if k != "sha256"})
    ):
        raise ValueError("invalid research family registration or digest")
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


def register_family(results_dir: Path, plan: dict) -> dict:
    """Freeze plan, code and all earlier research evidence before further reservations can begin."""
    root = Path(results_dir)
    plan = _plan(plan)
    with file_lock(root / LOCK):
        if load_family(root) is not None:
            raise ValueError("research family already registered; replacement or amendment is not allowed")
        prior = [r for r in history(root) if not r["holdout"]]
        if any("family_id" in r for r in prior):
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
        value["sha256"] = _digest(value)
        atomic_json(root / FAMILY, value)
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
