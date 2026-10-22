"""
Promotion binds the exact artifact that was validated (G007-F5, ChatGPT's audit).

`lab research promote` writes approval.json next to the hypothesis: the SHA-256 of the source
the operator read, the SHA-256 of the train and test evidence that made it a candidate, and the
approved parameters. Trading a promoted signal re-checks all three, at start-up and again at the
moment the code is handed to its worker; any change means re-validate and promote again.

Risk profile and sizing settings are recorded, not enforced: the statistical gates judge a
strategy at its own capital, and leverage is a sizing decision taken after (Claude, G002). A
different profile does not invalidate the evidence of an edge; different code or parameters do.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

EVIDENCE = ("train.json", "test.json")


class ApprovalError(Exception):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_approval(hdir: Path, source: bytes, params: dict, config_snapshot: dict | None = None) -> dict:
    """Record what was approved. `source` is the exact bytes the operator was shown."""
    missing = [n for n in EVIDENCE if not (hdir / n).exists()]
    if missing:
        raise ApprovalError(f"{hdir.name}: no {', '.join(missing)}; only validated candidates can be promoted")
    record = {
        "hypothesis": hdir.name,
        "source_sha256": sha256_bytes(source),
        "evidence_sha256": {n: sha256_file(hdir / n) for n in EVIDENCE},
        "params": params,
        "config_at_approval": config_snapshot or {},
        "approved_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (hdir / "approval.json").write_text(json.dumps(record, indent=1, sort_keys=True, default=str))
    return record


def verify_approval(hdir: Path, params: dict) -> str:
    """Check the promoted artifact is the approved one. Returns the approved source digest."""
    p = hdir / "approval.json"
    if not p.exists():
        raise ApprovalError(f"{hdir.name}: promoted without an approval manifest; promote it again")
    try:
        rec = json.loads(p.read_text())
        approved_src, approved_ev, approved_params = rec["source_sha256"], rec["evidence_sha256"], rec["params"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise ApprovalError(f"{hdir.name}: unreadable approval manifest ({e}); promote it again") from e
    if sha256_file(hdir / "signal.py") != approved_src:
        raise ApprovalError(f"{hdir.name}: signal.py changed since it was promoted; re-validate and promote again")
    for name, digest in approved_ev.items():
        if not (hdir / name).exists() or sha256_file(hdir / name) != digest:
            raise ApprovalError(f"{hdir.name}: {name} changed since promotion; the approval no longer applies")
    if dict(params) != dict(approved_params):
        raise ApprovalError(
            f"{hdir.name}: parameters {params} differ from the approved {approved_params}; "
            "validate those parameters as their own hypothesis"
        )
    return approved_src
