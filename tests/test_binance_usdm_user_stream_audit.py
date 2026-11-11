"""Synthetic, offline USD-M user-data consistency checks."""

from copy import deepcopy

import pytest

from lab.binance_usdm_margin_rules import source_sha256
from lab.binance_usdm_user_stream_audit import audit_user_stream

START = 1_767_225_600_000


def bundle():
    return {
        "version": 1,
        "environment": "binance-usdm-demo",
        "symbol": "BTCUSDT",
        "start_ms": START,
        "end_ms": START + 120_000,
        "opening": {"symbol": "BTCUSDT", "wallet_balance": "300", "position_amt": "-2"},
        "closing": {"symbol": "BTCUSDT", "wallet_balance": "298", "position_amt": "-2"},
        "stream": {
            "connected_ms": START - 1000,
            "disconnected_ms": START + 121_000,
            "reconnects": 0,
            "listen_key_expired": False,
            "message_capture_complete": True,
        },
        "events": [account_update()],
    }


def account_update(*, time_ms=START + 30_000, wallet="298", qty="-2"):
    return {
        "received_ms": time_ms + 40,
        "payload": {
            "e": "ACCOUNT_UPDATE",
            "E": time_ms,
            "T": time_ms - 5,
            "a": {
                "m": "FUNDING_FEE",
                "B": [{"a": "USDT", "wb": wallet, "cw": "0", "bc": "-2"}],
                "P": [
                    {
                        "s": "BTCUSDT",
                        "pa": qty,
                        "ep": "600",
                        "cr": "0",
                        "up": "0",
                        "mt": "isolated",
                        "iw": "300",
                        "ps": "BOTH",
                    }
                ],
            },
        },
    }


def trade_update(*, time_ms=START + 50_000, trade_id=4, filled="0.1", last="0.1"):
    return {
        "received_ms": time_ms + 40,
        "payload": {
            "e": "ORDER_TRADE_UPDATE",
            "E": time_ms,
            "T": time_ms - 5,
            "o": {
                "s": "BTCUSDT",
                "i": 11,
                "t": trade_id,
                "x": "TRADE",
                "X": "PARTIALLY_FILLED",
                "z": filled,
                "l": last,
                "rp": "0",
                "n": "0.01",
                "N": "USDT",
                "S": "SELL",
                "ps": "BOTH",
            },
        },
    }


def check(source):
    return audit_user_stream(source, expected_sha256=source_sha256(source))


def test_consistent_observed_funding_update_is_not_a_completeness_claim():
    result = check(bundle())
    assert result.account_updates == 1 and result.order_updates == 0
    assert result.reported_wallet_delta == result.observed_wallet_delta == -2
    assert result.ending_quantity == -2
    assert result.status == "OBSERVED_EVENTS_CONSISTENT_UNVERIFIED"
    assert not result.websocket_delivery_complete
    assert not result.provider_authenticated
    assert not result.execution_history_complete
    assert not result.risk_ready and not result.execution_authorized


def test_empty_stream_can_only_support_unchanged_snapshots():
    b = bundle()
    b["events"] = []
    b["closing"]["wallet_balance"] = "300"
    result = check(b)
    assert result.account_updates == 0
    assert result.first_event_ms is None
    assert not result.websocket_delivery_complete
    b["closing"]["wallet_balance"] = "298"
    with pytest.raises(ValueError, match="does not reconcile"):
        check(b)


def test_wrong_external_hash_rejected():
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        audit_user_stream(bundle(), expected_sha256="0" * 64)


def test_stream_disconnect_reconnect_and_expiry_are_rejected():
    for field, value in (
        ("connected_ms", START + 1),
        ("disconnected_ms", START + 1000),
        ("reconnects", 1),
        ("listen_key_expired", True),
        ("message_capture_complete", False),
    ):
        b = bundle()
        b["stream"][field] = value
        with pytest.raises(ValueError, match="websocket capture gap"):
            check(b)


def test_wallet_and_position_mismatch_refused():
    b = bundle()
    b["closing"]["wallet_balance"] = "290"
    with pytest.raises(ValueError, match="does not reconcile"):
        check(b)
    b = bundle()
    b["closing"]["position_amt"] = "-3"
    with pytest.raises(ValueError, match="does not reconcile"):
        check(b)


def test_duplicate_or_disordered_events_refused():
    b = bundle()
    first = b["events"][0]
    b["events"] = [first, deepcopy(first)]
    with pytest.raises(ValueError, match="unordered"):
        check(b)
    b = bundle()
    second = account_update(time_ms=START + 15_000)
    b["events"].append(second)
    with pytest.raises(ValueError, match="unordered"):
        check(b)


def test_non_usdt_wallet_and_hedged_position_rejected():
    b = bundle()
    b["events"][0]["payload"]["a"]["B"][0]["a"] = "BTC"
    with pytest.raises(ValueError, match="unsupported balance"):
        check(b)
    b = bundle()
    b["events"][0]["payload"]["a"]["P"][0]["ps"] = "SHORT"
    with pytest.raises(ValueError, match="one-way isolated"):
        check(b)


def test_unknown_account_update_reason_or_event_rejected():
    b = bundle()
    b["events"][0]["payload"]["a"]["m"] = "UNVERIFIED"
    with pytest.raises(ValueError, match="unsupported account update reason"):
        check(b)
    b = bundle()
    b["events"][0]["payload"]["e"] = "listenKeyExpired"
    with pytest.raises(ValueError, match="unsupported stream event"):
        check(b)


def test_account_event_empty_balance_and_position_arrays_do_not_erase_state():
    b = bundle()
    b["events"][0]["payload"]["a"]["B"] = []
    b["events"][0]["payload"]["a"]["P"] = []
    b["closing"]["wallet_balance"] = "300"
    result = check(b)
    assert result.ending_quantity == -2 and result.reported_wallet_delta == 0


def test_observed_trade_updates_do_not_fabricate_settled_cash_or_fills():
    b = bundle()
    b["events"].append(trade_update())
    result = check(b)
    assert result.order_updates == 1
    assert result.unresolved_order_updates == 1
    assert result.observed_trade_ids == (4,)
    assert not result.execution_history_complete


def test_duplicate_trade_id_or_cumulative_fill_regression_rejected():
    b = bundle()
    b["events"].extend([trade_update(), trade_update(time_ms=START + 60_000)])
    with pytest.raises(ValueError, match="duplicate or empty trade"):
        check(b)
    b = bundle()
    b["events"].extend([trade_update(), trade_update(time_ms=START + 60_000, trade_id=5, filled="0.05")])
    with pytest.raises(ValueError, match="decreased"):
        check(b)


def test_trade_cumulative_fill_increment_must_match_last_fill():
    b = bundle()
    b["events"].extend([trade_update(), trade_update(time_ms=START + 60_000, trade_id=5, filled="0.2", last="0.02")])
    with pytest.raises(ValueError, match="does not conserve"):
        check(b)


def test_malformed_capture_timestamp_and_schema_refused():
    b = bundle()
    b["events"][0]["received_ms"] = START
    with pytest.raises(ValueError, match="unordered, out-of-window"):
        check(b)
    b = bundle()
    b["events"][0]["payload"]["T"] = START - 100_000
    with pytest.raises(ValueError, match="unordered, out-of-window"):
        check(b)
    b = bundle()
    b["events"][0]["payload"]["a"]["P"] = "not a list"
    with pytest.raises(ValueError, match="incomplete account"):
        check(b)


def test_credential_fields_rejected_before_processing():
    b = bundle()
    b["events"][0]["payload"]["API_KEY"] = "not-real"
    with pytest.raises(ValueError, match="prohibited credential"):
        check(b)


def test_original_evidence_untouched_after_audit():
    b = bundle()
    original = deepcopy(b)
    check(b)
    assert b == original
