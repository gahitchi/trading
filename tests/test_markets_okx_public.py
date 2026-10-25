"""Read-only EEA public feed must not smuggle unconfirmed or malformed data."""

from __future__ import annotations

import io
import json
from decimal import Decimal

import pytest

from lab.markets.okx_public import MarketDataError, latest_confirmed_candles, parse_confirmed_candles


def row(ts: str, *, complete: str = "1") -> list[str]:
    return [ts, "100", "105", "95", "102", "10", "1000", "1000", complete]


def test_only_completed_candles_are_allowed() -> None:
    payload = {"code": "0", "data": [row("120000", complete="0"), row("60000")]}
    data = parse_confirmed_candles(payload, "BTC-USDT")
    assert len(data) == 1
    assert data[0].start_ns == 60_000_000_000
    assert data[0].high == Decimal("105")


def test_duplicate_and_bad_ohlc_are_rejected() -> None:
    with pytest.raises(MarketDataError, match="duplicate"):
        parse_confirmed_candles({"code": "0", "data": [row("60000"), row("60000")]}, "BTC-USDT")
    corrupt = row("60000")
    corrupt[2] = "99"
    with pytest.raises(MarketDataError, match="OHLC"):
        parse_confirmed_candles({"code": "0", "data": [corrupt]}, "BTC-USDT")
    with pytest.raises(MarketDataError, match="request failed"):
        parse_confirmed_candles({"code": "50011", "data": []}, "BTC-USDT")


def test_public_fetch_pins_eea_host_and_excludes_unconfirmed(monkeypatch) -> None:
    import lab.markets.okx_public as okx

    def fake_urlopen(request, timeout):
        assert request.full_url.startswith("https://eea.okx.com/api/v5/market/candles?")
        assert "bar=1m" in request.full_url
        assert "instId=BTC-USDT" in request.full_url
        assert timeout == 10
        return io.BytesIO(json.dumps({"code": "0", "data": [row("60000"), row("120000", complete="0")]}).encode())

    monkeypatch.setattr(okx, "urlopen", fake_urlopen)
    assert len(latest_confirmed_candles("BTC-USDT")) == 1
    with pytest.raises(MarketDataError, match="canonical"):
        latest_confirmed_candles("https://evil.example")
    with pytest.raises(MarketDataError, match="limit"):
        latest_confirmed_candles("BTC-USDT", limit=301)


def test_candle_rejects_nonfinite_fields_and_provider_schema_drift() -> None:
    from decimal import Decimal

    from lab.markets.okx_public import PublicCandle

    for field in ("open", "high", "low", "close", "volume_native"):
        values = {
            "open": Decimal("100"),
            "high": Decimal("105"),
            "low": Decimal("95"),
            "close": Decimal("102"),
            "volume_native": Decimal("10"),
        }
        values[field] = Decimal("NaN")
        with pytest.raises(MarketDataError, match="nonfinite"):
            PublicCandle("BTC-USDT", 60_000_000_000, 120_000_000_000, **values)
    with pytest.raises(MarketDataError, match="schema"):
        parse_confirmed_candles({"code": "0", "data": [[*row("60000"), "unexpected-field"]]}, "BTC-USDT")


@pytest.mark.parametrize(
    "timestamp",
    [60000.9, True, "+60000", "60000.0", " 60000", "1" * 24],
)
def test_provider_timestamp_never_coerces_to_integer(timestamp) -> None:
    with pytest.raises(MarketDataError, match="timestamp"):
        parse_confirmed_candles({"code": "0", "data": [row(timestamp)]}, "BTC-USDT")


@pytest.mark.parametrize(
    ("field", "corruption"),
    [
        (1, 100.0),
        (5, True),
        (6, "NaN"),
        (6, "-1"),
        (7, "Infinity"),
        (7, "-0.01"),
    ],
)
def test_entire_candle_numeric_schema_is_exact_and_finite(field, corruption) -> None:
    corrupt = row("60000")
    corrupt[field] = corruption
    with pytest.raises(MarketDataError):
        parse_confirmed_candles({"code": "0", "data": [corrupt]}, "BTC-USDT")


@pytest.mark.parametrize(
    "final_url",
    [
        "https://www.okx.com/api/v5/market/candles",
        "http://eea.okx.com/api/v5/market/candles",
        "https://eea.okx.com:8443/api/v5/market/candles",
        "https://attacker@eea.okx.com/api/v5/market/candles",
        "https://eea.okx.com/api/v5/market/trades",
    ],
)
def test_candle_response_redirect_cannot_steal_eea_provenance(monkeypatch, final_url) -> None:
    import lab.markets.okx_public as api

    class Response(io.BytesIO):
        def geturl(self):
            return final_url

    def fake_urlopen(request, timeout):
        assert request.full_url.startswith("https://eea.okx.com/")
        return Response(json.dumps({"code": "0", "data": [row("60000")]}).encode())

    monkeypatch.setattr(api, "urlopen", fake_urlopen)
    with pytest.raises(MarketDataError, match="qualified origin"):
        latest_confirmed_candles("BTC-USDT")
