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
        "descriptor": {"name": "grype", "version": "0.121.0", "db": {"status": {"valid": True}}},
        "distro": {"name": "debian", "version": "13.7"},
        "source": {"type": "image"},
        "matches": [
            {
                "vulnerability": {"id": accepted[0], "severity": "High", "fix": {"state": "wont-fix"}},
                "artifact": {"name": accepted[1], "version": accepted[2], "type": "deb"},
            }
        ],
    }
    baseline.write_text(json.dumps(policy))
    report.write_text(json.dumps(scan))
    return report, baseline, scan, policy


def test_exact_known_high_is_debt_not_release_approval(scenario):
    report, baseline, _, _ = scenario
    result = validate(report, baseline, today=date(2026, 10, 27))
    assert result["carried_high_matches"] == 1
    assert result["qualification"] == "BLOCKED"
    assert result["release_authorization"] is False
    assert result["broker_authorization"] is False


@pytest.mark.parametrize(
    "change",
    ["critical", "unknown", "new_version", "fixed", "foreign_scanner", "stale_db", "foreign_distro"],
)
def test_new_or_dangerous_findings_refused(scenario, change):
    report, baseline, obj, _ = scenario
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
        validate(report, baseline, today=date(2026, 10, 27))


def test_expiry_and_authorization_forgery_rejected(scenario):
    report, baseline, _, policy = scenario
    with pytest.raises(VulnerabilityPolicyError, match="expired"):
        validate(report, baseline, today=date(2026, 11, 27))
    policy["broker_authorization"] = True
    baseline.write_text(json.dumps(policy))
    with pytest.raises(VulnerabilityPolicyError, match="broadened"):
        validate(report, baseline, today=date(2026, 10, 27))


def test_report_symlink_refused(scenario, tmp_path):
    report, baseline, _, _ = scenario
    alias = tmp_path / "symlink.json"
    alias.symlink_to(report)
    with pytest.raises(VulnerabilityPolicyError, match="symlinked"):
        validate(alias, baseline, today=date(2026, 10, 27))
