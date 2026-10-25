"""Bounded provider pagination must never silently duplicate history or invent bars."""

from __future__ import annotations

import pytest

from lab.markets.okx_public import MarketDataError, historical_confirmed_candles


def row(minute: int) -> list[str]:
    return [
        str(minute * 60_000),
        "100",
        "101",
        "99",
        "100",
        "1",
        "100",
        "100",
        "1",
    ]


def response(*minutes: int) -> dict:
    return {"code": "0", "data": [row(m) for m in minutes]}


def test_historical_provider_pages_are_strictly_older_and_sorted(monkeypatch) -> None:
    import lab.markets.okx_observations as api

    calls = []

    def fake_fetch(path, params):
        assert path == "/api/v5/market/history-candles"
        assert params["bar"] == "1m"
        assert params["limit"] == 2
        calls.append(dict(params))
        return (
            response(5, 4) if "after" not in params else response(3, 2),
            1_000_000_000_000,
        )

    monkeypatch.setattr(api, "_fetch", fake_fetch)
    monkeypatch.setattr("time.sleep", lambda _: None)
    bars = historical_confirmed_candles("BTC-USDT", pages=2, limit=2)
    assert [b.start_ns // 60_000_000_000 for b in bars] == [2, 3, 4, 5]
    assert calls[1]["after"] == str(4 * 60_000)


def test_historical_overlap_or_stuck_page_is_refused(monkeypatch) -> None:
    import lab.markets.okx_observations as api

    monkeypatch.setattr(api, "_fetch", lambda path, params: (response(5, 4), 1_000_000_000_000))
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(MarketDataError, match="older cursor"):
        historical_confirmed_candles("BTC-USDT", pages=2, limit=2)


def test_gaps_remain_visible_in_historical_rows(monkeypatch) -> None:
    import lab.markets.okx_observations as api

    monkeypatch.setattr(api, "_fetch", lambda path, params: (response(7, 2), 1_000_000_000_000))
    bars = historical_confirmed_candles("BTC-USDT", pages=2, limit=100)
    assert len(bars) == 2
    assert bars[1].start_ns - bars[0].start_ns == 5 * 60_000_000_000


@pytest.mark.parametrize(
    ("pages", "limit"),
    [(0, 100), (51, 100), (True, 100), (2, 0), (2, 101), (2, False)],
)
def test_pagination_bounds_fail_closed_before_http(pages, limit) -> None:
    with pytest.raises(MarketDataError):
        historical_confirmed_candles("BTC-USDT", pages=pages, limit=limit)
