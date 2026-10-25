"""Idempotency and corruption detection for the broker-neutral journal."""

from __future__ import annotations

import sqlite3

import pytest

from lab.markets.journal import EventJournal, JournalError


def test_durable_append_and_idempotent_restart(tmp_path) -> None:
    db = tmp_path / "sim" / "events.sqlite3"
    with EventJournal(db) as journal:
        seq1 = journal.append("orderA", "intent", 123, {"side": "sell", "qty": "0.001"})
        assert journal.append("orderA", "intent", 123, {"qty": "0.001", "side": "sell"}) == seq1
        with pytest.raises(JournalError, match="idempotency"):
            journal.append("orderA", "intent", 123, {"qty": "2.0"})
        seq2 = journal.append("fillA", "fill", 125, {"fee": "0.0005"})
        assert seq2 > seq1
        journal.verify()
    with EventJournal(db) as reopened:
        assert [r["event_id"] for r in reopened.read_all()] == ["orderA", "fillA"]
        assert reopened.append("fillA", "fill", 125, {"fee": "0.0005"}) == seq2


def test_corrupted_evidence_rejected_on_restart(tmp_path) -> None:
    path = tmp_path / "events.db"
    with EventJournal(path) as journal:
        journal.append("fillA", "fill", 100, {"qty": "1"})
    with sqlite3.connect(path) as db:
        db.execute("UPDATE events SET payload=? WHERE event_id=?", ('{"qty":"9"}', "fillA"))
    with pytest.raises(JournalError, match="digest mismatch"):
        EventJournal(path)


def test_money_floats_and_unknown_event_kinds_are_rejected(tmp_path) -> None:
    with EventJournal(tmp_path / "events.db") as journal:
        with pytest.raises(JournalError, match="no floats"):
            journal.append("bad1", "fill", 100, {"fee": 0.01})
        with pytest.raises(JournalError, match="unknown"):
            journal.append("bad2", "live_order", 100, {"qty": "1"})
        with pytest.raises(JournalError, match="timestamp"):
            journal.append("bad3", "fill", -1, {"qty": "1"})
        assert journal.read_all() == ()

def test_offline_simulator_journals_intents_and_fills_without_silent_restart(tmp_path) -> None:
    from decimal import Decimal

    from lab.markets import AssetClass, ContractKind, Instrument, OfflineSimulator, OrderIntent, Side, Trade

    d = Decimal
    contract = Instrument(
        "SIM",
        "BTC-USD-LINEAR",
        AssetClass.CRYPTO,
        ContractKind.LINEAR_DERIVATIVE,
        "USD",
        d("0.1"),
        d("0.001"),
        d("0.001"),
        d("5"),
    )
    path = tmp_path / "events.db"
    with EventJournal(path) as journal:
        sim = OfflineSimulator(contract, journal=journal, taker_fee_bps=d("0"), slippage_bps=d("0"))
        sim.on_trade(Trade("m1", 100, 100, d("100"), d("100")))
        sim.submit(OrderIntent("order1", contract.instrument_id, Side.SELL, d("1"), 100))
        sim.on_trade(Trade("m2", 200, 200, d("100"), d("100")))
        assert [e["kind"] for e in journal.read_all()] == ["mark", "intent", "mark", "fill"]
        assert sim.position == d("-1")
        with pytest.raises(JournalError, match="nonempty journal"):
            OfflineSimulator(contract, journal=journal)
