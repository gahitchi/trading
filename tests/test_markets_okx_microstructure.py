"""Contract tests for public EEA order books and indicative funding observations."""

from __future__ import annotations

import io
import json
from decimal import Decimal

import pytest

from lab.markets.okx_microstructure import (
    current_public_book,
    current_public_funding,
    parse_public_book,
    parse_public_funding,
)
from lab.markets.okx_public import MarketDataError

D = Decimal


def level(price: str, quantity: str = "1.5", orders: str = "2") -> list[str]:
    return [price, quantity, "0", orders]


def book_row() -> dict:
    return {
        "bids": [level("99.9"), level("99.8")],
        "asks": [level("100.1"), level("100.2")],
        "ts": "1000",
        "seqId": 7,
    }


def funding_row() -> dict:
    return {
        "instId": "BTC-USDT-SWAP",
        "instType": "SWAP",
        "ts": "1000",
        "fundingRate": "-0.000125",
        "fundingTime": "2000",
        "nextFundingTime": "3000",
    }


def wrap(row: dict) -> dict:
    return {"code": "0", "data": [row]}


def test_snapshot_preserves_exact_bid_ask_native_size_and_sequence() -> None:
    snapshot = parse_public_book(wrap(book_row()), "BTC-USDT-SWAP", 2_000_000_000, depth=5)
    assert snapshot.sequence_id == 7
    assert snapshot.best_bid == D("99.9")
    assert snapshot.best_ask == D("100.1")
    assert snapshot.spread == D("0.2")
    assert snapshot.bids[0].size_native == D("1.5")
    assert snapshot.asks[0].order_count == 2
    assert snapshot.exchange_ns == 1_000_000_000
    assert snapshot.received_ns == 2_000_000_000
    assert snapshot.source == "OKX_EEA_PUBLIC_REST_BOOK"


def test_crossed_preopen_book_never_pretends_executable_spread() -> None:
    crossed = book_row()
    crossed["asks"][0][0] = "99.8"
    crossed["asks"][1][0] = "100.2"
    observed = parse_public_book(wrap(crossed), "BTC-USDT-SWAP", 2_000_000_000)
    assert observed.crossed_or_locked
    assert observed.spread is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(seqId=True),
        lambda r: r.update(seqId=-1),
        lambda r: r.update(ts="1000.3"),
        lambda r: r.update(ts=1000),
        lambda r: r.update(ts="9999999999999999999"),
        lambda r: r.update(asks=[]),
        lambda r: r.update(bids=[level("99.9"), level("99.9")]),
        lambda r: r.update(asks=[level("100.2"), level("100.1")]),
        lambda r: r.update(bids=[level("99.8"), level("99.9")]),
        lambda r: r.update(bids=[level("99.9", "NaN")]),
        lambda r: r.update(bids=[level("99.9", "-0.1")]),
        lambda r: r.update(bids=[level("99.9", "0")]),
        lambda r: r.update(bids=[level("99.9", orders="-1")]),
        lambda r: r.update(bids=[["99.9", "1.0", "0"]]),
        lambda r: r.update(bids=[level("99.9", "1.0", orders="a")]),
        lambda r: r.update(bids=[level("99.9", "1.0"), level("99.8", "1.0"), level("99.7", "1.0")]),
    ],
)
def test_corrupt_or_unsorted_book_is_rejected(mutate) -> None:
    row = book_row()
    mutate(row)
    with pytest.raises(MarketDataError):
        parse_public_book(wrap(row), "BTC-USDT-SWAP", 2_000_000_000, depth=2)


@pytest.mark.parametrize("bad", [-1, 0, 51, True, 2.3])
def test_depth_is_bounded(bad) -> None:
    with pytest.raises(MarketDataError, match="depth"):
        parse_public_book(wrap(book_row()), "BTC-USDT-SWAP", 2_000_000_000, depth=bad)


def test_future_book_timestamp_or_multirow_response_is_rejected() -> None:
    with pytest.raises(MarketDataError, match="timestamps"):
        parse_public_book(wrap(book_row()), "BTC-USDT-SWAP", 500_000_000)
    with pytest.raises(MarketDataError, match="exactly one"):
        parse_public_book({"code": "0", "data": [book_row(), book_row()]}, "BTC-USDT-SWAP", 2_000_000_000)


def test_funding_indication_is_not_realized_cash_settlement() -> None:
    value = parse_public_funding(wrap(funding_row()), "BTC-USDT-SWAP", 2_000_000_000)
    assert value.indicated_rate == D("-0.000125")
    assert value.funding_time_ns == 2_000_000_000
    assert value.next_funding_time_ns == 3_000_000_000
    assert value.realized_payment_qualified is False
    assert value.source == "OKX_EEA_PUBLIC_INDICATIVE_FUNDING"


@pytest.mark.parametrize(
    "changes",
    [
        {"instId": "ETH-USDT-SWAP"},
        {"instType": "SPOT"},
        {"fundingRate": "NaN"},
        {"fundingRate": "1.2"},
        {"fundingRate": 0.01},
        {"fundingTime": "0"},
        {"nextFundingTime": "1999"},
        {"ts": "1000.2"},
    ],
)
def test_funding_contract_rejects_malformed_or_wrong_instrument(changes) -> None:
    row = funding_row()
    row.update(changes)
    with pytest.raises(MarketDataError):
        parse_public_funding(wrap(row), "BTC-USDT-SWAP", 2_000_000_000)


def test_funding_rejects_non_swap_and_future_exchange_timestamp() -> None:
    with pytest.raises(MarketDataError, match="swap"):
        parse_public_funding(wrap(funding_row()), "BTC-USDT", 2_000_000_000)
    with pytest.raises(MarketDataError, match="timestamps"):
        parse_public_funding(wrap(funding_row()), "BTC-USDT-SWAP", 900_000_000)


def test_read_only_endpoints_use_eea_pinned_transport(monkeypatch) -> None:
    import lab.markets.okx_observations as transport

    requested = []

    class Response(io.BytesIO):
        def __init__(self, payload, url):
            super().__init__(json.dumps(payload).encode())
            self.url = url

        def geturl(self):
            return self.url

    def fake_urlopen(request, timeout):
        assert request.get_header("Ok-access-key") is None
        assert timeout == 10
        requested.append(request.full_url)
        if "/market/books?" in request.full_url:
            return Response(wrap(book_row()), "https://eea.okx.com/api/v5/market/books")
        return Response(wrap(funding_row()), "https://eea.okx.com/api/v5/public/funding-rate")

    monkeypatch.setattr(transport, "urlopen", fake_urlopen)
    monkeypatch.setattr(transport.time, "time_ns", lambda: 2_000_000_000)
    assert current_public_book("BTC-USDT-SWAP", depth=2).spread == D("0.2")
    assert current_public_funding("BTC-USDT-SWAP").realized_payment_qualified is False
    assert requested[0].startswith("https://eea.okx.com/api/v5/market/books?")
    assert "sz=2" in requested[0]
    assert requested[1].startswith("https://eea.okx.com/api/v5/public/funding-rate?")


def test_public_book_transport_blocks_redirect_without_using_unqualified_feed(monkeypatch) -> None:
    import lab.markets.okx_observations as transport

    class Response(io.BytesIO):
        def geturl(self):
            return "https://www.okx.com/api/v5/market/books"

    monkeypatch.setattr(
        transport,
        "urlopen",
        lambda request, timeout: Response(json.dumps(wrap(book_row())).encode()),
    )
    with pytest.raises(MarketDataError, match="outside qualified region"):
        current_public_book("BTC-USDT-SWAP")
