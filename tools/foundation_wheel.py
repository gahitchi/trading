"""F0 source-to-wheel parity and two-build byte identity (unsigned evidence).

Compares two separately invoked local wheel builds, not two independent builders.
Successful evidence is never a release, broker or strategy authorization.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import stat
import sys
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from tools import foundation_manifest as foundation

KIND = "F0_WHEEL_REPEAT_BUILD_OBSERVATION_UNATTESTED"
MAX_WHEEL_BYTES = 100 * 1024 * 1024
MAX_MEMBERS = 5000


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _tracked_sources(root: Path) -> dict[str, bytes]:
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--", "src/lab"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if proc.returncode:
        raise foundation.FoundationError("cannot enumerate tracked package sources")
    items = {}
    for raw in filter(None, proc.stdout.split(b"\x00")):
        try:
            relative = raw.decode("utf-8")
        except UnicodeError as exc:
            raise foundation.FoundationError("package source path is not UTF-8") from exc
        path = PurePosixPath(relative)
        if path.parts[:2] != ("src", "lab") or ".." in path.parts:
            raise foundation.FoundationError("unconfined package source path")
        file = root / relative
        if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(root):
            raise foundation.FoundationError("package source is missing or symlinked")
        items[str(PurePosixPath(*path.parts[1:]))] = file.read_bytes()
    if not items or "lab/__init__.py" not in items:
        raise foundation.FoundationError("no complete tracked Python package")
    return items


def _wheel(path: Path, sources: dict[str, bytes], project: dict) -> dict:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size < MAX_WHEEL_BYTES:
        raise foundation.FoundationError("wheel is missing, symlinked or too large")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_MEMBERS:
                raise foundation.FoundationError("wheel member inventory is empty or too large")
            names = set()
            actual = {}
            expanded_bytes = 0
            for item in entries:
                name = item.filename
                pure = PurePosixPath(name)
                if (
                    not name
                    or name.startswith("/")
                    or "\\" in name
                    or "\x00" in name
                    or ".." in pure.parts
                    or name in names
                    or stat.S_IFMT(item.external_attr >> 16) == stat.S_IFLNK
                ):
                    raise foundation.FoundationError("wheel has an unsafe or duplicate member")
                if item.file_size > MAX_WHEEL_BYTES:
                    raise foundation.FoundationError("wheel member exceeds permitted expanded size")
                expanded_bytes += item.file_size
                if expanded_bytes > MAX_WHEEL_BYTES * 2:
                    raise foundation.FoundationError("wheel expansion exceeds bounded evidence budget")
                names.add(name)
                if not item.is_dir():
                    actual[name] = archive.read(item)
            packaged = {name: data for name, data in actual.items() if name.startswith("lab/")}
            if packaged != sources:
                raise foundation.FoundationError("wheel package bytes differ from committed sources")
            metadata_files = [name for name in actual if name.endswith(".dist-info/METADATA")]
            if len(metadata_files) != 1:
                raise foundation.FoundationError("wheel must have exactly one distribution metadata file")
            prefix = metadata_files[0].removesuffix("METADATA")
            expected_name = project["name"].replace("-", "_") + "-" + project["version"] + ".dist-info/"
            if prefix.lower() != expected_name.lower():
                raise foundation.FoundationError("wheel distribution identity mismatch")
            if any(not (name.startswith("lab/") or name.startswith(prefix)) for name in actual):
                raise foundation.FoundationError("wheel contains an unexpected executable or data payload")
            metadata = BytesParser().parsebytes(actual[prefix + "METADATA"], headersonly=True)
            if metadata.get("Name", "").lower().replace("_", "-") != project["name"].lower():
                raise foundation.FoundationError("wheel package name differs from pyproject")
            if metadata.get("Version") != project["version"]:
                raise foundation.FoundationError("wheel version differs from pyproject")
            if prefix + "WHEEL" not in actual or prefix + "RECORD" not in actual:
                raise foundation.FoundationError("wheel metadata or RECORD is absent")
            wheel_headers = BytesParser().parsebytes(actual[prefix + "WHEEL"], headersonly=True)
            if wheel_headers.get("Wheel-Version") != "1.0" or not wheel_headers.get("Tag"):
                raise foundation.FoundationError("wheel header is missing an expected format or tag")
            rows = list(csv.reader(io.StringIO(actual[prefix + "RECORD"].decode("utf-8"))))
            recorded = set()
            for row in rows:
                if len(row) != 3 or row[0] in recorded or row[0] not in actual:
                    raise foundation.FoundationError("wheel RECORD is incomplete or duplicated")
                recorded.add(row[0])
                name, digest, size = row
                if name == prefix + "RECORD" and digest == size == "":
                    continue
                if not digest.startswith("sha256=") or size != str(len(actual[name])):
                    raise foundation.FoundationError("wheel RECORD member metadata mismatch")
                expected = base64.urlsafe_b64encode(hashlib.sha256(actual[name]).digest()).rstrip(b"=").decode()
                if digest != "sha256=" + expected:
                    raise foundation.FoundationError("wheel RECORD member digest mismatch")
            if recorded != set(actual):
                raise foundation.FoundationError("wheel RECORD does not cover every member")
            return {
                "wheel_sha256": _digest(path.read_bytes()),
                "distribution": project["name"],
                "version": project["version"],
                "source_members": len(sources),
                "wheel_members": len(actual),
            }
    except (zipfile.BadZipFile, KeyError, UnicodeError, ValueError) as exc:
        if isinstance(exc, foundation.FoundationError):
            raise
        raise foundation.FoundationError("wheel cannot be inspected safely") from exc


def observation(root: Path, first: Path, second: Path) -> dict:
    root = root.expanduser().resolve()
    candidate = foundation.inventory(root)
    if first.resolve() == second.resolve():
        raise foundation.FoundationError("repeat-build comparison requires two distinct artifact paths")
    sources = _tracked_sources(root)
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject.get("project")
    if not isinstance(project, dict) or not all(isinstance(project.get(k), str) for k in ("name", "version")):
        raise foundation.FoundationError("invalid distribution identity in pyproject")
    one = _wheel(first, sources, project)
    two = _wheel(second, sources, project)
    if one != two:
        raise foundation.FoundationError("repeat-built wheel contents differ")
    return {
        "schema_version": 1,
        "kind": KIND,
        "candidate_commit": candidate["candidate_commit"],
        "candidate_tree": candidate["candidate_tree"],
        "source_inventory_sha256": _digest(foundation._encoded(candidate).encode()),
        "wheel": one,
        "qualification": "BLOCKED",
        "release_authorization": False,
        "broker_authorization": False,
        "limitations": [
            "repeat builds were produced on one runner, not by independent builders",
            "wheel contents match only the committed package tree; no signer or registry custody",
            "no real deployment, external security review, market or broker acceptance",
        ],
    }


def capture(root: Path, first: Path, second: Path, receipt: Path) -> Path:
    record = observation(root, first, second)
    destination = receipt.expanduser().absolute()
    if destination.resolve().is_relative_to(root.resolve()):
        raise foundation.FoundationError("evidence must be saved outside the source checkout")
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(foundation._encoded(record))
        stream.flush()
        os.fsync(stream.fileno())
    return destination


def verify(root: Path, first: Path, second: Path, receipt: Path) -> dict:
    if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_size > 128_000:
        raise foundation.FoundationError("repeat-build evidence is unavailable or unconfined")
    try:
        saved = json.loads(
            receipt.read_bytes(), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number"))
        )
    except (ValueError, UnicodeError) as exc:
        raise foundation.FoundationError("invalid F0 wheel evidence") from exc
    current = observation(root, first, second)
    if saved != current:
        raise foundation.FoundationError("wheel receipt no longer matches exact candidate and artifacts")
    return {"candidate_commit": current["candidate_commit"], "verified": True, "qualification": "BLOCKED"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--second", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("capture").add_argument("--out", type=Path, required=True)
    sub.add_parser("verify").add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "capture":
            capture(args.root, args.first, args.second, args.out)
            print(json.dumps({"created": True, "qualification": "BLOCKED"}))
        else:
            print(json.dumps(verify(args.root, args.first, args.second, args.input)))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"F0 wheel parity refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
