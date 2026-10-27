"""F0 offline-only vulnerability risk debt remains bounded and fail-closed."""

from __future__ import annotations

import json
from datetime import date

import pytest

from tools.foundation_vulnerabilities import VulnerabilityPolicyError, validate


@pytest.fixture
def scenario(tmp_path):
    baseline = tmp_path / "baseline.json"
    report = tmp_path / "scan.json"
    image = tmp_path / "image.json"
    sha = "sha256:" + "a" * 64
    layers = ["sha256:" + "b" * 64, "sha256:" + "c" * 64]
    image.write_text(
        json.dumps({"id": sha, "os": "linux", "architecture": "amd64", "user": "lab", "rootfs_layers": layers})
    )
    accepted = ["CVE-2026-5435", "libc6", "2.41-12+deb13u4", "wont-fix"]
    policy = {
        "schema_version": 1,
        "scope": "F0_OFFLINE_NO_BROKER_OR_RELEASE",
        "expires_on": "2026-11-26",
        "release_authorization": False,
        "broker_authorization": False,
        "accepted_high_matches": [accepted],
    }
    scan = {
        "descriptor": {
            "name": "grype",
            "version": "0.121.0",
            "db": {"status": {"valid": True, "built": "2026-10-27T06:30:41Z"}},
        },
        "distro": {"name": "debian", "version": "13.7"},
        "source": {
            "type": "image",
            "target": {
                "userInput": "trading-lab:ci",
                "imageID": sha,
                "os": "linux",
                "architecture": "amd64",
                "layers": [{"digest": digest} for digest in layers],
            },
        },
        "matches": [
            {
                "vulnerability": {"id": accepted[0], "severity": "High", "fix": {"state": "wont-fix"}},
                "artifact": {"name": accepted[1], "version": accepted[2], "type": "deb"},
            }
        ],
    }
    baseline.write_text(json.dumps(policy))
    report.write_text(json.dumps(scan))
    return report, baseline, image, scan, policy


def test_exact_known_high_is_debt_not_release_approval(scenario):
    report, baseline, image, _, _ = scenario
    result = validate(report, baseline, image=image, today=date(2026, 10, 27))
    assert result["carried_high_matches"] == 1
    assert result["observed_image_id"].startswith("sha256:")
    assert result["qualification"] == "BLOCKED"
    assert result["release_authorization"] is False
    assert result["broker_authorization"] is False


@pytest.mark.parametrize(
    "change",
    ["critical", "unknown", "new_version", "fixed", "foreign_scanner", "stale_db", "foreign_distro"],
)
def test_new_or_dangerous_findings_refused(scenario, change):
    report, baseline, image, obj, _ = scenario
    vuln = obj["matches"][0]["vulnerability"]
    pkg = obj["matches"][0]["artifact"]
    if change == "critical":
        vuln["severity"] = "Critical"
    elif change == "unknown":
        vuln["id"] = "CVE-2099-9999"
    elif change == "new_version":
        pkg["version"] = "new-version"
    elif change == "fixed":
        vuln["fix"]["state"] = "fixed"
    elif change == "foreign_scanner":
        obj["descriptor"]["name"] = "manually-created"
    elif change == "stale_db":
        obj["descriptor"]["db"]["status"]["valid"] = False
    else:
        obj["distro"]["version"] = "12"
    report.write_text(json.dumps(obj))
    with pytest.raises(VulnerabilityPolicyError):
        validate(report, baseline, image=image, today=date(2026, 10, 27))


def test_expiry_and_authorization_forgery_rejected(scenario):
    report, baseline, image, _, policy = scenario
    with pytest.raises(VulnerabilityPolicyError, match="expired"):
        validate(report, baseline, image=image, today=date(2026, 11, 27))
    policy["broker_authorization"] = True
    baseline.write_text(json.dumps(policy))
    with pytest.raises(VulnerabilityPolicyError, match="broadened"):
        validate(report, baseline, image=image, today=date(2026, 10, 27))


def test_report_symlink_refused(scenario, tmp_path):
    report, baseline, image, _, _ = scenario
    alias = tmp_path / "symlink.json"
    alias.symlink_to(report)
    with pytest.raises(VulnerabilityPolicyError, match="symlinked"):
        validate(alias, baseline, image=image, today=date(2026, 10, 27))


@pytest.mark.parametrize(
    "mutation",
    ["wrong_image_id", "wrong_scan_image_id", "wrong_layer", "foreign_tag", "root_user", "wrong_arch"],
)
def test_reject_foreign_image_or_scan_evidence(scenario, mutation):
    report, baseline, image, scan, _ = scenario
    local = json.loads(image.read_text())
    target = scan["source"]["target"]
    if mutation == "wrong_image_id":
        local["id"] = "sha256:" + "d" * 64
    elif mutation == "wrong_scan_image_id":
        target["imageID"] = "sha256:" + "e" * 64
    elif mutation == "wrong_layer":
        target["layers"][0]["digest"] = "sha256:" + "f" * 64
    elif mutation == "foreign_tag":
        target["userInput"] = "untrusted:latest"
    elif mutation == "root_user":
        local["user"] = "root"
    else:
        target["architecture"] = "arm64"
    image.write_text(json.dumps(local))
    report.write_text(json.dumps(scan))
    with pytest.raises(VulnerabilityPolicyError, match="signed image identity"):
        validate(report, baseline, image=image, today=date(2026, 10, 27))


def test_outdated_db_is_not_an_acceptable_clean_scan(scenario):
    report, baseline, image, scan, _ = scenario
    scan["matches"] = []
    scan["descriptor"]["db"]["status"]["built"] = "2026-09-01T00:00:00Z"
    report.write_text(json.dumps(scan))
    with pytest.raises(VulnerabilityPolicyError, match="stale"):
        validate(report, baseline, image=image, today=date(2026, 10, 27))


def test_unknown_severity_is_not_downgraded(scenario):
    report, baseline, image, scan, _ = scenario
    scan["matches"][0]["vulnerability"]["severity"] = "Unknown"
    report.write_text(json.dumps(scan))
    with pytest.raises(VulnerabilityPolicyError, match="severity"):
        validate(report, baseline, image=image, today=date(2026, 10, 27))
