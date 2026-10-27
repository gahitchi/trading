"""Expiry-bound offline-only known-CVE exceptions, never release authorization."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

MAX_REPORT_BYTES = 50_000_000


class VulnerabilityPolicyError(ValueError):
    """A scan or a bounded research exception is not acceptable."""


def _read(path: Path, bound: int) -> dict:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= bound:
        raise VulnerabilityPolicyError("missing, symlinked, or oversized evidence")
    try:
        doc = json.loads(
            path.read_bytes(),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON number")),
        )
    except (ValueError, UnicodeError) as exc:
        raise VulnerabilityPolicyError("invalid scanner JSON") from exc
    if type(doc) is not dict:
        raise VulnerabilityPolicyError("scanner evidence must be an object")
    return doc


def validate(report: Path, baseline: Path, *, image: Path, today: date | None = None) -> dict:
    policy = _read(baseline, 65_536)
    if (
        policy.get("schema_version") != 1
        or policy.get("scope") != "F0_OFFLINE_NO_BROKER_OR_RELEASE"
        or policy.get("release_authorization") is not False
        or policy.get("broker_authorization") is not False
        or type(policy.get("accepted_high_matches")) is not list
    ):
        raise VulnerabilityPolicyError("invalid or broadened F0 exception")
    try:
        expiry = date.fromisoformat(policy["expires_on"])
    except (TypeError, KeyError, ValueError) as exc:
        raise VulnerabilityPolicyError("invalid F0 expiry") from exc
    if (today or date.today()) > expiry:
        raise VulnerabilityPolicyError("F0 exception expired; new review required")
    rows = policy["accepted_high_matches"]
    if (
        len(rows) > 256
        or any(type(r) is not list or len(r) != 4 for r in rows)
        or any(any(type(s) is not str or not s for s in r) for r in rows)
    ):
        raise VulnerabilityPolicyError("invalid accepted-vulnerability inventory")
    allowed = {tuple(r) for r in rows}
    if len(allowed) != len(rows) or any(
        not r[0].startswith("CVE-") or r[3] not in ("wont-fix", "not-fixed") for r in allowed
    ):
        raise VulnerabilityPolicyError("duplicate or improper exception")

    scan = _read(report, MAX_REPORT_BYTES)
    descriptor = scan.get("descriptor")
    distro = scan.get("distro")
    source = scan.get("source")
    if (
        type(descriptor) is not dict
        or type(distro) is not dict
        or type(source) is not dict
        or type(scan.get("matches")) is not list
        or descriptor.get("name") != "grype"
        or descriptor.get("version") != "0.121.0"
        or type(descriptor.get("db")) is not dict
        or type(descriptor["db"].get("status")) is not dict
        or descriptor["db"]["status"].get("valid") is not True
        or distro.get("name") != "debian"
        or not str(distro.get("version", "")).startswith("13.")
        or source.get("type") != "image"
    ):
        raise VulnerabilityPolicyError("wrong scanner, database, image, or operating system")

    # Bind scanner output to the *same built image* as the signed F0
    # container identity receipt. A matching CVE list for another image
    # must never stand in for the actual runtime scan.
    observed_image = _read(image, 65_536)
    target = source.get("target")
    layers = observed_image.get("rootfs_layers")
    if (
        type(target) is not dict
        or type(layers) is not list
        or not 1 <= len(layers) <= 256
        or any(type(value) is not str or not value.startswith("sha256:") for value in layers)
        or observed_image.get("os") != "linux"
        or observed_image.get("architecture") not in ("amd64", "arm64")
        or observed_image.get("user") != "lab"
        or type(observed_image.get("id")) is not str
        or not observed_image["id"].startswith("sha256:")
        or target.get("imageID") != observed_image["id"]
        or target.get("userInput") != "trading-lab:ci"
        or target.get("os") != observed_image["os"]
        or target.get("architecture") != observed_image["architecture"]
        or type(target.get("layers")) is not list
        or [item.get("digest") for item in target["layers"] if type(item) is dict] != layers
        or len(target["layers"]) != len(layers)
    ):
        raise VulnerabilityPolicyError("scanner target does not match signed image identity")
    # Reject a stale or future-dated vulnerability database. A zero-findings
    # report alone is not evidence of a current scan.
    built = descriptor["db"]["status"].get("built")
    try:
        built_on = date.fromisoformat(built[:10]) if type(built) is str else None
    except ValueError as exc:
        raise VulnerabilityPolicyError("vulnerability database build date is invalid") from exc
    checked_on = today or date.today()
    if built_on is None or not checked_on - timedelta(days=14) <= built_on <= checked_on + timedelta(days=1):
        raise VulnerabilityPolicyError("vulnerability database is stale or future-dated")

    counts: dict[str, int] = {}
    highs: set[tuple[str, str, str, str]] = set()
    for match in scan["matches"]:
        if (
            type(match) is not dict
            or type(match.get("vulnerability")) is not dict
            or type(match.get("artifact")) is not dict
        ):
            raise VulnerabilityPolicyError("malformed security match")
        vuln, pkg = match["vulnerability"], match["artifact"]
        severity = vuln.get("severity")
        if severity not in ("Negligible", "Low", "Medium", "High", "Critical"):
            raise VulnerabilityPolicyError("unrecognized severity")
        counts[severity] = counts.get(severity, 0) + 1
        if severity == "Critical":
            raise VulnerabilityPolicyError("Critical vulnerabilities always block F0")
        if severity == "High":
            fix = vuln.get("fix")
            if type(fix) is not dict:
                raise VulnerabilityPolicyError("High finding missing fix state")
            item = (vuln.get("id"), pkg.get("name"), pkg.get("version"), fix.get("state"))
            if item not in allowed or pkg.get("type") != "deb":
                raise VulnerabilityPolicyError("new, changed, or fixable High vulnerability")
            if item in highs:
                raise VulnerabilityPolicyError("duplicate High vulnerability observation")
            highs.add(item)
    return {
        "scope": policy["scope"],
        "scanner_report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        "observed_image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "observed_image_id": observed_image["id"],
        "carried_high_matches": len(highs),
        "critical_matches": 0,
        "unknown_high_matches": 0,
        "counts": counts,
        "exception_expires_on": expiry.isoformat(),
        "qualification": "BLOCKED",
        "release_authorization": False,
        "broker_authorization": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "docs/security/f0-offline-cve-baseline.json",
    )
    args = parser.parse_args(argv)
    try:
        result = validate(args.report, args.baseline, image=args.image)
    except (OSError, VulnerabilityPolicyError) as exc:
        print(f"F0 security exception rejected: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
