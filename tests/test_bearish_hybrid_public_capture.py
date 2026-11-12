"""All public feature tests use synthetic responses. Never send exchange requests."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from lab.bearish_hybrid import MINUTE_NS
from lab.bearish_hybrid_public_capture import (
    first_seen_funding,
    first_seen_oi,
    load_public_features,
    record_public_feature,
)

S = "ETHUSDT"
T = 1_700_006_400_000_000_000


def clock(start=T, duration_ns=10_000_000):
    times = iter((start, start + duration_ns))
    return lambda: next(times)


def oi(*, symbol=S, time_ms=None, value="1000"):
    return json.dumps({"symbol": symbol, "openInterest": value, "time": (T - MINUTE_NS) // 1_000_000 if time_ms is None else time_ms}).encode()


def funding(*, symbol=S, settled_ms=None, rate="-0.0002"):
    return json.dumps([{"symbol": symbol, "fundingRate": rate, "fundingTime": (T - HOUR_NS) // 1_000_000 if settled_ms is None else settled_ms, "markPrice": "200"}]).encode()


HOUR_NS = 60 * MINUTE_NS


def record(tmp_path, kind="open-interest", raw=None, start=T):
    raw = raw if raw is not None else (oi() if kind == "open-interest" else funding())
    return record_public_feature(tmp_path, S, kind, source=lambda _url: raw, clock_ns=clock(start))


def folder(root: Path, kind="open-interest"):
    return root / "binance-usdm" / "forward-features" / S / kind


def test_oi_receipt_retains_exact_body_and_first_received_clock(tmp_path):
    raw = oi()
    got = record(tmp_path, raw=raw)
    assert got.received_ns == T + 10_000_000
    assert got.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert got.source == "injected-test-source"
    saved = json.loads(next(folder(tmp_path).glob("*.json")).read_text())
    assert base64.b64decode(saved["raw_base64"]) == raw
    assert saved["independently_authenticated"] is False
    assert saved["historical_publication_verified"] is False
    assert saved["funding_history_complete"] is False
    assert saved["url"] == "https://fapi.binance.com/fapi/v1/openInterest?symbol=ETHUSDT"


def test_funding_receipt_keeps_event_time_separate_from_reception(tmp_path):
    event = record(tmp_path, "settled-funding")
    assert event.payload[0]["fundingTime"] * 1_000_000 == T - HOUR_NS
    assert event.received_ns == T + 10_000_000
    saved = json.loads(next(folder(tmp_path, "settled-funding").glob("*.json")).read_text())
    assert saved["url"] == "https://fapi.binance.com/fapi/v1/fundingRate?symbol=ETHUSDT&limit=100"
    assert saved["funding_history_complete"] is False


def test_invalid_symbol_and_kind_rejected_without_calling_network(tmp_path):
    calls = []
    def source(url):
        calls.append(url)
        raise AssertionError("source must never be reached")
    for symbol in ("BTCUSDT", "../ETHUSDT", "ethusdt", "ETHUSD"):
        with pytest.raises(ValueError, match="non-BTC"):
            record_public_feature(tmp_path, symbol, "open-interest", source=source)
    with pytest.raises(ValueError, match="unsupported"):
        record_public_feature(tmp_path, S, "order", source=source)
    assert calls == []


def test_oi_wrong_symbol_future_measurement_and_zero_rejected(tmp_path):
    for raw in (oi(symbol="SOLUSDT"), oi(time_ms=(T + MINUTE_NS) // 1_000_000), oi(value="0"), oi(value="NaN"), oi(value="inf")):
        with pytest.raises(ValueError):
            record(tmp_path, raw=raw)
    assert not list(tmp_path.rglob("*.json"))


def test_funding_wrong_symbol_future_settlement_invalid_rate_rejected(tmp_path):
    for raw in (funding(symbol="SOLUSDT"), funding(settled_ms=(T + MINUTE_NS) // 1_000_000), funding(rate="NaN"), funding(rate="3")):
        with pytest.raises(ValueError):
            record(tmp_path, "settled-funding", raw=raw)
    assert not list(tmp_path.rglob("*.json"))


def test_funding_unordered_duplicate_entries_rejected(tmp_path):
    entry = {"symbol": S, "fundingRate": "0.0001", "fundingTime": (T - HOUR_NS) // 1_000_000}
    with pytest.raises(ValueError, match="unordered"):
        record(tmp_path, "settled-funding", raw=json.dumps([entry, entry]).encode())


def test_invalid_json_and_duplicate_keys_rejected(tmp_path):
    for raw in (b"{", b' {"symbol":"ETHUSDT","symbol":"ETHUSDT"}', b'{"v":NaN}', b'\xff'):
        with pytest.raises(ValueError):
            record(tmp_path, raw=raw)


def test_oversized_response_and_unreliable_clock_rejected(tmp_path):
    with pytest.raises(ValueError, match="oversized"):
        record(tmp_path, raw=b"x" * 70_000)
    with pytest.raises(ValueError, match="clock"):
        record(tmp_path, start=0)
    with pytest.raises(ValueError, match="clock"):
        record_public_feature(tmp_path, S, "open-interest", source=lambda _: oi(), clock_ns=clock(T, -1))
    with pytest.raises(ValueError, match="clock"):
        record_public_feature(tmp_path, S, "open-interest", source=lambda _: oi(), clock_ns=clock(T, 61_000_000_000))


def test_offline_reload_verifies_exact_payload_and_source(tmp_path):
    first = record(tmp_path)
    got = load_public_features(tmp_path, S, "open-interest")
    assert got == (first,)
    assert load_public_features(tmp_path, S, "settled-funding") == ()


def test_offline_rejects_receipt_payload_mutation(tmp_path):
    record(tmp_path)
    path = next(folder(tmp_path).glob("*.json"))
    receipt = json.loads(path.read_text())
    receipt["raw_base64"] = base64.b64encode(oi(value="9999")).decode()
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="modified"):
        load_public_features(tmp_path, S, "open-interest")


def test_offline_rejects_forged_attestation_and_path_rename(tmp_path):
    record(tmp_path)
    path = next(folder(tmp_path).glob("*.json"))
    receipt = json.loads(path.read_text())
    receipt["historical_publication_verified"] = True
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="untrusted"):
        load_public_features(tmp_path, S, "open-interest")
    receipt["historical_publication_verified"] = False
    path.write_text(json.dumps(receipt))
    path.rename(path.with_name("12345.json"))
    with pytest.raises(ValueError, match="untrusted"):
        load_public_features(tmp_path, S, "open-interest")


def test_duplicate_capture_timestamp_cannot_overwrite(tmp_path):
    original = record(tmp_path)
    with pytest.raises(ValueError, match="duplicate immutable"):
        record(tmp_path, raw=oi(value="2500"))
    assert load_public_features(tmp_path, S, "open-interest") == (original,)


def test_out_of_order_funding_capture_files_are_sorted_and_reloadable(tmp_path):
    record(tmp_path, "settled-funding", start=T)
    record(tmp_path, "settled-funding", start=T + 5 * MINUTE_NS)
    got = load_public_features(tmp_path, S, "settled-funding")
    assert len(got) == 2 and got[0].received_ns < got[1].received_ns


def test_oi_is_unavailable_on_or_before_receive_time(tmp_path):
    early = record(tmp_path, start=T)
    late = record(tmp_path, raw=oi(value="1010"), start=T + 6 * MINUTE_NS)
    assert first_seen_oi([early, late], decision_ns=T + 6 * MINUTE_NS) is None
    assert first_seen_oi([early, late], decision_ns=T + 7 * MINUTE_NS) is not None


def test_oi_window_uses_received_time_not_source_measurement_time(tmp_path):
    early = record(tmp_path, start=T)
    late = record(tmp_path, raw=oi(value="1010", time_ms=(T - MINUTE_NS) // 1_000_000), start=T + 6 * MINUTE_NS)
    result = first_seen_oi([early, late], decision_ns=T + 7 * MINUTE_NS)
    assert result.previous_ns == early.received_ns
    assert result.observed_ns == late.received_ns
    assert result.change_pct(T + 7 * MINUTE_NS, 60) == pytest.approx(1.0)


def test_oi_stale_or_insufficient_history_return_none(tmp_path):
    first = record(tmp_path, start=T)
    near = record(tmp_path, raw=oi(value="1010"), start=T + 2 * MINUTE_NS)
    assert first_seen_oi([first, near], decision_ns=T + 3 * MINUTE_NS) is None
    assert first_seen_oi([first, near], decision_ns=T + 70 * MINUTE_NS) is None
    with pytest.raises(ValueError, match="nonchronological"):
        first_seen_oi([near, first], decision_ns=T + 3 * MINUTE_NS)


def test_late_funding_backfill_cannot_rewrite_earlier_decision(tmp_path):
    capture = record(tmp_path, "settled-funding", start=T)
    assert first_seen_funding((capture,), decision_ns=T) == ()
    later = first_seen_funding((capture,), decision_ns=T + MINUTE_NS)
    assert len(later) == 1
    assert later[0].settlement_ns == T - HOUR_NS
    assert later[0].published_ns == capture.received_ns


def test_funding_rates_are_deduplicated_but_not_backdated(tmp_path):
    a = record(tmp_path, "settled-funding", start=T)
    b = record(tmp_path, "settled-funding", start=T + MINUTE_NS)
    r = first_seen_funding([a, b], decision_ns=T + 2 * MINUTE_NS)
    assert len(r) == 1 and r[0].published_ns == a.received_ns


def test_conflicting_funding_correction_requires_audit(tmp_path):
    a = record(tmp_path, "settled-funding", start=T)
    b = record(tmp_path, "settled-funding", raw=funding(rate="0.0003"), start=T + MINUTE_NS)
    with pytest.raises(ValueError, match="conflicting revisions"):
        first_seen_funding([a, b], decision_ns=T + 2 * MINUTE_NS)


def test_funding_history_never_automatically_enables_funding_verified(tmp_path):
    capture = record(tmp_path, "settled-funding", start=T)
    data = first_seen_funding([capture], decision_ns=T + MINUTE_NS)
    assert data[0].published_ns >= data[0].settlement_ns
    saved = json.loads(next(folder(tmp_path, "settled-funding").glob("*.json")).read_text())
    assert saved["funding_history_complete"] is False
    assert saved["historical_publication_verified"] is False


def test_offline_load_rejects_symlinks_and_unknown_files(tmp_path):
    record(tmp_path)
    (folder(tmp_path) / "junk.txt").write_text("junk")
    with pytest.raises(ValueError, match="unexpected"):
        load_public_features(tmp_path, S, "open-interest")
    (folder(tmp_path) / "junk.txt").unlink()
    receipt = next(folder(tmp_path).glob("*.json"))
    receipt.unlink()
    receipt.symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="unexpected"):
        load_public_features(tmp_path, S, "open-interest")


def test_full_empty_funding_response_captures_without_claim_of_completeness(tmp_path):
    f = record(tmp_path, "settled-funding", raw=b"[]")
    assert first_seen_funding([f], decision_ns=T + MINUTE_NS) == ()
    saved = json.loads(next(folder(tmp_path, "settled-funding").glob("*.json")).read_text())
    assert saved["funding_history_complete"] is False


def test_reject_non_utc_minute_decision_and_wrong_capture_kind(tmp_path):
    o = record(tmp_path)
    f = record(tmp_path, "settled-funding")
    with pytest.raises(ValueError, match="completed UTC minute"):
        first_seen_oi([o], decision_ns=T + 1)
    with pytest.raises(ValueError, match="completed UTC minute"):
        first_seen_funding([f], decision_ns=T + 1)
    with pytest.raises(ValueError, match="open-interest"):
        first_seen_oi([f], decision_ns=T + MINUTE_NS)
    with pytest.raises(ValueError, match="settled-funding"):
        first_seen_funding([o], decision_ns=T + MINUTE_NS)
