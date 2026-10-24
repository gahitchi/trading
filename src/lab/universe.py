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
    AggressorSide,
    Bar,
    BarType,
    Currency,
    Equity,
    InstrumentId,
    Price,
    Quantity,
    Symbol,
    TradeId,
    TradeTick,
    Venue,
)

SIM = Venue("SIM")
USD = Currency.from_str("USD")
NY = "America/New_York"


def instrument_id(ticker: str, venue: str = "SIM") -> InstrumentId:
    return InstrumentId.from_str(f"{ticker.upper()}.{venue}")


def ticker_of(iid: InstrumentId) -> str:
    return str(iid.symbol)


def bar_type(iid: InstrumentId, spec: str = "1-DAY") -> BarType:
    """spec: "1-DAY", "1-HOUR", "15-MINUTE" or "5-MINUTE" (lab.intraday.Speed.bar_spec())."""
    return BarType.from_str(f"{iid}-{spec}-LAST-EXTERNAL")


# Simulated prices keep four decimals (Gemini audit F-R07): split-adjusted histories put pre-split prices at a
# few dollars or cents, where a one-cent grid moves a price by whole percents. Live instruments keep the
# broker's cent tick.
SIM_PRICE_PRECISION = 4


def equity(iid: InstrumentId, precision: int = 2) -> Equity:
    """US stock priced in USD: a one-cent tick, or a finer one for simulated (adjusted) prices."""
    return Equity(iid, Symbol(ticker_of(iid)), USD, precision, Price(10.0**-precision, precision), 0, 0)


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
    d = SIM_PRICE_PRECISION
    o = df["open"].round(d).to_numpy()
    c = df["close"].round(d).to_numpy()
    h = df["high"].round(d).to_numpy()
    lo = df["low"].round(d).to_numpy()
    v = df["volume"].to_numpy()
    events = []
    sym = ticker_of(iid)
    for i in range(len(df)):
        hi = max(h[i], o[i], c[i])
        low = min(lo[i], o[i], c[i])
        size = int(v[i] * open_print_fraction)
        if size > 0:
            events.append(
                TradeTick(
                    iid,
                    Price(o[i], d),
                    Quantity(size, 0),
                    AggressorSide.NO_AGGRESSOR,
                    TradeId(f"{sym}-open-{i}"),
                    int(t_open[i]),
                    int(t_open[i]),
                )
            )
        events.append(
            Bar(
                bt,
                Price(o[i], d),
                Price(hi, d),
                Price(low, d),
                Price(c[i], d),
                Quantity(int(v[i]), 0),
                int(t_close[i]),
                int(t_close[i]),
            )
        )
    return events


OPEN_PRINT_LAG_NS = 5_000_000_000  # an intraday bar's opening print, 5 s after its start (see below)


def to_intraday_events(
    df: pd.DataFrame, iid: InstrumentId, open_print_fraction: float, minutes: int, spec: str, delay_bars: int
) -> list:
    """Intraday bars (indexed by bar START, New York, regular hours) as replay events:

      start + 5 s           a trade at the bar's real opening price, the only price orders fill at
      end + delay_bars bars the completed bar reaches the agents: ts_event is its real close, ts_init the
                            moment it becomes known (Nautilus replays by ts_init)

    With delayed market data (15 minutes for IBKR paper without a subscription), a decision on the bar that
    closed at 10:00 is made at 10:15 and fills at the opening print of the 10:15 bar, never at a price the
    agents could not have traded at. The delay is rounded UP to whole bars (1h bars with 15 min delay: one
    hour), which changes the fill scenario and is not guaranteed to be conservative. Orders decided after the last opening print of a session fill at the next
    session's first one.
    """
    from lab.intraday import bar_end

    bt = bar_type(iid, spec)
    starts = pd.DatetimeIndex(df.index).as_unit("ns")
    ends = pd.DatetimeIndex([bar_end(t, minutes) for t in starts]).as_unit("ns")
    seen = ends + pd.Timedelta(minutes=minutes * delay_bars)

    def ns(idx):
        return idx.tz_localize(NY).tz_convert("UTC").asi8

    t_open, t_end, t_seen = ns(starts) + OPEN_PRINT_LAG_NS, ns(ends), ns(seen)
    if len(t_end) and not (1e18 < t_end[0] < 1e19):
        raise ValueError(f"{iid}: timestamps are not UNIX nanoseconds ({t_end[0]})")
    d = SIM_PRICE_PRECISION
    o, h, lo, c = (df[k].round(d).to_numpy() for k in ("open", "high", "low", "close"))
    v = df["volume"].to_numpy()
    sym = ticker_of(iid)
    events = []
    for i in range(len(df)):
        hi, low = max(h[i], o[i], c[i]), min(lo[i], o[i], c[i])
        size = int(v[i] * open_print_fraction)
        if size > 0:
            events.append(
                TradeTick(
                    iid,
                    Price(o[i], d),
                    Quantity(size, 0),
                    AggressorSide.NO_AGGRESSOR,
                    TradeId(f"{sym}-open-{i}"),
                    int(t_open[i]),
                    int(t_open[i]),
                )
            )
        events.append(
            Bar(
                bt,
                Price(o[i], d),
                Price(hi, d),
                Price(low, d),
                Price(c[i], d),
                Quantity(int(v[i]), 0),
                int(t_end[i]),
                int(t_seen[i]),
            )
        )
    return events
