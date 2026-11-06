"""Portable sandbox receipt tests; no NautilusTrader dependency required."""

from datetime import UTC, datetime

import pytest

import lab.sandbox_capture as capture_mod
from lab.sandbox_capture import PaperOrderEventCapture, event_receipt

IID = "BTCUSDT-PERP.BINANCE"

NS = int(datetime(2026, 11, 5, tzinfo=UTC).timestamp() * 1_000_000_000)


def event(kind="OrderFilled", **override):
    values = {
        "event_id": "evt-1",
        "trader_id": "TRADER-001",
        "strategy_id": "STRAT-001",
        "instrument_id": IID,
        "client_order_id": "CO-1",
        "ts_event": NS,
        "ts_init": NS + 1,
        "trade_id": "trade-1",
        "venue_order_id": "V-1",
        "account_id": "sandbox-1",
        "order_side": "SELL",
        "order_type": "LIMIT",
        "last_qty": "0.25",
        "last_px": "100.50",
        "currency": "USDT",
        "liquidity_side": "TAKER",
        "commission": "0.01 USDT",
        "position_id": None,
        "reconciliation": False,
    }
    values.update(override)
    return type(kind, (), values)()


def archive(tmp_path):
    return PaperOrderEventCapture(tmp_path / "orders.jsonl", session_id="test-20261105", instrument_id=IID)


def test_fill_receipt_preserves_exact_quantity_fee_price_order_ids():
    receipt = event_receipt(event(), received_ns=NS + 2, instrument_id=IID)
    assert receipt["kind"] == "OrderFilled"
    assert receipt["last_qty"] == "0.25"
    assert receipt["last_px"] == "100.50"
    assert receipt["commission"] == "0.01 USDT"
    assert receipt["trade_id"] == "trade-1"
    assert receipt["ts_event_ns"] == NS


def test_partial_fills_replay_and_flat_start_position_match(tmp_path):
    capture = archive(tmp_path)
    capture.append(event(), received_ns=NS + 2)
    capture.append(event(event_id="evt-2", trade_id="trade-2", last_qty="0.1"), received_ns=NS + 3)
    assert capture.compare_flat_start_positions({IID: "-0.35"})[IID] == "-0.35"
    restarted = archive(tmp_path)
    assert len(restarted.receipts()) == 2
    with pytest.raises(ValueError, match="mismatch"):
        restarted.compare_flat_start_positions({IID: "-0.30"})
    with pytest.raises(ValueError, match="duplicate sandbox event ID"):
        restarted.append(event(), received_ns=NS + 4)


def test_fill_restatement_cannot_be_double_booked(tmp_path):
    capture = archive(tmp_path)
    capture.append(event(), received_ns=NS + 2)
    capture.append(event(event_id="evt-2", last_px="101.00"), received_ns=NS + 3)
    with pytest.raises(ValueError, match="restated"):
        capture.compare_flat_start_positions({IID: "-0.25"})


def test_fill_void_blocks_independent_reconciliation(tmp_path):
    capture = archive(tmp_path)
    capture.append(event(), received_ns=NS + 2)
    capture.append(event("OrderFillVoided", event_id="evt-2"), received_ns=NS + 3)
    with pytest.raises(ValueError, match="voided"):
        capture.compare_flat_start_positions({IID: "-0.25"})


def test_missing_original_or_invalid_fill_is_refused_before_write(tmp_path):
    capture = archive(tmp_path)
    initial = capture.path.read_bytes()
    with pytest.raises(ValueError, match="missing last_qty"):
        capture.append(event(last_qty=None), received_ns=NS + 2)
    with pytest.raises(ValueError, match="invalid sandbox fill"):
        capture.append(event(last_qty="-1"), received_ns=NS + 2)
    with pytest.raises(ValueError, match="another instrument"):
        capture.append(event(instrument_id="WRONG.SANDBOX"), received_ns=NS + 2)
    with pytest.raises(ValueError, match="unsupported"):
        capture.append(event("UnknownOrderEvent"), received_ns=NS + 2)
    assert capture.path.read_bytes() == initial


def test_archive_detects_changed_receipt_and_torn_append(tmp_path):
    capture = archive(tmp_path)
    capture.append(event(), received_ns=NS + 2)
    raw = capture.path.read_bytes()
    capture.path.write_bytes(raw.replace(b"trade-1", b"trade-x"))
    with pytest.raises(ValueError, match="hash mismatch"):
        capture.receipts()
    capture.path.write_bytes(raw + b'{"broken":')
    with pytest.raises(ValueError, match="incomplete"):
        capture.receipts()


def test_archive_detects_truncated_committed_suffix(tmp_path):
    capture = archive(tmp_path)
    capture.append(event(), received_ns=NS + 2)
    capture.path.write_bytes(capture.path.read_bytes().split(b"\n")[0] + b"\n")
    with pytest.raises(ValueError, match="checkpoint mismatch"):
        capture.receipts()


def test_checkpoint_failure_blocks_all_future_appends(tmp_path, monkeypatch):
    capture = archive(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError("injected failure")

    monkeypatch.setattr(capture_mod, "atomic_json", fail)
    with pytest.raises(OSError, match="injected failure"):
        capture.append(event(), received_ns=NS + 2)
    with pytest.raises(ValueError, match="checkpoint mismatch"):
        capture.receipts()


def test_different_session_and_symlink_are_rejected(tmp_path):
    capture = archive(tmp_path)
    with pytest.raises(ValueError, match="source/session mismatch"):
        PaperOrderEventCapture(capture.path, session_id="other", instrument_id=IID)
    link = tmp_path / "alias.jsonl"
    link.symlink_to(capture.path)
    with pytest.raises(ValueError, match="symlinks"):
        PaperOrderEventCapture(link, session_id="test-20261105", instrument_id=IID)


