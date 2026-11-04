"""Shared market constants do not require the native NautilusTrader runtime."""

from __future__ import annotations

import builtins

from lab.market_symbols import DEFAULT_SYMBOLS, PERPETUAL, SYMBOL_RE, VENUES


def test_cli_parses_without_importing_native_recorder(monkeypatch):
    # Intercept even if the heavy module happens to be available on CI.
    original_import = builtins.__import__

    def reject_native(name, *args, **kwargs):
        if name == "lab.record" or name.startswith("nautilus_trader"):
            raise AssertionError(f"CLI parser attempted a native import: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_native)
    from lab.cli import build_parser

    parser = build_parser()
    record = parser.parse_args(["record"])
    assert record.venue == "binance"
    assert record.symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    crash = parser.parse_args(["crash", "study", "--mode", "breakdown"])
    assert crash.mode == "breakdown"
    carry = parser.parse_args(["carry", "fetch"])
    assert carry.venues is None


def test_venue_and_perpetual_suffix_defaults_preserved():
    assert VENUES == ("binance", "bybit")
    assert PERPETUAL == {"BINANCE": "PERP", "BYBIT": "LINEAR"}
    assert DEFAULT_SYMBOLS == ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def test_full_symbol_validation_does_not_accept_suffixes():
    assert SYMBOL_RE.fullmatch("BTCUSDT")
    assert SYMBOL_RE.fullmatch("ETHUSDT")
    assert not SYMBOL_RE.fullmatch("BTCUSDT-PERP")
    assert not SYMBOL_RE.fullmatch("../../etc/passwd")
    assert not SYMBOL_RE.fullmatch("btcusdt")
