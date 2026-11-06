"""Nautilus-compatible account-cache adapter checks without native dependencies."""

from decimal import Decimal

import pytest

from lab.nautilus_snapshot import record_sandbox_snapshot, verify_fresh_sandbox_start
from lab.paper_account_snapshot import PaperAccountSnapshotJournal
from lab.sandbox_capture import PaperOrderEventCapture
from tests.test_sandbox_capture import IID, NS, event


class Instrument:
    venue = "BINANCE"

    def __str__(self):
        return IID


class Currency:
    code = "USDT"

    def __str__(self):
        return self.code


class Money:
    def __init__(self, amount, currency=None):
        self._value = Decimal(amount)
        self.currency = currency or Currency()

    def as_decimal(self):
        return self._value


class Balance:
    currency = Currency()

    def __init__(self, total="1000.00", free="1000.00", locked="0.00"):
        self.total = Money(total)
        self.free = Money(free)
        self.locked = Money(locked)


class Account:
    id = "sandbox-1"

    def __init__(self, balance=None):
        self._balance = Balance() if balance is None else balance

    def balances(self):
        return {Currency(): self._balance}


class Position:
    instrument_id = Instrument()

    def __init__(self, signed_qty):
        signed = Decimal(signed_qty)
        self.side = type("PositionSide", (), {"name": "LONG" if signed > 0 else "SHORT"})()
        self.quantity = type("Quantity", (), {"as_decimal": lambda self: abs(signed)})()


class Order:
    instrument_id = Instrument()

    def __init__(self, status, client_order_id, leaves):
        self.status = type("Status", (), {"name": status})()
        self.client_order_id = client_order_id
        self.leaves_qty = Decimal(leaves)


class Cache:
    def __init__(self):
        self.account = Account()
        self._orders = []
        self._positions = []

    def account_for_venue(self, venue):
        return self.account if venue == "BINANCE" else None

    def orders(self):
        return self._orders

    def positions_open(self):
        return self._positions


def init(tmp_path):
    capture = PaperOrderEventCapture(tmp_path / "orders.jsonl", session_id="session-a", instrument_id=IID)
    return Cache(), capture


def test_producer_reads_actual_cache_balances_and_fill_signed_inventory(tmp_path):
    cache, source = init(tmp_path)
    aid = verify_fresh_sandbox_start(cache, Instrument(), source)
    assert aid == "sandbox-1"
    store = PaperAccountSnapshotJournal(tmp_path / "accounts.jsonl", source, account_id=aid)
    source.append(event(), received_ns=NS + 2)
    cache._positions = [Position("-0.25")]
    cache._orders = [Order("FILLED", "CO-1", "0")]
    report = record_sandbox_snapshot(cache, Instrument(), source, store, at_ns=NS + 3, initial_account_id=aid)
    assert report["signed_quantity"] == "-0.25"
    assert report["cash"]["USDT"] == {"total": "1000.00", "available": "1000.00", "locked": "0.00"}
    assert not report["risk_ready"]
    reopened = PaperAccountSnapshotJournal(store.path, source, account_id=aid)
    assert len(reopened.snapshots()) == 1


def test_next_snapshot_captures_live_order_and_partial_close(tmp_path):
    cache, source = init(tmp_path)
    aid = verify_fresh_sandbox_start(cache, Instrument(), source)
    store = PaperAccountSnapshotJournal(tmp_path / "accounts.jsonl", source, account_id=aid)
    source.append(event(), received_ns=NS + 2)
    cache._positions = [Position("-0.25")]
    record_sandbox_snapshot(cache, Instrument(), source, store, at_ns=NS + 3, initial_account_id=aid)
    source.append(
        event(
            event_id="evt-2",
            trade_id="trade-2",
            client_order_id="CO-2",
            order_side="BUY",
            last_qty="0.1",
        ),
        received_ns=NS + 5,
    )
    cache._positions = [Position("-0.15")]
    cache._orders = [Order("ACCEPTED", "CO-2", "0.1")]
    report = record_sandbox_snapshot(cache, Instrument(), source, store, at_ns=NS + 6, initial_account_id=aid)
    assert report["signed_quantity"] == "-0.15"
    assert report["open_orders_known_ids"] == 1
    assert not report["order_state_complete"]
    assert store.snapshots()[1].open_orders == {"CO-2": "0.1"}


def test_initial_inventory_and_other_orders_block_fresh_baseline(tmp_path):
    cache, source = init(tmp_path)
    cache._positions = [Position("1")]
    with pytest.raises(ValueError, match="did not start flat"):
        verify_fresh_sandbox_start(cache, Instrument(), source)
    cache._positions = []
    cache._orders = [Order("ACCEPTED", "CO-1", "1")]
    with pytest.raises(ValueError, match="did not start flat"):
        verify_fresh_sandbox_start(cache, Instrument(), source)


@pytest.mark.parametrize(
    "balance",
    [
        Balance(total="1000.00", free="1001.00", locked="0.00"),
        Balance(total="NaN", free="NaN", locked="0.00"),
    ],
)
def test_unreconciled_sandbox_balances_block_production(tmp_path, balance):
    cache, source = init(tmp_path)
    cache.account = Account(balance)
    if balance.total.as_decimal().is_finite():
        aid = verify_fresh_sandbox_start(cache, Instrument(), source)
        store = PaperAccountSnapshotJournal(tmp_path / "accounts.jsonl", source, account_id=aid)
        with pytest.raises(ValueError, match="cash balance"):
            record_sandbox_snapshot(cache, Instrument(), source, store, at_ns=NS + 3, initial_account_id=aid)
        assert store.snapshots() == ()
    else:
        with pytest.raises(ValueError, match=r"nonfinite|finite"):
            verify_fresh_sandbox_start(cache, Instrument(), source)


def test_capture_order_stream_must_be_fresh_at_session_start(tmp_path):
    cache, source = init(tmp_path)
    source.append(event(), received_ns=NS + 2)
    with pytest.raises(ValueError, match="fresh order capture"):
        verify_fresh_sandbox_start(cache, Instrument(), source)


def test_changed_account_and_out_of_scope_exposure_are_rejected(tmp_path):
    cache, source = init(tmp_path)
    aid = verify_fresh_sandbox_start(cache, Instrument(), source)
    store = PaperAccountSnapshotJournal(tmp_path / "accounts.jsonl", source, account_id=aid)
    cache.account = Account()
    cache.account.id = "other-account"
    with pytest.raises(ValueError, match="identity"):
        record_sandbox_snapshot(cache, Instrument(), source, store, at_ns=NS + 3, initial_account_id=aid)
    cache.account.id = aid
    cache._positions = [Position("0.3")]
    cache._positions[0].instrument_id = "OTHER-PERP.BINANCE"
    with pytest.raises(ValueError, match="other instrument"):
        record_sandbox_snapshot(cache, Instrument(), source, store, at_ns=NS + 3, initial_account_id=aid)
    cache._positions = []
    cache._orders = [Order("ACCEPTED", "foreign", "1")]
    cache._orders[0].instrument_id = "OTHER-PERP.BINANCE"
    with pytest.raises(ValueError, match="outside captured instrument"):
        record_sandbox_snapshot(cache, Instrument(), source, store, at_ns=NS + 3, initial_account_id=aid)


def test_unknown_order_and_bad_remaining_quantity_fail_closed(tmp_path):
    cache, source = init(tmp_path)
    aid = verify_fresh_sandbox_start(cache, Instrument(), source)
    store = PaperAccountSnapshotJournal(tmp_path / "accounts.jsonl", source, account_id=aid)
    cache._orders = [Order("ACCEPTED", "never-captured", "1")]
    with pytest.raises(ValueError, match="absent from captured"):
        record_sandbox_snapshot(cache, Instrument(), source, store, at_ns=NS + 3, initial_account_id=aid)
    cache._orders = [Order("ACCEPTED", "never-captured", "0")]
    with pytest.raises(ValueError, match="no positive leaves"):
        record_sandbox_snapshot(cache, Instrument(), source, store, at_ns=NS + 3, initial_account_id=aid)


def test_cannot_record_without_known_cache_account(tmp_path):
    cache, source = init(tmp_path)
    cache.account = None
    with pytest.raises(ValueError, match="missing or ambiguous"):
        verify_fresh_sandbox_start(cache, Instrument(), source)
