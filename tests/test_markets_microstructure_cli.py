"""Microstructure CLI remains read-only and does not pretend cross-feed synchronization."""

from __future__ import annotations

import json
import sys
from decimal import Decimal

from lab.markets.okx_microstructure import BookLevel, BookSnapshot, FundingIndication


def test_microstructure_cli_reports_real_source_separately_and_no_orders(monkeypatch, capsys) -> None:
    import lab.markets.__main__ as cli
    import lab.markets.okx_microstructure as api

    d = Decimal
    book = BookSnapshot(
        "BTC-USDT-SWAP",
        1_000_000_000,
        2_000_000_000,
        102,
        (BookLevel(d("100.0"), d("1"), 2),),
        (BookLevel(d("100.2"), d("2"), 3),),
    )
    funding = FundingIndication(
        "BTC-USDT-SWAP",
        1_500_000_000,
        2_100_000_000,
        d("0.0002"),
        9_000_000_000,
        12_000_000_000,
    )
    monkeypatch.setattr(api, "current_public_book", lambda inst, *, depth: book)
    monkeypatch.setattr(api, "current_public_funding", lambda inst: funding)
    monkeypatch.setattr(sys, "argv", ["markets", "microstructure", "--instrument", "BTC-USDT-SWAP"])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["book"]["best_bid"] == "100.0"
    assert result["book"]["best_ask"] == "100.2"
    assert Decimal(result["book"]["spread_bps"]) > 0
    assert result["funding"]["indicated_rate"] == "0.0002"
    assert result["funding"]["realized_payment_qualified"] is False
    assert result["feeds_synchronized"] is False
    assert result["snapshot_tradable"] is False
    assert result["orders_routed"] is False
    assert result["live_trading_enabled"] is False


def test_crossed_book_cli_reports_no_valid_spread(monkeypatch, capsys) -> None:
    import lab.markets.__main__ as cli
    import lab.markets.okx_microstructure as api

    d = Decimal
    book = BookSnapshot(
        "BTC-USDT-SWAP",
        1,
        2,
        3,
        (BookLevel(d("100.2"), d("1"), 1),),
        (BookLevel(d("100.1"), d("1"), 1),),
    )
    funding = FundingIndication("BTC-USDT-SWAP", 1, 2, d("0"), 1000, 2000)
    monkeypatch.setattr(api, "current_public_book", lambda inst, *, depth: book)
    monkeypatch.setattr(api, "current_public_funding", lambda inst: funding)
    monkeypatch.setattr(sys, "argv", ["markets", "microstructure"])
    cli.main()
    data = json.loads(capsys.readouterr().out)
    assert data["book"]["spread"] is None
    assert data["book"]["spread_bps"] is None
    assert data["book"]["crossed_or_locked"] is True
