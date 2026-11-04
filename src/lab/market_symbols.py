"""Nautilus-independent market names and symbol validation constants.

Keep public research command-line parsing portable. Recorder-specific
NautilusTrader imports belong only in the actual recording implementation.
"""

from __future__ import annotations

import re

BINANCE = "BINANCE"
BYBIT = "BYBIT"

# NautilusTrader instrument ID suffixes for supported USDT perpetual venues.
PERPETUAL = {BINANCE: "PERP", BYBIT: "LINEAR"}
VENUES = tuple(v.lower() for v in PERPETUAL)
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
SYMBOL_RE = re.compile(r"[A-Z0-9]{2,20}")
