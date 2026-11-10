"""Pure synthetic Binance USD-M tier and mark-price risk diagnostic tests."""

from dataclasses import replace
from decimal import Decimal

import pytest

from lab.binance_usdm_margin_rules import (
    IsolatedShort,
    MarkEvidence,
    assess_isolated_short,
    parse_leverage_brackets,
    source_sha256,
)

START = 1_767_225_600_000  # synthetic UTC-minute-aligned test instant


def brackets():
    return {
        "symbol": "BTCUSDT",
        "brackets": [
            {
                "bracket": 1,
                "initialLeverage": 20,
                "notionalCap": "1000",
                "notionalFloor": "0",
                "maintMarginRatio": "0.005",
                "cum": "0",
            },
            {
                "bracket": 2,
                "initialLeverage": 10,
                "notionalCap": "5000",
                "notionalFloor": "1000",
                "maintMarginRatio": "0.01",
                "cum": "5",
            },
            {
                "bracket": 3,
                "initialLeverage": 5,
                "notionalCap": "20000",
                "notionalFloor": "5000",
                "maintMarginRatio": "0.025",
                "cum": "80",
            },
        ],
    }


def bars(*, closes=(600, 600, 600), highs=(605, 610, 612)):
    rows = []
    for i, (close, high) in enumerate(zip(closes, highs, strict=True)):
        t = START + i * 60_000
        rows.append([t, "600", str(high), "590", str(close), "0", t + 59_999, "0", 60, "0", "0", "0"])
    return rows


def position():
    return IsolatedShort(
        symbol="BTCUSDT",
        quantity=Decimal("2"),
        entry_fill_price=Decimal("600"),
        isolated_wallet_after_entry=Decimal("300"),
        leverage=10,
        funding_debit_reserve=Decimal("2"),
        close_fee_bps=Decimal("5"),
        buffer_bps=Decimal("5"),
    )


def assess(
    *,
    raw=None,
    position_=None,
    klines=None,
    mark_at=None,
    capture_at=None,
    expected_rules=None,
    expected_marks=None,
):
    raw = brackets() if raw is None else raw
    klines = bars() if klines is None else klines
    return assess_isolated_short(
        raw,
        position_ or position(),
        MarkEvidence(
            klines,
            source_sha256({"endpoint": "/fapi/v1/markPriceKlines", "symbol": "BTCUSDT", "candles": klines}),
            START,
            capture_at if capture_at is not None else START + 3 * 60_000,
            mark_at if mark_at is not None else START + 3 * 60_000,
            symbol="BTCUSDT",
            endpoint="/fapi/v1/markPriceKlines",
        ),
        expected_bracket_sha256=expected_rules or source_sha256(raw),
        expected_mark_sha256=expected_marks
        or source_sha256({"endpoint": "/fapi/v1/markPriceKlines", "symbol": "BTCUSDT", "candles": klines}),
    )


def test_tiered_maintenance_uses_notional_at_mark_not_entry():
    result = assess()
    assert result.status == "NO_MARK_ENVELOPE_BREACH_OBSERVED_UNQUALIFIED"
    assert result.current_maintenance == Decimal("7")
    assert result.worst_maintenance == Decimal("7.24")
    assert result.worst_excess < result.current_excess
    assert result.execution_authorized is False
    assert result.authenticated_exchange_state is False
    assert result.qualified is False


def test_mark_price_spike_triggers_a_possible_breach_even_when_last_mark_recovers():
    result = assess(klines=bars(highs=(605, 800, 610)))
    assert result.mark_price == Decimal("600")
    assert result.worst_observed_mark == Decimal("800")
    assert result.current_excess > 0
    assert result.worst_excess < 0
    assert result.status == "MARK_ENVELOPE_BREACH_POSSIBLE"
    assert Decimal("600") < result.modeled_liquidation_mark < Decimal("800")


def test_tier_boundary_maintenance_is_continuous():
    tiers = parse_leverage_brackets(brackets(), symbol="BTCUSDT", expected_sha256=source_sha256(brackets()))
    assert tiers.tier(Decimal("999")).bracket == 1
    assert tiers.tier(Decimal("1000")).bracket == 2
    assert tiers.tier(Decimal("5000")).bracket == 3
    at_1000 = tiers.tier(Decimal("1000"))
    assert at_1000.maintenance_rate * 1000 - at_1000.cum == Decimal("5")
    with pytest.raises(ValueError, match="exceeds"):
        tiers.tier(Decimal("20000"))


def test_liquidation_threshold_uses_cum_and_rechecks_the_solved_tier():
    result = assess()
    predicted = (Decimal("300") - Decimal("2") + 2 * 600 + Decimal("5")) / (2 * Decimal("1.011"))
    assert abs(result.modeled_liquidation_mark - predicted) < Decimal("1e-24")
    assert 1000 <= 2 * predicted < 5000


def test_missing_or_changed_bracket_source_rejected():
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        assess(expected_rules="f" * 64)
    bad = brackets()
    bad["brackets"][1]["maintMarginRatio"] = "0.011"
    with pytest.raises(ValueError, match="cum"):
        assess(raw=bad)


def test_account_specific_notional_coefficient_not_silently_ignored():
    data = brackets()
    data["notionalCoef"] = "2.0"
    with pytest.raises(ValueError, match="coefficient"):
        assess(raw=data)


def test_symbol_mismatch_and_multiple_symbol_response_rejected():
    wrong = brackets()
    wrong["symbol"] = "ETHUSDT"
    with pytest.raises(ValueError, match="symbol mismatch"):
        assess(raw=wrong)
    with pytest.raises(ValueError, match="exactly one"):
        assess(raw=[brackets(), brackets()])


def test_cross_margin_hedge_mode_and_multi_asset_refused():
    for unsupported in (
        replace(position(), margin_type="CROSSED"),
        replace(position(), position_side="SHORT"),
        replace(position(), multi_assets=True),
    ):
        with pytest.raises(ValueError, match="only one-way isolated"):
            assess(position_=unsupported)


def test_leverage_above_pinned_bracket_limit_rejected():
    with pytest.raises(ValueError, match="exceeds account notional bracket"):
        assess(position_=replace(position(), leverage=11))


def test_insufficient_initial_margin_refused():
    with pytest.raises(ValueError, match="insufficient isolated wallet"):
        assess(position_=replace(position(), isolated_wallet_after_entry=Decimal("100")))


def test_gap_duplicate_or_wrong_kline_end_timestamp_rejected():
    missing = bars()
    missing.pop(1)
    with pytest.raises(ValueError, match="missing, overlapping"):
        assess(klines=missing)
    wrong_time = bars()
    wrong_time[0][6] += 1
    with pytest.raises(ValueError, match="misaligned"):
        assess(klines=wrong_time)


def test_last_price_is_not_accepted_without_full_mark_candle_schema():
    with pytest.raises(ValueError, match="mark-price kline schema"):
        assess(klines=[[START, "600", "610", "590", "600"]])


def test_stale_or_future_snapshot_rejected():
    with pytest.raises(ValueError, match="stale or unordered"):
        assess(capture_at=START + 60_000, mark_at=START + 300_000)
    with pytest.raises(ValueError, match="stale, future"):
        assess(mark_at=START + 500_000, capture_at=START + 500_000)


def test_unpinned_or_tampered_mark_prices_rejected():
    with pytest.raises(ValueError, match="mark-price source SHA-256 mismatch"):
        assess(expected_marks="a" * 64)
    bad = bars()
    bad[1][2] = "4"
    with pytest.raises(ValueError, match="inconsistent mark-price"):
        assess(klines=bad)


def test_extra_large_notional_beyond_tiers_rejected_without_extrapolation():
    with pytest.raises(ValueError, match="exceeds pinned leverage-bracket coverage"):
        assess(position_=replace(position(), quantity=Decimal("50")))


def test_margin_liability_or_fee_buffer_cannot_be_negative():
    for bad in (
        replace(position(), funding_debit_reserve=Decimal("-1")),
        replace(position(), close_fee_bps=Decimal("-1")),
        replace(position(), buffer_bps=Decimal("-1")),
    ):
        with pytest.raises(ValueError, match="adverse funding"):
            assess(position_=bad)


def test_malformed_bracket_tiers_do_not_leak_into_risk_math():
    bad = brackets()
    bad["brackets"][1]["notionalFloor"] = "900"
    with pytest.raises(ValueError, match="tier coverage"):
        assess(raw=bad)
    bad2 = brackets()
    bad2["brackets"][2]["bracket"] = 2
    with pytest.raises(ValueError, match="unordered"):
        assess(raw=bad2)


def test_mark_price_symbol_and_endpoint_provenance_cannot_be_swapped():
    raw = brackets()
    klines = bars()
    digest = source_sha256({"endpoint": "/fapi/v1/markPriceKlines", "symbol": "BTCUSDT", "candles": klines})
    evidence = MarkEvidence(
        klines,
        digest,
        START,
        START + 3 * 60_000,
        START + 3 * 60_000,
        "ETHUSDT",
        "/fapi/v1/markPriceKlines",
    )
    with pytest.raises(ValueError, match="endpoint or symbol provenance"):
        assess_isolated_short(
            raw, position(), evidence, expected_bracket_sha256=source_sha256(raw), expected_mark_sha256=digest
        )
    wrong = replace(evidence, symbol="BTCUSDT", endpoint="/fapi/v1/klines")
    with pytest.raises(ValueError, match="endpoint or symbol provenance"):
        assess_isolated_short(
            raw, position(), wrong, expected_bracket_sha256=source_sha256(raw), expected_mark_sha256=digest
        )
