"""Offline WS sequence and displayed-liquidity evidence is not a fill claim."""

from __future__ import annotations

from decimal import Decimal

import pytest

from lab.markets.book_sequence import apply_recorded_book_frame, estimate_displayed_taker_capacity
from lab.markets.domain import AssetClass, ContractKind, Instrument, Side
from lab.markets.okx_public import MarketDataError

D = Decimal


def frame(action="snapshot", *, seq=7, prev=-1, bids=None, asks=None, ts="1000"):
    return {
        "arg": {"channel": "books", "instId": "BTC-USD-SWAP"},
        "action": action,
        "data": [{
            "ts": ts,
            "seqId": seq,
            "prevSeqId": prev,
            "checksum": 0,
            "bids": [["100.0", "10.000", "0", "2"]] if bids is None else bids,
            "asks": [["100.2", "10.000", "0", "2"]] if asks is None else asks,
        }],
    }


def instrument():
    return Instrument(
        "SIM", "BTC-USD-SWAP", AssetClass.CRYPTO,
        ContractKind.LINEAR_DERIVATIVE, "USD",
        D("0.1"), D("0.001"), D("0.001"), D("5"),
    )


def test_incremental_updates_modify_levels_and_share_a_contiguous_sequence():
    snap = apply_recorded_book_frame(None, frame(), received_ns=1_100_000_000)
    assert not snap.execution_qualified
    assert snap.best_ask == D("100.2")
    changes = frame(
        "update", seq=9, prev=7, ts="1001",
        bids=[["100.1", "4.000", "0", "1"]],
        asks=[["100.2", "0", "0", "0"], ["100.3", "20.000", "0", "4"]],
    )
    book = apply_recorded_book_frame(snap, changes, received_ns=1_200_000_000)
    assert book.seq_id == 9
    assert book.best_bid == D("100.1")
    assert book.best_ask == D("100.3")
    keepalive = frame("update", seq=9, prev=9, bids=[], asks=[], ts="1002")
    still = apply_recorded_book_frame(book, keepalive, received_ns=1_300_000_000)
    assert still.bids == book.bids and still.asks == book.asks
    assert still.received_ns == 1_300_000_000


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"prevSeqId": 6}, "gap"),
        ({"seqId": 7, "prevSeqId": 7, "asks": [["100.3", "1", "0", "1"]]}, "heartbeat"),
        ({"seqId": 6, "prevSeqId": 7}, "regressed"),
        ({"checksum": 41235}, "checksum"),
        ({"ts": "900"}, "regressed"),
        ({"bids": [["100.2", "10", "0", "1"]]}, "crossed"),
        ({"bids": [["100.0", "1", "0", "1"], ["100.0", "2", "0", "1"]]}, "unique"),
        ({"asks": [["100.2", "0", "0", "1"]]}, "zero order count"),
    ],
)
def test_causality_sequence_and_price_integrity_fail_closed(change, reason):
    snap = apply_recorded_book_frame(None, frame(), received_ns=1_100_000_000)
    payload = frame("update", seq=8, prev=7, ts="1001", bids=[], asks=[])
    payload["data"][0].update(change)
    with pytest.raises(MarketDataError, match=reason):
        apply_recorded_book_frame(snap, payload, received_ns=1_200_000_000)


def test_snapshot_replay_cannot_restart_or_backdate_data():
    snap = apply_recorded_book_frame(None, frame(), received_ns=1_100_000_000)
    with pytest.raises(MarketDataError, match="new replay epoch"):
        apply_recorded_book_frame(snap, frame(), received_ns=1_200_000_000)
    with pytest.raises(MarketDataError, match="future"):
        apply_recorded_book_frame(None, frame(), received_ns=900_000_000)
    with pytest.raises(MarketDataError, match="matching snapshot"):
        apply_recorded_book_frame(None, frame("update", prev=7), received_ns=1_200_000_000)


def test_displayed_liquidity_is_capped_to_fraction_not_recreated_as_fills():
    snap = apply_recorded_book_frame(
        None,
        frame(asks=[["100.2", "10.000", "0", "2"], ["100.3", "20.000", "0", "3"]]),
        received_ns=1_100_000_000,
    )
    result = estimate_displayed_taker_capacity(snap, instrument(), Side.BUY, D("2.000"))
    assert result.bounded_quantity == D("1.500")
    assert result.unfilled_quantity == D("0.500")
    assert result.average_displayed_price == (D("0.5") * D("100.2") + D("1") * D("100.3")) / D("1.5")
    assert not result.executable_fill_verified
    assert not result.execution_authorized


def test_rejects_wrong_contract_or_unrepresentable_quantity():
    snap = apply_recorded_book_frame(None, frame(), received_ns=1_100_000_000)
    with pytest.raises(MarketDataError, match="contract"):
        estimate_displayed_taker_capacity(
            snap,
            Instrument(
                "SIM", "ETH-USD-SWAP", AssetClass.CRYPTO,
                ContractKind.LINEAR_DERIVATIVE, "USD",
                D("0.1"), D("0.001"), D("0.001"), D("5"),
            ),
            Side.BUY, D("1.000"),
        )
    with pytest.raises(ValueError, match="step"):
        estimate_displayed_taker_capacity(snap, instrument(), Side.SELL, D("0.0001"))
    with pytest.raises(TypeError, match="exact Decimal"):
        estimate_displayed_taker_capacity(snap, instrument(), Side.SELL, 1.0)
