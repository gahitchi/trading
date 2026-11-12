"""Synthetic quality controls: no public network, actual wallets or historical edge."""

from __future__ import annotations

from dataclasses import replace

import pytest

from lab.bearish_hybrid import MINUTE_NS, Bar
from lab.bearish_hybrid_forward_gate import inspect_public_features
from lab.bearish_hybrid_public_capture import FeatureCapture, first_seen_funding, first_seen_oi

T = 1_700_006_400_000_000_000
SYMBOL = "ETHUSDT"


def oi(receive_ns, measured_ns, value="1000", symbol=SYMBOL):
    return FeatureCapture(
        symbol,
        "open-interest",
        receive_ns - 10_000_000,
        receive_ns,
        "0" * 64,
        "injected-test-source",
        {"symbol": symbol, "time": measured_ns // 1_000_000, "openInterest": value},
    )


def funding(receive_ns, symbol=SYMBOL):
    return FeatureCapture(
        symbol,
        "settled-funding",
        receive_ns - 10_000_000,
        receive_ns,
        "0" * 64,
        "injected-test-source",
        [{"symbol": symbol, "fundingTime": (T - MINUTE_NS) // 1_000_000, "fundingRate": "0.0001"}],
    )


def bar():
    return Bar(T + 7 * MINUTE_NS, 100.0, 101.0, 99.0, 100.0, 1000.0, 450.0)


def test_distinct_measurements_use_first_receipt_times():
    early = oi(T + 10_000_000, T)
    late = oi(T + 6 * MINUTE_NS + 10_000_000, T + 6 * MINUTE_NS, "1010")
    result = first_seen_oi((early, late), decision_ns=T + 7 * MINUTE_NS)
    assert result is not None
    assert result.previous_ns == early.received_ns
    assert result.observed_ns == late.received_ns
    assert result.change_pct(T + 7 * MINUTE_NS, 15) == pytest.approx(1.0)
    assert first_seen_oi((early, late), decision_ns=T + 6 * MINUTE_NS) is None


def test_duplicate_same_sample_never_creates_second_oi_observation():
    early = oi(T + 10_000_000, T)
    late = oi(T + 6 * MINUTE_NS + 10_000_000, T)
    assert first_seen_oi((early, late), decision_ns=T + 7 * MINUTE_NS) is None


def test_conflicting_same_measurement_fails_closed():
    early = oi(T + 10_000_000, T)
    late = oi(T + 6 * MINUTE_NS + 10_000_000, T, "1010")
    with pytest.raises(ValueError, match="conflicting revisions"):
        first_seen_oi((early, late), decision_ns=T + 7 * MINUTE_NS)
    # The later correction must not reach earlier decisions.
    assert first_seen_oi((early, late), decision_ns=T + MINUTE_NS) is None


def test_stale_measurement_excluded_from_fresh_pairs():
    early = oi(T + 10_000_000, T)
    late = oi(T + 8 * MINUTE_NS + 10_000_000, T + 4 * MINUTE_NS + MINUTE_NS // 2, "1010")
    assert first_seen_oi((early, late), decision_ns=T + 9 * MINUTE_NS) is None


def test_sample_gap_must_be_five_minutes_independent_of_receipt_gap():
    early = oi(T + 10_000_000, T)
    late = oi(T + 6 * MINUTE_NS + 10_000_000, T + 4 * MINUTE_NS + MINUTE_NS // 2, "1010")
    assert first_seen_oi((early, late), decision_ns=T + 7 * MINUTE_NS) is None


def test_backdated_measurement_detected_when_received():
    early = oi(T + 10_000_000, T)
    late = oi(T + 6 * MINUTE_NS + 10_000_000, T - MINUTE_NS, "1010")
    with pytest.raises(ValueError, match="out-of-order"):
        first_seen_oi((early, late), decision_ns=T + 7 * MINUTE_NS)


def test_cross_symbol_oi_and_funding_rejected():
    first = oi(T + 10_000_000, T)
    later = oi(T + 6 * MINUTE_NS + 10_000_000, T + 6 * MINUTE_NS, "1010", symbol="SOLUSDT")
    with pytest.raises(ValueError, match="mixed-symbol"):
        first_seen_oi((first, later), decision_ns=T + 7 * MINUTE_NS)
    f1 = funding(T + 10_000_000)
    f2 = funding(T + MINUTE_NS + 10_000_000, symbol="SOLUSDT")
    with pytest.raises(ValueError, match="mixed-symbol"):
        first_seen_funding((f1, f2), decision_ns=T + 7 * MINUTE_NS)


def test_audit_never_promotes_incomplete_source_or_funding():
    early = oi(T + 10_000_000, T)
    late = oi(T + 6 * MINUTE_NS + 10_000_000, T + 6 * MINUTE_NS, "1010")
    settled = funding(T + 10_000_000)
    report = inspect_public_features(
        bar(), SYMBOL, oi_captures=(early, late), funding_captures=(settled,)
    )
    assert report.oi_receipts == 2 and report.funding_receipts == 1
    assert report.observation.oi is not None
    assert len(report.observation.funding) == 1
    assert report.observation.market is None
    assert report.observation.source_complete is False
    assert report.observation.funding_verified is False
    assert report.qualified is False and report.actual_notional == 0


def test_audit_never_uses_future_oi_or_funding():
    future = oi(T + 8 * MINUTE_NS + 10_000_000, T + 8 * MINUTE_NS)
    future_funding = funding(T + 8 * MINUTE_NS + 10_000_000)
    report = inspect_public_features(
        bar(), SYMBOL, oi_captures=(future,), funding_captures=(future_funding,)
    )
    assert report.observation.oi is None
    assert report.observation.funding == ()
    assert report.oi_receipts == report.funding_receipts == 0


def test_audit_rejects_wrong_symbol_and_unaligned_bar():
    early = oi(T + 10_000_000, T)
    with pytest.raises(ValueError, match="mixed-symbol"):
        inspect_public_features(bar(), "SOLUSDT", oi_captures=(early,), funding_captures=())
    with pytest.raises(ValueError, match="minute-end"):
        inspect_public_features(replace(bar(), end_ns=bar().end_ns + 1), SYMBOL, oi_captures=(), funding_captures=())


def test_audit_rejects_btc_instrument_even_with_no_captures():
    with pytest.raises(ValueError, match="non-BTC"):
        inspect_public_features(bar(), "BTCUSDT", oi_captures=(), funding_captures=())
