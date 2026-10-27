"""Local F0.4 input custody: private byte identities, not market data qualification.

Requires a clean source candidate, local research config and raw SPY daily
data. Records *only* role, SHA-256 and byte count; no account numbers,
symbols from private account sources, passwords or source file contents.
Receipts never leave the operator machine automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tomllib
from pathlib import Path

from tools import foundation_manifest as foundation

KIND = "F0_LOCAL_DATA_CONFIG_BYTES_UNATTESTED"
ROLES = {
    "configuration": "lab.toml",
    "market_manifest": "data/manifest.json",
    "reference_bars": "data/SPY.parquet",
}
MAX_CONFIG = 2 * 1024 * 1024
MAX_DATA = 20 * 1024 * 1024 * 1024


def _read_identity(
    root: Path, relative: str | Path, max_bytes: int, *, local_config_override: bool = False
) -> tuple[str, int, bytes | None]:
    target = root / relative
    if (
        target.is_symlink()
        or not target.is_file()
        or (not local_config_override and not target.resolve().is_relative_to(root))
    ):
        raise foundation.FoundationError("required local input file missing, unsafe or nonregular")
    # No symbolic links and no mutable writing: constrain the input to a
    # single opened descriptor, and detect changes in identity during hashing.
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > max_bytes:
            raise foundation.FoundationError("required local input has unexpected size or type")
        digest = hashlib.sha256()
        sampled = bytearray() if max_bytes == MAX_CONFIG else None
        observed = 0
        while chunk := os.read(fd, 1024 * 1024):
            observed += len(chunk)
            if observed > max_bytes:
                raise foundation.FoundationError("required local input exceeds size budget")
            digest.update(chunk)
            if sampled is not None:
                sampled.extend(chunk)
        after = os.fstat(fd)
        if (
            before.st_ino != after.st_ino
            or before.st_dev != after.st_dev
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or observed != before.st_size
            or observed != after.st_size
        ):
            raise foundation.FoundationError("local input changed while hashing")
        return digest.hexdigest(), observed, bytes(sampled) if sampled is not None else None
    finally:
        os.close(fd)


def observation(directory: Path, *, config: Path | None = None) -> dict:
    root = directory.expanduser().resolve()
    inventory = foundation.inventory(root)
    observed = {}
    for role, relative in ROLES.items():
        chosen = config if role == "configuration" and config is not None else relative
        digest, count, data = _read_identity(
            root,
            chosen,
            MAX_DATA if role == "reference_bars" else MAX_CONFIG,
            local_config_override=role == "configuration" and config is not None,
        )
        observed[role] = {"sha256": digest, "bytes": count}
        if role == "configuration":
            try:
                parsed_config = tomllib.loads(data.decode("utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise foundation.FoundationError("unreadable research configuration") from exc
            paper = parsed_config.get("paper", {})
            if not isinstance(paper, dict):
                raise foundation.FoundationError("reference broker configuration is not a table")
            account = paper.get("account_id", "")
            port = paper.get("port")
            if (
                not isinstance(parsed_config.get("universe"), dict)
                or not isinstance(parsed_config.get("trading"), dict)
                or set(parsed_config["universe"]) != {"SPY"}
                or parsed_config["universe"]["SPY"] != "ARCX"
                or parsed_config["trading"].get("speed") != "daily"
                or not isinstance(account, str)
                or (account and not account.startswith("DU"))
                or (port is not None and (type(port) is not int or not 1 <= port <= 65535 or port in (4001, 7496)))
                or paper.get("mode", "paper") != "paper"
            ):
                raise foundation.FoundationError("reference configuration is not isolated SPY/PAPER daily scope")
        if role == "market_manifest":
            try:
                manifest = json.loads(
                    data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON number"))
                )
            except (UnicodeError, ValueError) as exc:
                raise foundation.FoundationError("invalid local market data manifest") from exc
            if not isinstance(manifest, dict) or not isinstance(manifest.get("SPY"), dict):
                raise foundation.FoundationError("reference dataset is missing from source manifest")
    bars = root / ROLES["reference_bars"]
    with bars.open("rb") as stream:
        if stream.read(4) != b"PAR1":
            raise foundation.FoundationError("reference data is not a Parquet container")
        stream.seek(-4, os.SEEK_END)
        if stream.read(4) != b"PAR1":
            raise foundation.FoundationError("reference data Parquet footer is absent")
    # Re-check source and local inputs against mutation between first/last read.
    if foundation.inventory(root) != inventory:
        raise foundation.FoundationError("source candidate changed during input observation")
    for role, relative in ROLES.items():
        chosen = config if role == "configuration" and config is not None else relative
        digest, count, _ = _read_identity(
            root,
            chosen,
            MAX_DATA if role == "reference_bars" else MAX_CONFIG,
            local_config_override=role == "configuration" and config is not None,
        )
        if observed[role] != {"sha256": digest, "bytes": count}:
            raise foundation.FoundationError("local inputs changed during input observation")
    return {
        "schema_version": 1,
        "kind": KIND,
        "candidate_commit": inventory["candidate_commit"],
        "source_inventory_sha256": hashlib.sha256(foundation._encoded(inventory).encode()).hexdigest(),
        "local_inputs": observed,
        "qualification": "BLOCKED",
        "broker_authorization": False,
        "release_authorization": False,
        "unverified": [
            "actual source authenticity, completeness, and point-in-time history",
            "data vendor permissions, availability timestamps and revisions",
            "account identity, fee schedules, positions and broker observations",
            "independent review and immutable externally trusted custody",
        ],
    }


def capture(root: Path, output: Path, *, config: Path | None = None) -> Path:
    result = observation(root, config=config)
    destination = output.expanduser().absolute()
    if destination.resolve().is_relative_to(root.resolve()):
        raise foundation.FoundationError("local evidence must be outside the source checkout")
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(foundation._encoded(result))
        stream.flush()
        os.fsync(stream.fileno())
    return destination


def verify(root: Path, evidence: Path, *, config: Path | None = None) -> dict:
    if evidence.is_symlink() or not evidence.is_file() or evidence.stat().st_size > 128_000:
        raise foundation.FoundationError("local input evidence is unavailable or nonregular")
    try:
        saved = json.loads(
            evidence.read_bytes(), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON number"))
        )
    except (ValueError, UnicodeError) as exc:
        raise foundation.FoundationError("unreadable local input evidence") from exc
    current = observation(root, config=config)
    if saved != current:
        raise foundation.FoundationError("local input evidence differs from observed source and files")
    return {
        "candidate_commit": current["candidate_commit"],
        "byte_identity_matches": True,
        "qualification": "BLOCKED",
        "release_authorization": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config", type=Path, help="explicit private SPY-only pilot TOML (default: root lab.toml)")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("capture").add_argument("--out", type=Path, required=True)
    sub.add_parser("verify").add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "capture":
            capture(args.root, args.out, config=args.config)
            payload = {"recorded": True, "qualification": "BLOCKED", "release_authorization": False}
        else:
            payload = verify(args.root, args.input, config=args.config)
        print(json.dumps(payload, sort_keys=True))
    except (OSError, ValueError, TypeError) as exc:
        print(f"F0 local input custody refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
