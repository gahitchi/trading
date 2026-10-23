"""
Promotion binds the exact artifact that was validated (G007-F5, ChatGPT's audit).

`lab research promote` writes approval.json next to the hypothesis: the SHA-256 of the source
the operator read, the SHA-256 of the train and test evidence that made it a candidate, and the
approved parameters. Trading a promoted signal re-checks all three, at start-up and again at the
moment the code is handed to its worker; any change means re-validate and promote again.

Validation binds the evaluated source BEFORE promotion, not just the bytes present when
the operator approves. Risk, costs, capital and trading context also change the executed
path (drawdown stops, financing, rounding); deployment must match that evaluated context.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

EVIDENCE = ("train.json", "test.json")


def deployment_context(snapshot: dict, *, live: bool = False, tickers=None) -> dict:
    if not snapshot:
        return {}
    # Capital is not bound: it changes only share rounding, and binding it would refuse every promoted
    # signal whenever the paper allocation differs from the research capital. At the daily speed the
    # intraday-only trading settings (delay, collar, flat at close, intraday holdout) do not act either.
    keys = ("risk", "portfolio", "costs", "margin")
    trading = snapshot["trading"]
    return {
        **{k: snapshot[k] for k in keys},
        "trading": trading if trading.get("speed") != "daily" else {"speed": "daily"},
        "universe": {t: snapshot["universe"][t] for t in sorted(tickers or snapshot["universe"])},
    }


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
    evidence = {}
    records = []
    for name in EVIDENCE:
        raw = (hdir / name).read_bytes()
        evidence[name] = sha256_bytes(raw)
        try:
            result = json.loads(raw)
            validation = result["validation"]
            passed = result["passed" if name == "train.json" else "survived"] is True
            context = deployment_context(config_snapshot or {}, tickers=result.get("tickers"))
            if (
                not passed
                or validation["source_sha256"] != sha256_bytes(source)
                or validation["params"] != params
                or validation["deployment"] != context
            ):
                raise ValueError("source, parameters, configuration or verdict differs")
            records.append(validation)
        except (ValueError, KeyError, TypeError) as exc:
            raise ApprovalError(f"{hdir.name}: {name} is not evidence for this artifact; re-validate ({exc})") from exc
    if records[0] != records[1]:
        raise ApprovalError("train and test validated different artifacts/configurations; re-validate")
    record = {
        "hypothesis": hdir.name,
        "source_sha256": sha256_bytes(source),
        "evidence_sha256": evidence,
        "params": params,
        "config_at_approval": config_snapshot or {},
        "validation": records[0],
        "approved_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (hdir / "approval.json").write_text(json.dumps(record, indent=1, sort_keys=True, default=str))
    return record


def verify_approval(hdir: Path, params: dict, *, context: dict | None = None) -> str:
    """Check the promoted artifact is the approved one. Returns the approved source digest."""
    p = hdir / "approval.json"
    if not p.exists():
        raise ApprovalError(f"{hdir.name}: promoted without an approval manifest; promote it again")
    try:
        rec = json.loads(p.read_text())
        approved_src, approved_ev, approved_params = rec["source_sha256"], rec["evidence_sha256"], rec["params"]
        if set(approved_ev) != set(EVIDENCE) or rec["validation"]["source_sha256"] != approved_src:
            raise ValueError("missing validation binding")
    except (ValueError, KeyError, TypeError) as e:
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
    if context is not None and rec["validation"]["deployment"] != context:
        raise ApprovalError(f"{hdir.name}: deployment configuration differs from validation; re-validate")
    return approved_src
