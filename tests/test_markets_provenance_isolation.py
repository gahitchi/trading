"""Prove equity research digests ignore crypto-only edits, without a tautological hash fixture."""

from __future__ import annotations

from pathlib import Path

import lab.provenance as provenance


def test_crypto_only_changes_do_not_invalidate_equity_approval_context(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path
    (root / "markets").mkdir()
    (root / "agents").mkdir()
    (root / "provenance.py").write_text("legacy provenance source", encoding="utf-8")
    (root / "agents" / "risk.py").write_text("shared equity calculation v1", encoding="utf-8")
    (root / "markets" / "accounting.py").write_text("crypto linear math v1", encoding="utf-8")
    (root / "markets" / "minute_batch.py").write_text("historical bars v1", encoding="utf-8")
    monkeypatch.setattr(provenance, "Path", lambda _: root / "provenance.py")

    equity_before = provenance.simulation_digest()
    crypto_before = provenance.simulation_digest(market="crypto")
    runtime_before = provenance.runtime_digest()

    (root / "markets" / "accounting.py").write_text("crypto linear math v2", encoding="utf-8")

    assert provenance.simulation_digest() == equity_before
    assert provenance.simulation_digest(market="crypto") != crypto_before
    assert provenance.runtime_digest() != runtime_before

    (root / "agents" / "risk.py").write_text("shared equity calculation v2", encoding="utf-8")
    assert provenance.simulation_digest() != equity_before


def test_operator_only_change_still_does_not_invalidate_trial_digest(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path
    (root / "agents").mkdir()
    (root / "agents" / "execution.py").write_text("execution code", encoding="utf-8")
    (root / "app.py").write_text("operator UI v1", encoding="utf-8")
    monkeypatch.setattr(provenance, "Path", lambda _: root / "provenance.py")

    evidence_before = provenance.simulation_digest()
    runtime_before = provenance.runtime_digest()
    (root / "app.py").write_text("operator UI v2", encoding="utf-8")

    assert provenance.simulation_digest() == evidence_before
    assert provenance.runtime_digest() != runtime_before
