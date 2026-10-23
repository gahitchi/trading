"""Check complete release evidence for an explicitly identified candidate; not a certification."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

REQUIRED_GATES = frozenset(
    {
        "code-review",
        "broker-lifecycle",
        "research-calibration",
        "market-data-and-execution",
        "adaptive-product",
        "operations-and-build",
        "independent-security",
    }
)
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def blockers(record: dict, root: Path, candidate: str | None = None) -> list[str]:
    if (
        not isinstance(record, dict)
        or type(record.get("version")) is not int
        or record["version"] != 1
        or not isinstance(record.get("gates"), list)
        or not record["gates"]
    ):
        return ["invalid or empty release criteria"]
    out = []
    seen = set()
    root = root.resolve()
    for gate in record["gates"]:
        if (
            not isinstance(gate, dict)
            or not isinstance(gate.get("id"), str)
            or gate["id"] not in REQUIRED_GATES
            or gate["id"] in seen
        ):
            out.append("invalid, unknown or duplicate gate")
            continue
        ident = gate["id"]
        seen.add(ident)
        passed = gate.get("status") == "passed"
        if not passed:
            out.append(f"{ident}: {gate.get('criterion', 'evidence required')}")
        evidence = gate.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            out.append(f"{ident}: evidence is missing")
            continue
        hashes = gate.get("evidence_sha256", {})
        if passed and (not isinstance(hashes, dict) or set(hashes) != set(p for p in evidence if isinstance(p, str))):
            out.append(f"{ident}: exact evidence digests are required")
            hashes = {}
        for relative in evidence:
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
                out.append(f"{ident}: evidence path must be repository-relative")
                continue
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                out.append(f"{ident}: evidence file is missing or outside the repository")
                continue
            if passed:
                digest = hashes.get(relative)
                if (
                    not isinstance(digest, str)
                    or not DIGEST.fullmatch(digest)
                    or hashlib.sha256(path.read_bytes()).hexdigest() != digest
                ):
                    out.append(f"{ident}: evidence digest mismatch: {relative}")
        if passed:
            reviewed = gate.get("review_commit")
            if not isinstance(reviewed, str) or not SHA.fullmatch(reviewed):
                out.append(f"{ident}: full reviewed commit SHA is required")
            elif candidate is None or reviewed != candidate:
                out.append(f"{ident}: evidence does not review the requested candidate")
            if not isinstance(gate.get("reviewer"), str) or not gate["reviewer"].strip():
                out.append(f"{ident}: reviewer is missing")
    for ident in sorted(REQUIRED_GATES - seen):
        out.append(f"{ident}: required gate is missing")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--criteria", type=Path, default=root / "docs/release-criteria.json")
    parser.add_argument("--candidate", default="HEAD", help="commit/ref whose evidence is being checked (default HEAD)")
    args = parser.parse_args(argv)
    candidate = None
    try:
        candidate = subprocess.check_output(
            ["git", "rev-parse", "--verify", "--end-of-options", f"{args.candidate}^{{commit}}"],
            cwd=root,
            text=True,
            stderr=subprocess.PIPE,
        ).strip()
        reasons = blockers(json.loads(args.criteria.read_text()), root, candidate)
        if subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root, text=True
        ).strip():
            reasons.append("working tree is dirty: checks do not describe only the candidate commit")
    except (OSError, ValueError, TypeError, AttributeError, subprocess.CalledProcessError) as exc:
        reasons = [f"cannot read release evidence: {exc}"]
    print(json.dumps({"candidate": candidate, "ready": not reasons, "blockers": reasons}, indent=2))
    return 1 if reasons else 0


if __name__ == "__main__":
    raise SystemExit(main())
