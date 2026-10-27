"""F0: read-only, exact-revision development-candidate inventory.

A Git commit and a locally hashed lock graph are necessary F0 evidence but
NOT a signed release, reproducible OS image, independent review or trading authority.
The manifest intentionally excludes broker identifiers, credentials and raw market data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

VERSION = 1
KIND = "F0_SOURCE_INVENTORY_UNATTESTED"
SCOPE = "docs/architecture/f0-reference-scope.json"
INPUTS = (
    ".github/workflows/ci.yml",
    "Dockerfile",
    "compose.app.yml",
    "docker-compose.yml",
    "pyproject.toml",
    "requirements-build.lock",
    "requirements.lock",
    "src/lab/default.toml",
    "uv.lock",
    SCOPE,
)
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
COMMIT = re.compile(r"[a-f0-9]{40}\Z")
MISSING_EVIDENCE = (
    "immutable signed release/artifact attestation",
    "actual deployment OS package inventory/digest",
    "runtime configuration and raw data provenance",
    "independent code and security approval",
    "economic, broker and paper acceptance evidence",
)


class FoundationError(ValueError):
    """Local candidate inventory could not be established or verified."""


def _git(root: Path, *arguments: str) -> str:
    proc = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise FoundationError("cannot obtain complete Git candidate identity")
    return proc.stdout.strip()


def _root(directory: Path) -> Path:
    directory = directory.expanduser().resolve()
    git_root = _git(directory, "rev-parse", "--show-toplevel")
    if Path(git_root).resolve() != directory:
        raise FoundationError("candidate root must be the top level of a Git checkout")
    return directory


def _candidate(root: Path) -> tuple[str, str]:
    if _git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise FoundationError("candidate working tree has uncommitted or untracked files")
    commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    tree = _git(root, "rev-parse", "--verify", "HEAD^{tree}")
    if not COMMIT.fullmatch(commit) or not COMMIT.fullmatch(tree):
        raise FoundationError("candidate Git object identity is invalid")
    return commit, tree


def _hash_input(root: Path, relative: str) -> str:
    target = root / relative
    if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(root):
        raise FoundationError(f"required pinned input is unavailable: {relative}")
    return hashlib.sha256(target.read_bytes()).hexdigest()


def inventory(directory: Path) -> dict[str, object]:
    """Produce an identity bound to the clean checkout, never an approval."""
    root = _root(directory)
    commit, tree = _candidate(root)
    digests = {path: _hash_input(root, path) for path in INPUTS}
    for relative, digest in digests.items():
        tracked = subprocess.run(
            ["git", "show", f"{commit}:{relative}"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if tracked.returncode != 0 or hashlib.sha256(tracked.stdout).hexdigest() != digest:
            raise FoundationError("pinned file differs from exact candidate revision")
    if _git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise FoundationError("candidate changed during evidence collection")
    try:
        scope = json.loads((root / SCOPE).read_bytes())
    except (ValueError, UnicodeError) as exc:
        raise FoundationError("invalid F0 reference scope JSON") from exc
    if (
        type(scope) is not dict
        or scope.get("schema_version") != 1
        or scope.get("stage") != "F0_REFERENCE_SLICE_UNQUALIFIED"
        or type(scope.get("operator")) is not dict
        or scope["operator"].get("authority") != "NONE_IN_F0"
        or scope["operator"].get("account_mode") != "PAPER_ONLY"
    ):
        raise FoundationError("F0 scope cannot assert execution authority or qualification")
    return {
        "schema_version": VERSION,
        "kind": KIND,
        "candidate_commit": commit,
        "candidate_tree": tree,
        "reference_scope_sha256": digests[SCOPE],
        "pinned_inputs_sha256": digests,
        "observed_environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "qualification": "BLOCKED",
        "broker_authorization": False,
        "release_authorization": False,
        "unresolved_evidence": list(MISSING_EVIDENCE),
    }


def _encoded(record: dict[str, object]) -> str:
    return json.dumps(record, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"


def create(directory: Path, output: Path) -> Path:
    """Create a write-once evidence snapshot outside the Git checkout."""
    record = inventory(directory)
    root = _root(directory)
    output = output.expanduser().absolute()
    if output.resolve().is_relative_to(root):
        raise FoundationError("inventory output must remain outside the Git working tree")
    # Exclusive creation avoids replacing a previously captured candidate receipt.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(_encoded(record))
        stream.flush()
        os.fsync(stream.fileno())
    return output


def verify(directory: Path, evidence: Path) -> dict[str, object]:
    """Recompute all claims; never trust a self-reported inventory result."""
    if evidence.is_symlink() or not evidence.is_file() or evidence.stat().st_size > 128_000:
        raise FoundationError("candidate inventory is missing, symlinked or exceeds size limit")
    try:
        recorded = json.loads(
            evidence.read_bytes(),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
        )
    except (UnicodeError, ValueError) as exc:
        raise FoundationError("invalid recorded F0 candidate inventory") from exc
    if type(recorded) is not dict or recorded != inventory(directory):
        raise FoundationError("candidate inventory differs from exact source, lock graph or environment")
    return {
        "candidate_commit": recorded["candidate_commit"],
        "source_inventory_matches": True,
        "qualification": "BLOCKED",
        "release_authorization": False,
        "unresolved_evidence": list(MISSING_EVIDENCE),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    sub = parser.add_subparsers(dest="action", required=True)
    create_cmd = sub.add_parser("create", help="write a new write-once local development candidate inventory")
    create_cmd.add_argument("--out", type=Path, required=True)
    check_cmd = sub.add_parser("verify", help="verify source, locks, scope and observed environment")
    check_cmd.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "create":
            create(args.root, args.out)
            print(json.dumps({"created": True, "qualification": "BLOCKED", "release_authorization": False}))
        else:
            print(json.dumps(verify(args.root, args.input), sort_keys=True))
    except (FoundationError, OSError) as exc:
        # Never report a broker account, local config, private evidence or environment variables.
        print(
            f"F0 inventory refused: {type(exc).__name__}; check clean Git checkout and pinned inputs", file=sys.stderr
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
