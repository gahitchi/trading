"""
Ticker <-> NautilusTrader instrument mapping.

Agents refer to plain tickers ("AAPL"). This module decides which instrument a
ticker becomes in each environment:

  backtest:  AAPL.SIM   every stock trades through ONE simulated broker account,
                        like a real brokerage account spanning many exchanges
  paper:     AAPL.XNAS  the real listing venue, matching Interactive Brokers
"""

from __future__ import annotations

import pandas as pd

from nautilus_trader.model import (
    AggressorSide, Bar, BarType, Currency, Equity, InstrumentId, Price, Quantity, Symbol,
    TradeId, TradeTick, Venue,
)

SIM = Venue("SIM")
USD = Currency.from_str("USD")
NY = "America/New_York"


def instrument_id(ticker: str, venue: str = "SIM") -> InstrumentId:
    return InstrumentId.from_str(f"{ticker.upper()}.{venue}")


def ticker_of(iid: InstrumentId) -> str:
    return str(iid.symbol)


def bar_type(iid: InstrumentId) -> BarType:
    return BarType.from_str(f"{iid}-1-DAY-LAST-EXTERNAL")


def equity(iid: InstrumentId) -> Equity:
    """US stock: priced in USD, one-cent tick."""
    return Equity(iid, Symbol(ticker_of(iid)), USD, 2, Price.from_str("0.01"), 0, 0)


def to_market_events(df: pd.DataFrame, iid: InstrumentId, open_print_fraction: float) -> list:
    """Turn daily OHLCV rows into the events the simulator replays:

      09:30 New York  a trade at the day's real opening price (the opening print)
      16:00 New York  the completed daily bar

    Orders decided after a close can therefore only fill at the NEXT session's open,
    which removes the classic same-bar look-ahead of daily backtests.
    """
    bt = bar_type(iid)
    # as_unit("ns"): pandas >= 3 may store microseconds, and Nautilus expects UNIX nanoseconds.
    days = pd.DatetimeIndex(df.index).as_unit("ns").tz_localize(NY)
    t_open = (days + pd.Timedelta(hours=9, minutes=30)).tz_convert("UTC").asi8
    t_close = (days + pd.Timedelta(hours=16)).tz_convert("UTC").asi8
    if len(t_close) and not (1e18 < t_close[0] < 1e19):
        raise ValueError(f"{iid}: timestamps are not UNIX nanoseconds ({t_close[0]})")
    o = df["open"].round(2).to_numpy()
    c = df["close"].round(2).to_numpy()
    h = df["high"].round(2).to_numpy()
    lo = df["low"].round(2).to_numpy()
    v = df["volume"].to_numpy()
    events = []
    sym = ticker_of(iid)
    for i in range(len(df)):
        hi = max(h[i], o[i], c[i])
        low = min(lo[i], o[i], c[i])
        size = max(1, int(v[i] * open_print_fraction))
        events.append(TradeTick(iid, Price(o[i], 2), Quantity(size, 0), AggressorSide.NO_AGGRESSOR,
                                TradeId(f"{sym}-open-{i}"), int(t_open[i]), int(t_open[i])))
        events.append(Bar(bt, Price(o[i], 2), Price(hi, 2), Price(low, 2), Price(c[i], 2),
                          Quantity(max(1, int(v[i])), 0), int(t_close[i]), int(t_close[i])))
    return events
