"""Report release blockers. This is an evidence checklist, not a certification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def blockers(record: dict, root: Path) -> list[str]:
    if record.get("version") != 1 or not isinstance(record.get("gates"), list) or not record["gates"]:
        return ["invalid or empty release criteria"]
    out = []
    seen = set()
    for gate in record["gates"]:
        if not isinstance(gate, dict) or not isinstance(gate.get("id"), str) or gate["id"] in seen:
            out.append("invalid or duplicate gate")
            continue
        ident = gate["id"]
        seen.add(ident)
        if gate.get("status") != "passed":
            out.append(f"{ident}: {gate.get('criterion', 'evidence required')}")
        evidence = gate.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            out.append(f"{ident}: evidence is missing")
        elif any(not isinstance(p, str) or not (root / p).is_file() for p in evidence):
            out.append(f"{ident}: evidence file is missing")
        if gate.get("status") == "passed" and not gate.get("review_commit"):
            out.append(f"{ident}: reviewed commit is missing")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--criteria", type=Path, default=Path(__file__).resolve().parents[1] / "docs/release-criteria.json"
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        reasons = blockers(json.loads(args.criteria.read_text()), root)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        reasons = [f"cannot read release evidence: {exc}"]
    print(json.dumps({"ready": not reasons, "blockers": reasons}, indent=2))
    return 1 if reasons else 0


if __name__ == "__main__":
    raise SystemExit(main())
