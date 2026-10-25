"""EEA market observations remain unprivileged and preserve venue semantics."""

from __future__ import annotations

import io
import json
from decimal import Decimal

import pytest

from lab.markets.domain import MarkPrice, Side, Trade
from lab.markets.journal import EventJournal
from lab.markets.okx_observations import (
    current_public_mark,
    parse_public_mark,
    parse_public_trades,
    recent_public_trades,
)
from lab.markets.okx_public import MarketDataError


def trade_row(trade_id: str = "12", timestamp: str = "1000", *, side: str = "sell") -> dict:
    return {
        "instId": "BTC-USDT-SWAP",
        "tradeId": trade_id,
        "px": "100.0",
        "sz": "0.25",
        "side": side,
        "source": "0",
        "ts": timestamp,
    }


def mark_row(timestamp: str = "1000") -> dict:
    return {"instType": "SWAP", "instId": "BTC-USDT-SWAP", "markPx": "100.012345", "ts": timestamp}


def test_recent_trades_preserve_venue_aggressor_and_exact_quantity() -> None:
    payload = {"code": "0", "data": [trade_row("12", "1000"), trade_row("11", "999", side="buy")]}
    trades = parse_public_trades(payload, "BTC-USDT-SWAP", 2_000_000_000)
    assert tuple(x.trade_id for x in trades) == ("11", "12")
    assert trades[0].taker_side == Side.BUY
    assert trades[1].taker_side == Side.SELL
    assert trades[0].venue_source == "0"
    assert trades[0].quantity == Decimal("0.25")
    assert trades[0].exchange_ns == 999_000_000
    assert trades[0].received_ns == 2_000_000_000


@pytest.mark.parametrize(
    "bad",
    [
        {"code": "50001", "data": []},
        {"code": "0", "data": [trade_row("12"), trade_row("12")]},
        {"code": "0", "data": [dict(trade_row(), instId="ETH-USDT-SWAP")]},
        {"code": "0", "data": [dict(trade_row(), side="hold")]},
        {"code": "0", "data": [dict(trade_row(), px="NaN")]},
        {"code": "0", "data": [dict(trade_row(), ts="3.12")]},
    ],
)
def test_invalid_recent_trades_fail_closed(bad: dict) -> None:
    with pytest.raises(MarketDataError):
        parse_public_trades(bad, "BTC-USDT-SWAP", 2_000_000_000)


def test_mark_is_independent_of_last_and_accepts_fine_precision() -> None:
    mark = parse_public_mark({"code": "0", "data": [mark_row()]}, "BTC-USDT-SWAP", 2_000_000_000)
    assert isinstance(mark, MarkPrice)
    assert mark.exchange_ns == 1_000_000_000
    assert mark.price == Decimal("100.012345")
    with pytest.raises(MarketDataError):
        parse_public_mark({"code": "0", "data": [mark_row()]}, "BTC-USDT", 2_000_000_000)


def test_public_snapshot_pins_eea_without_credentials_and_rejects_redirect(monkeypatch) -> None:
    import lab.markets.okx_observations as api

    class Response(io.BytesIO):
        def __init__(self, body, *, target=None):
            super().__init__(json.dumps(body).encode())
            self.target = target

        def geturl(self):
            return self.target or "https://eea.okx.com/api/v5/market/trades"

    def fake_urlopen(req, timeout):
        assert req.full_url.startswith("https://eea.okx.com/api/v5/")
        assert timeout == 10
        assert req.get_header("Ok-access-key") is None
        if "/market/trades?" in req.full_url:
            return Response({"code": "0", "data": [trade_row()]})
        return Response({"code": "0", "data": [mark_row()]}, target="https://eea.okx.com/api/v5/public/mark-price")

    monkeypatch.setattr(api, "urlopen", fake_urlopen)
    monkeypatch.setattr(api.time, "time_ns", lambda: 2_000_000_000)
    assert recent_public_trades("BTC-USDT-SWAP", limit=2)[0].taker_side == Side.SELL
    assert current_public_mark("BTC-USDT-SWAP").price == Decimal("100.012345")
    monkeypatch.setattr(
        api,
        "urlopen",
        lambda req, timeout: Response({"code": "0", "data": [trade_row()]}, target="https://www.okx.com/fallback"),
    )
    with pytest.raises(MarketDataError, match="outside qualified region"):
        recent_public_trades("BTC-USDT-SWAP")


def test_trade_taker_metadata_survives_exact_offline_journal_replay(tmp_path) -> None:
    from lab.markets.domain import AssetClass, ContractKind, Instrument
    from lab.markets.recovery import recover_offline
    from lab.markets.simulator import OfflineSimulator

    D = Decimal
    contract = Instrument(
        "SIM",
        "BTC-USDT-SWAP",
        AssetClass.CRYPTO,
        ContractKind.LINEAR_DERIVATIVE,
        "USDT",
        D("0.1"),
        D("0.001"),
        D("0.001"),
        D("5"),
    )
    path = tmp_path / "trades.db"
    with EventJournal(path) as journal:
        sim = OfflineSimulator(contract, journal=journal)
        sim.on_mark(MarkPrice("mark", 1_000_000_000, 1_000_000_000, D("100.012345")))
        sim.on_trade(Trade("taker-tick", 1_000_000_000, 1_000_000_000, D("100.0"), D("3"), Side.SELL, "0"))
        assert journal.read_all()[-1]["payload"]["taker_side"] == "sell"
        assert journal.read_all()[-1]["payload"]["venue_source"] == "0"
    with EventJournal(path) as journal:
        recovered = recover_offline(contract, journal)
        assert recovered.last_trade_price == D("100.0")
        journal.verify()


@pytest.mark.parametrize(
    "final_url",
    [
        "https://eea.okx.com:8443/api/v5/market/trades",
        "https://some-user@eea.okx.com/api/v5/market/trades",
        "https://eea.okx.com/api/v5/public/mark-price",
    ],
)
def test_trade_observations_require_exact_regional_origin_and_endpoint(monkeypatch, final_url) -> None:
    import lab.markets.okx_observations as api

    class Response(io.BytesIO):
        def geturl(self):
            return final_url

    monkeypatch.setattr(
        api,
        "urlopen",
        lambda request, timeout: Response(json.dumps({"code": "0", "data": [trade_row()]}).encode()),
    )
    with pytest.raises(MarketDataError, match="qualified region"):
        recent_public_trades("BTC-USDT-SWAP")
