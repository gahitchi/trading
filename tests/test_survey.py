"""The spread survey: public quotes in, spread over a maker round trip out (no network in tests)."""

import pytest

from lab.survey import BINANCE_FAPI, BYBIT_TICKERS, survey


def binance(spreads):
    """A fake Binance API whose book tickers walk through `spreads` ({symbol: [ask per call]}), bid 100."""
    calls = {"n": 0}

    def get(url):
        if url == f"{BINANCE_FAPI}/exchangeInfo":
            return {
                "symbols": [
                    {"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"}
                    for s in ("AAAUSDT", "BBBUSDT")
                ]
                + [{"symbol": "CCCUSDT", "contractType": "CURRENT_QUARTER", "quoteAsset": "USDT", "status": "TRADING"}]
            }
        if url == f"{BINANCE_FAPI}/ticker/24hr":
            return [{"symbol": s, "quoteVolume": "50000000"} for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT")]
        if url == f"{BINANCE_FAPI}/ticker/bookTicker":
            i = calls["n"]
            calls["n"] += 1
            return [{"symbol": s, "bidPrice": "100", "askPrice": str(asks[i])} for s, asks in spreads.items()]
        raise AssertionError(url)

    return get


def test_survey_ranks_by_median_spread_over_a_maker_round_trip():
    get = binance({"AAAUSDT": [100.1, 100.1, 100.5], "BBBUSDT": [100.01] * 3, "CCCUSDT": [101] * 3})
    sleeps = []
    table = survey("binance", 3, 10.0, 2.0, get=get, sleep=sleeps.append)
    assert sleeps == [10.0, 10.0]
    assert list(table["symbol"]) == ["AAAUSDT", "BBBUSDT"]  # the quarterly contract is not a perpetual
    a = table.iloc[0]
    assert a["spread_bps"] == pytest.approx(0.1 / 100.05 * 1e4) and a["samples"] == 3
    assert a["room_bps"] == pytest.approx(a["spread_bps"] - 4.0)
    assert a["turnover_musd"] == 50.0
    assert table.iloc[1]["room_bps"] < 0  # one-tick markets leave no room for a maker


def test_survey_reads_bybit_and_skips_one_sided_or_dated_contracts():
    def get(url):
        assert url == BYBIT_TICKERS
        rows = [
            ("XUSDT", "1.000", "1.002", "30000000"),
            ("YUSDT", "", "2.0", "1"),  # no bid
            ("BTCUSDT-27DEC26", "1", "1.1", "1"),  # dated future
            ("BTCPERP", "1", "1.1", "1"),  # USDC perpetual
        ]
        return {
            "result": {
                "list": [dict(zip(("symbol", "bid1Price", "ask1Price", "turnover24h"), r, strict=True)) for r in rows]
            }
        }

    table = survey("bybit", 1, 0.0, 2.0, get=get)
    assert list(table["symbol"]) == ["XUSDT"]
    assert table.iloc[0]["spread_bps"] == pytest.approx(0.002 / 1.001 * 1e4)
