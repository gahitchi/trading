"""Synthetic-only prospective trial registration tests; no remote archives or trading."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from lab.bearish_hybrid import MINUTE_NS, Costs, VirtualAccount
from lab.bearish_hybrid_dynamic import ProfitPolicy
from lab.bearish_hybrid_replay import DAY_NS
from lab.bearish_hybrid_study import StudySpec, planned_study_member, run_registered_study
from lab.research_family import register_family
from lab.trials import history

START = 1_700_006_400_000_000_000
ACCOUNT = VirtualAccount(10_000, 10_000, 0, 0, 5_000, 0, "run", True)
COSTS = Costs(4, 3, 2, 1)
POLICY = ProfitPolicy(0.5)


def archive(*, symbol="ETHUSDT", days=1, invalid=False):
    # Complete synthetic minute bars only; no OI, funding or genuine exchange evidence.
    bars = [
        {
            "end_ns": START + i * MINUTE_NS,
            "open": 100.0,
            "high": 100.1,
            "low": 99.9,
            "close": 100.0,
            "quote_volume": 100.0,
            "taker_buy_quote": 50.0,
            "source_complete": not invalid,
        }
        for i in range(1, days * 1440 + 1)
    ]
    return json.dumps({"version": 1, "venue": "binance-usdm", "symbol": symbol, "bars": bars,
                       "oi": [], "funding": [], "market": []}, separators=(",", ":")).encode()


def spec_of(raw, *, symbol="ETHUSDT", end=START + DAY_NS):
    return StudySpec(
        symbol=symbol,
        start_ns=START,
        end_ns=end,
        boundary_ns=START + 3 * DAY_NS,
        archive_sha256=hashlib.sha256(raw).hexdigest(),
    )


def member_of(spec, *, policy=POLICY):
    return planned_study_member(spec, costs=COSTS, account=ACCOUNT, policy=policy)


def register(root, spec, *, policy=POLICY):
    member = member_of(spec, policy=policy)
    return register_family(root, {"name": "prospective synthetic hybrid trial", "rationale": "freeze parameters before opening any outcomes", "members": [member]})


def evaluate(root, file, spec, *, policy=POLICY):
    return run_registered_study(root, file, spec, costs=COSTS, account=ACCOUNT, policy=policy)


def test_prospective_member_freezes_entire_setting_and_actual_capital():
    s = spec_of(archive())
    m = member_of(s)
    assert m["kind"] == "bearish-hybrid-dynamic-study"
    assert m["window"] == [s.start_ns, s.end_ns]
    assert m["params"]["profit_policy"] == {"reinvest_positive_fraction": 0.5}
    assert m["params"]["actual_capital"] == 0.0
    assert member_of(s, policy=ProfitPolicy(0.25)) != m


def test_missing_registration_fails_before_archive_is_opened(tmp_path):
    with pytest.raises(ValueError, match="frozen"):
        evaluate(tmp_path, tmp_path / "never-created.json", spec_of(archive()))
    assert not list(tmp_path.glob("research_attempts/*.json"))


def test_unplanned_reinvestment_fraction_rejected_without_attempt(tmp_path):
    s = spec_of(archive())
    register(tmp_path, s)
    with pytest.raises(ValueError, match="frozen"):
        evaluate(tmp_path, tmp_path / "missing.json", s, policy=ProfitPolicy(0.9))
    assert not history(tmp_path)


def test_registered_synthetic_flat_days_are_recorded_but_not_qualified(tmp_path):
    raw = archive()
    s = spec_of(raw)
    register(tmp_path, s)
    path = tmp_path / "archive.json"
    path.write_bytes(raw)
    result = evaluate(tmp_path, path, s)
    assert len(result.days) == result.flat_days == 1
    assert not result.qualified and result.actual_notional == 0.0
    assert result.completed_trades == 0 and result.ending_total_virtual_equity == ACCOUNT.equity
    rows = history(tmp_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "completed" and rows[0]["result"]["qualified"] is False
    assert rows[0]["result"]["archive_origin_authenticated"] is False
    assert rows[0]["inputs"]["streams"][0]["name"] == "hybrid-source-archive"
    assert rows[0]["inputs"]["streams"][0]["records"] == 1
    assert rows[0]["family_id"]


def test_reserved_attempt_exists_before_archive_validation(monkeypatch, tmp_path):
    import lab.bearish_hybrid_study as study

    raw = archive()
    s = spec_of(raw)
    register(tmp_path, s)
    p = tmp_path / "archive.json"
    p.write_bytes(raw)
    actual = study.load_hybrid_archive
    checked = []

    def check_reservation(*args, **kwargs):
        h = history(tmp_path)
        assert len(h) == 1 and h[0]["status"] == "reserved"
        checked.append(True)
        return actual(*args, **kwargs)

    monkeypatch.setattr(study, "load_hybrid_archive", check_reservation)
    evaluate(tmp_path, p, s)
    assert checked


def test_corrupted_archive_is_counted_as_failed_attempt(tmp_path):
    raw = archive()
    s = spec_of(raw)
    register(tmp_path, s)
    p = tmp_path / "archive.json"
    p.write_bytes(raw + b"corrupt")
    with pytest.raises(ValueError, match="SHA-256"):
        evaluate(tmp_path, p, s)
    rows = history(tmp_path)
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert rows[0]["result"]["status"] == "unavailable"
    assert rows[0]["inputs"]["streams"][0]["complete"] is True


def test_missing_source_file_is_failed_not_skipped(tmp_path):
    raw = archive()
    s = spec_of(raw)
    register(tmp_path, s)
    with pytest.raises(FileNotFoundError):
        evaluate(tmp_path, tmp_path / "absent.json", s)
    assert len(history(tmp_path)) == 1 and history(tmp_path)[0]["status"] == "failed"


def test_symbol_mismatch_is_counted_and_rejected(tmp_path):
    raw = archive(symbol="SOLUSDT")
    s = spec_of(raw, symbol="ETHUSDT")
    register(tmp_path, s)
    p = tmp_path / "wrong-symbol.json"
    p.write_bytes(raw)
    with pytest.raises(ValueError, match="symbol"):
        evaluate(tmp_path, p, s)
    assert history(tmp_path)[0]["status"] == "failed"


def test_missing_full_utc_window_fails_and_is_counted(tmp_path):
    raw = archive(days=1)
    s = spec_of(raw, end=START + 2 * DAY_NS)
    register(tmp_path, s)
    p = tmp_path / "short.json"
    p.write_bytes(raw)
    with pytest.raises(ValueError, match="exactly"):
        evaluate(tmp_path, p, s)
    assert history(tmp_path)[0]["status"] == "failed"


def test_btc_study_and_future_holdout_cannot_be_repackaged_as_new_research():
    raw = archive()
    with pytest.raises(ValueError, match="protected"):
        spec_of(raw, symbol="BTCUSDT")
    with pytest.raises(ValueError, match="pre-holdout"):
        replace(spec_of(raw), boundary_ns=START + DAY_NS)
    with pytest.raises(ValueError, match="whole-day"):
        replace(spec_of(raw), end_ns=START + DAY_NS + MINUTE_NS)


def test_funding_verification_cannot_be_claimed_without_pinned_external_evidence():
    s = spec_of(archive())
    with pytest.raises(ValueError, match="evidence"):
        replace(s, funding_verified=True)
    assert replace(s, funding_verified=True, external_attestation_sha256="f" * 64).funding_verified
    with pytest.raises(ValueError, match="SHA-256"):
        replace(s, external_attestation_sha256="not-a-digest")


def test_input_digest_wrong_case_and_invalid_account_or_policy_rejected():
    s = spec_of(archive())
    with pytest.raises(ValueError, match="SHA-256"):
        replace(s, archive_sha256=s.archive_sha256.upper())
    with pytest.raises(ValueError, match="virtual capital"):
        planned_study_member(s, costs=COSTS, account=VirtualAccount(), policy=POLICY)
    with pytest.raises(ValueError, match="profit policy"):
        planned_study_member(s, costs=COSTS, account=ACCOUNT, policy=None)
