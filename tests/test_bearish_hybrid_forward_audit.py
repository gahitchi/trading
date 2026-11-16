"""Synthetic-only as-of evidence tests; no exchange traffic or order access."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from lab.bearish_hybrid import HOUR_NS, MINUTE_NS
from lab.bearish_hybrid_forward_audit import audit_forward_evidence, main
from lab.bearish_hybrid_forward_price import record_forward_price
from lab.bearish_hybrid_public_capture import record_public_feature

S = "ETHUSDT"
T = 1_700_006_400_000_000_000


def _clock(received_ns: int):
    return iter((received_ns - 10_000_000, received_ns)).__next__


def _bar(open_ns: int):
    ms = open_ns // 1_000_000
    return [ms, "200", "205", "198", "201", "12", ms + 59_999, "2500", 5, "4", "800", "0"]


def _price(root: Path, end_ns: int, *, delay_ns: int = 5_000_000_000):
    raw = json.dumps([_bar(end_ns - MINUTE_NS), _bar(end_ns)]).encode()
    return record_forward_price(
        root, S, source=lambda _: raw, clock_ns=_clock(end_ns + delay_ns)
    )


def _oi(root: Path, received_ns: int, measured_ns: int, value: str):
    raw = json.dumps(
        {
            "symbol": S,
            "openInterest": value,
            "time": measured_ns // 1_000_000,
        }
    ).encode()
    return record_public_feature(
        root, S, "open-interest", source=lambda _: raw, clock_ns=_clock(received_ns)
    )


def _funding(root: Path, received_ns: int):
    raw = json.dumps(
        [
            {
                "symbol": S,
                "fundingTime": (T - 8 * HOUR_NS) // 1_000_000,
                "fundingRate": "-0.0002",
            }
        ]
    ).encode()
    return record_public_feature(
        root, S, "settled-funding", source=lambda _: raw, clock_ns=_clock(received_ns)
    )


def test_one_price_bar_does_not_backdate_received_features(tmp_path):
    _price(tmp_path, T)
    _oi(tmp_path, T + 25_000_000_000, T + 20_000_000_000, "1000")
    _funding(tmp_path, T + 40_000_000_000)
    audit = audit_forward_evidence(tmp_path, S)
    assert audit.price_receipts == 1
    assert audit.distinct_price_minutes == 1
    assert audit.missing_internal_minutes == 0
    row = audit.rows[0]
    assert row.bar_end_ns == T
    assert row.decision_ns == T + MINUTE_NS
    assert row.oi_receipts_asof == 1
    assert row.funding_receipts_asof == 1
    assert row.funding_events_known == 1
    assert row.oi_change_pct is None
    assert row.qualified is False
    assert row.actual_notional == 0
    assert audit.funding_verified is False
    assert audit.market_eligibility_verified is False
    assert audit.source_independently_authenticated is False


def test_later_oi_receipt_never_leaks_into_first_price_decision(tmp_path):
    _price(tmp_path, T)
    _price(tmp_path, T + 6 * MINUTE_NS)
    _oi(tmp_path, T + 25_000_000_000, T + 20_000_000_000, "1000")
    _oi(tmp_path, T + 6 * MINUTE_NS + 25_000_000_000, T + 6 * MINUTE_NS + 20_000_000_000, "1010")

    audit = audit_forward_evidence(tmp_path, S)
    assert audit.distinct_price_minutes == 2
    assert audit.missing_internal_minutes == 5
    early, late = audit.rows
    assert early.decision_ns == T + MINUTE_NS
    assert early.oi_receipts_asof == 1
    assert early.oi_change_pct is None
    assert late.decision_ns == T + 7 * MINUTE_NS
    assert late.oi_receipts_asof == 2
    assert late.oi_change_pct == pytest.approx(1.0)
    assert late.missing_minutes_before == 5
    assert late.qualified is False
    assert audit.qualified is False


def test_price_same_minute_repoll_not_new_bar(tmp_path):
    _price(tmp_path, T)
    _price(tmp_path, T, delay_ns=15_000_000_000)
    audit = audit_forward_evidence(tmp_path, S)
    assert audit.price_receipts == 2
    assert audit.distinct_price_minutes == 1
    assert audit.duplicate_price_receipts == 1
    assert audit.rows[0].price_received_ns == T + 5_000_000_000


def test_missing_internal_minutes_are_counted_without_backfilling(tmp_path):
    _price(tmp_path, T)
    _price(tmp_path, T + 3 * MINUTE_NS)
    _price(tmp_path, T + 4 * MINUTE_NS)
    result = audit_forward_evidence(tmp_path, S)
    assert result.missing_internal_minutes == 2
    assert [r.missing_minutes_before for r in result.rows] == [0, 2, 0]
    assert result.distinct_price_minutes == 3


def test_funding_first_received_after_decision_is_not_backdated(tmp_path):
    _price(tmp_path, T)
    _price(tmp_path, T + MINUTE_NS)
    _funding(tmp_path, T + MINUTE_NS + 25_000_000_000)
    result = audit_forward_evidence(tmp_path, S)
    assert result.rows[0].funding_events_known == 0
    assert result.rows[0].funding_receipts_asof == 0
    assert result.rows[1].funding_events_known == 1
    assert result.rows[1].funding_receipts_asof == 1


def test_no_price_does_not_infer_missing_historical_coverage(tmp_path):
    _oi(tmp_path, T + 25_000_000_000, T + 20_000_000_000, "1000")
    result = audit_forward_evidence(tmp_path, S)
    assert result.price_receipts == 0
    assert result.rows == ()
    assert result.missing_internal_minutes == 0
    assert result.oi_receipts == 1
    assert result.qualified is False


def test_bad_saved_price_digest_is_refused_before_join(tmp_path):
    _price(tmp_path, T)
    folder = tmp_path / "binance-usdm" / "forward-prices" / S
    path = next(folder.glob("*.json"))
    record = json.loads(path.read_text())
    record["raw_base64"] = base64.b64encode(b"tampered").decode("ascii")
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="modified public price"):
        audit_forward_evidence(tmp_path, S)


def test_invalid_symbol_rejected_without_network(tmp_path):
    with pytest.raises(ValueError, match="non-BTC"):
        audit_forward_evidence(tmp_path, "BTCUSDT")
    with pytest.raises(ValueError, match="non-BTC"):
        audit_forward_evidence(tmp_path, "../ETHUSDT")


def test_offline_cli_uses_local_receipts_only(tmp_path, capsys):
    _price(tmp_path, T)
    assert main(["--root", str(tmp_path), "--symbol", S, "--last", "1"]) == 0
    captured = capsys.readouterr().out
    assert "distinct_minutes=1" in captured
    assert "OI_delta_pct=None" in captured
    assert "qualified=False actual_notional=0" in captured


def test_cli_rejects_unbounded_output(tmp_path):
    with pytest.raises(SystemExit):
        main(["--root", str(tmp_path), "--symbol", S, "--last", "0"])
    with pytest.raises(SystemExit):
        main(["--root", str(tmp_path), "--symbol", S, "--last", "121"])
