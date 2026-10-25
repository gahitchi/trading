"""Offline restart replays *all* durable source inputs and derived accounting evidence."""

from __future__ import annotations

from decimal import Decimal

import pytest

from lab.markets import AssetClass, ContractKind, Instrument, MarkPrice, OfflineSimulator, OrderIntent, Side, Trade
from lab.markets.journal import EventJournal, JournalError
from lab.markets.recovery import recover_offline

D = Decimal


def instrument() -> Instrument:
    return Instrument(
        "SIM",
        "BTC-USD-LINEAR",
        AssetClass.CRYPTO,
        ContractKind.LINEAR_DERIVATIVE,
        "USD",
        D("0.1"),
        D("0.001"),
        D("0.001"),
        D("5"),
    )


def test_recover_partial_fill_pending_risk_and_funding_then_continue(tmp_path) -> None:
    path = tmp_path / "events.db"
    contract = instrument()
    with EventJournal(path) as journal:
        sim = OfflineSimulator(contract, journal=journal, taker_fee_bps=D("0"), slippage_bps=D("0"))
        sim.on_mark(MarkPrice("mark1", 1, 1, D("100")))
        sim.on_trade(Trade("trade1", 1, 1, D("100"), D("100")))
        sim.submit(OrderIntent("entry", contract.instrument_id, Side.BUY, D("0.5"), 1))
        sim.on_mark(MarkPrice("mark2", 2, 2, D("100")))
        assert len(sim.on_trade(Trade("trade2", 2, 2, D("100"), D("100")))) == 1
        assert sim.position == D("0.5")
        sim.submit(OrderIntent("exit", contract.instrument_id, Side.SELL, D("0.25"), 2, reduce_only=True))
        sim.on_mark(MarkPrice("mark3", 3, 3, D("100")))
        partial = sim.on_trade(Trade("trade3", 3, 3, D("100"), D("1")))
        assert len(partial) == 1
        assert partial[0].quantity == D("0.1")
        assert sim.pending["exit"].remaining == D("0.15")
        assert sim.apply_funding("pay1", D("0.01"), 4) == D("0.4")
        report, pending = sim.report(), sim.pending["exit"].remaining
        journal.verify()
    with EventJournal(path) as journal:
        recovered = recover_offline(contract, journal, taker_fee_bps=D("0"), slippage_bps=D("0"))
        assert recovered.report() == report
        assert recovered.pending["exit"].remaining == pending
        assert [f.trade_id for f in recovered.fills] == ["trade2", "trade3"]
        assert recovered.used_ids == {"entry", "exit"}
        assert recovered.used_funding_ids == {"pay1"}
        with pytest.raises(ValueError, match="duplicate"):
            recovered.submit(OrderIntent("entry", contract.instrument_id, Side.BUY, D("0.5"), 4))
        recovered.on_mark(MarkPrice("mark4", 5, 5, D("101")))
        recovered.on_trade(Trade("trade4", 5, 5, D("101"), D("100")))
        assert "exit" not in recovered.pending
        post = recovered.report()
        journal.verify()
    with EventJournal(path) as journal:
        restarted = recover_offline(contract, journal, taker_fee_bps=D("0"), slippage_bps=D("0"))
        assert restarted.report() == post


@pytest.mark.parametrize(
    ("balance", "fees", "slippage"),
    [
        ("2000", "0", "0"),
        ("1000", "1", "0"),
        ("1000", "0", "2"),
    ],
)
def test_recovery_rejects_different_economic_assumptions(tmp_path, balance, fees, slippage) -> None:
    path = tmp_path / "events.db"
    with EventJournal(path) as journal:
        OfflineSimulator(instrument(), journal=journal, taker_fee_bps=D("0"), slippage_bps=D("0"))
    with EventJournal(path) as journal, pytest.raises(JournalError, match="identity"):
        recover_offline(
            instrument(),
            journal,
            starting_balance=D(balance),
            taker_fee_bps=D(fees),
            slippage_bps=D(slippage),
        )


def test_replay_detects_missing_derived_fill_even_with_valid_hash_chain(tmp_path) -> None:
    path = tmp_path / "events.db"
    with EventJournal(path) as journal:
        sim = OfflineSimulator(instrument(), journal=journal, taker_fee_bps=D("0"), slippage_bps=D("0"))
        sim.on_mark(MarkPrice("mark1", 1, 1, D("100")))
        sim.on_trade(Trade("trade1", 1, 1, D("100"), D("100")))
        sim.submit(OrderIntent("entry", instrument().instrument_id, Side.BUY, D("1"), 1))
        # Crash after trade observation was persisted, before its derived fill was saved.
        journal.append(
            "trade:trade2",
            "trade",
            2,
            {"price": "100", "quantity": "100", "received_ns": 2},
        )
        journal.verify()
    with EventJournal(path) as journal, pytest.raises(JournalError, match="incomplete"):
        recover_offline(instrument(), journal, taker_fee_bps=D("0"), slippage_bps=D("0"))


def test_recovery_refuses_unrecognized_broker_lifecycle_events(tmp_path) -> None:
    path = tmp_path / "events.db"
    with EventJournal(path) as journal:
        OfflineSimulator(instrument(), journal=journal)
        journal.append("external:1", "ack", 2, {"venue_id": "x"})
    with EventJournal(path) as journal, pytest.raises(JournalError, match="unsupported offline event"):
        recover_offline(instrument(), journal)


def test_recovery_never_adopts_legacy_journal_without_session_fingerprint(tmp_path) -> None:
    path = tmp_path / "events.db"
    with EventJournal(path) as journal:
        journal.append("trade:one", "trade", 1, {"price": "100", "quantity": "1", "received_ns": 1})
    with EventJournal(path) as journal, pytest.raises(JournalError, match="identity"):
        recover_offline(instrument(), journal)
