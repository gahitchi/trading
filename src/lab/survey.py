"""
Which USDT perpetuals have a quoted spread wide enough for a maker to earn more than the fees?

Samples every perpetual's best bid and ask from the venue's public REST API a number of times and reports, per
symbol, the median spread and its room over a maker round trip (2 x maker fee), with the 24 h turnover so thin
markets can be left out. Wider than the fees is necessary, not sufficient: a passive fill also pays adverse
selection, which `lab maker-study` measures on recorded data. Public data only, no keys.
"""

from __future__ import annotations

import json
import time
import urllib.request

import pandas as pd

from lab.record import _SYMBOL

BINANCE_FAPI = "https://fapi.binance.com/fapi/v1"
BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers?category=linear"


def get_json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "trading-lab"}), timeout=15) as r:
        return json.load(r)


def binance_perpetuals(get=get_json) -> set[str]:
    info = get(f"{BINANCE_FAPI}/exchangeInfo")
    return {
        s["symbol"]
        for s in info["symbols"]
        if s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
    }


def quotes(venue: str, get=get_json, symbols: set[str] | None = None) -> dict[str, tuple[float, float, float]]:
    """{symbol: (bid, ask, 24 h turnover in USDT)} for USDT perpetuals with a valid two-sided quote."""
    if venue == "binance":
        turnover = {t["symbol"]: float(t["quoteVolume"]) for t in get(f"{BINANCE_FAPI}/ticker/24hr")}
        rows = (
            (b["symbol"], b["bidPrice"], b["askPrice"], turnover.get(b["symbol"], 0.0))
            for b in get(f"{BINANCE_FAPI}/ticker/bookTicker")
        )
    elif venue == "bybit":
        rows = (
            (t["symbol"], t["bid1Price"], t["ask1Price"], float(t["turnover24h"] or 0))
            for t in get(BYBIT_TICKERS)["result"]["list"]
        )
    else:
        raise ValueError(f"unknown venue: {venue!r}")
    out = {}
    for sym, bid, ask, vol in rows:
        if not sym.endswith("USDT") or not _SYMBOL.fullmatch(sym) or (symbols is not None and sym not in symbols):
            continue
        bid, ask = float(bid or 0), float(ask or 0)
        if 0 < bid < ask:
            out[sym] = (bid, ask, vol)
    return out


def survey(
    venue: str, samples: int, interval_s: float, maker_fee_bps: float, get=get_json, sleep=time.sleep
) -> pd.DataFrame:
    """One row per symbol: median and 90th-percentile spread (bps of mid), its room over a maker round trip, the
    24 h turnover (latest sample) and how many samples quoted it; widest room first."""
    if samples < 1:
        raise ValueError("samples must be at least 1")
    symbols = binance_perpetuals(get) if venue == "binance" else None
    rows = []
    for i in range(samples):
        if i:
            sleep(interval_s)
        for sym, (bid, ask, vol) in quotes(venue, get, symbols).items():
            rows.append((sym, (ask - bid) / ((ask + bid) / 2) * 10_000, vol))
    cols = ["symbol", "spread_bps", "p90_spread_bps", "room_bps", "turnover_musd", "samples"]
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows, columns=["symbol", "spread", "vol"])
    g = df.groupby("symbol")
    out = pd.DataFrame(
        {
            "spread_bps": g["spread"].median(),
            "p90_spread_bps": g["spread"].quantile(0.9),
            "turnover_musd": g["vol"].last() / 1e6,
            "samples": g["spread"].size(),
        }
    )
    out["room_bps"] = out["spread_bps"] - 2 * maker_fee_bps
    return out.reset_index()[cols].sort_values("room_bps", ascending=False, ignore_index=True)
