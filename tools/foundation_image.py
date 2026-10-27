"""F0.2 read-only container artifact observation, never a release attestation.

An image ID is a content-addressed local Docker image, not proof of two
independently reproducible builds, builder identity, runtime custody or security.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from tools import foundation_manifest as source

HEX256 = re.compile(r"sha256:[a-f0-9]{64}\\Z")
HASH = re.compile(r"[a-f0-9]{64}\\Z")
KIND = "F0_CONTAINER_BUILD_OBSERVATION_UNATTESTED"


def _json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128_000:
        raise source.FoundationError("invalid or excessive F0 image observation file")
    try:
        value = json.loads(
            path.read_bytes(),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
        )
    except (ValueError, UnicodeError) as exc:
        raise source.FoundationError("invalid F0 image observation JSON") from exc
    if type(value) is not dict:
        raise source.FoundationError("F0 image observation must be an object")
    return value


def _image(path: Path) -> dict:
    item = _json(path)
    if set(item) != {"id", "os", "architecture", "user", "rootfs_layers"}:
        raise source.FoundationError("F0 image observation shape mismatch")
    layers = item["rootfs_layers"]
    if (
        type(item["id"]) is not str
        or not HEX256.fullmatch(item["id"])
        or item["os"] != "linux"
        or item["architecture"] not in ("amd64", "arm64")
        or item["user"] != "lab"
        or type(layers) is not list
        or not 1 <= len(layers) <= 256
        or any(type(x) is not str or not HEX256.fullmatch(x) for x in layers)
    ):
        raise source.FoundationError("F0 unprivileged Linux image identity is invalid")
    return item


def _observed(path: Path) -> dict:
    item = _json(path)
    if set(item) != {
        "os_inventory_sha256",
        "installed_python_sha256",
        "installed_python_count",
        "nautilus_version",
        "lab_version",
    }:
        raise source.FoundationError("F0 installed dependency observation shape mismatch")
    if any(type(item[k]) is not str or not HASH.fullmatch(item[k]) for k in ("os_inventory_sha256", "installed_python_sha256")):
        raise source.FoundationError("F0 installed dependency digests invalid")
    if (
        type(item["installed_python_count"]) is not int
        or not 1 <= item["installed_python_count"] <= 3000
        or item["nautilus_version"] != "2.0.0rc7"
        or item["lab_version"] != "0.2.0"
    ):
        raise source.FoundationError("F0 pinned runtime dependency observation invalid")
    return item


def observation(root: Path, image: Path, installed: Path) -> dict:
    candidate = source.inventory(root)
    inspected_image = _image(image)
    actual_packages = _observed(installed)
    return {
        "schema_version": 1,
        "kind": KIND,
        "candidate_commit": candidate["candidate_commit"],
        "candidate_tree": candidate["candidate_tree"],
        "source_inventory_sha256": hashlib.sha256(source._encoded(candidate).encode()).hexdigest(),
        "image": inspected_image,
        "installed_runtime": actual_packages,
        "qualification": "BLOCKED",
        "broker_authorization": False,
        "release_authorization": False,
        "limitations": [
            "unsigned single-run observation, not reproducible two-builder proof",
            "no independent container-builder or runtime deployment attestation",
            "no audited OS/CVE, SBOM supply-chain review or signed image digest",
            "no broker, market, accounting, statistics or security acceptance",
        ],
    }


def capture(root: Path, image: Path, installed: Path, output: Path) -> Path:
    record = observation(root, image, installed)
    out = output.expanduser().absolute()
    if out.resolve().is_relative_to(root.resolve()):
        raise source.FoundationError("F0 image observation must be stored outside Git checkout")
    fd = os.open(out, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(source._encoded(record))
        stream.flush()
        os.fsync(stream.fileno())
    return out


def verify(root: Path, image: Path, installed: Path, evidence: Path) -> dict:
    recorded = _json(evidence)
    expected = observation(root, image, installed)
    if recorded != expected:
        raise source.FoundationError("F0 image evidence does not match exact candidate and observed runtime")
    return {
        "candidate_commit": expected["candidate_commit"],
        "image_id": expected["image"]["id"],
        "build_observation_matches": True,
        "qualification": "BLOCKED",
        "release_authorization": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--installed", type=Path, required=True)
    command = parser.add_subparsers(dest="action", required=True)
    command.add_parser("capture").add_argument("--out", type=Path, required=True)
    command.add_parser("verify").add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "capture":
            capture(args.root, args.image, args.installed, args.out)
            answer = {"recorded": True, "qualification": "BLOCKED", "release_authorization": False}
        else:
            answer = verify(args.root, args.image, args.installed, args.input)
        print(json.dumps(answer, sort_keys=True))
    except (OSError, ValueError) as exc:
        print(f"F0 image evidence refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
