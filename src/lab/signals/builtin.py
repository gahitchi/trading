"""Hand-written signals: the baseline and two classic hypotheses."""

from __future__ import annotations

from lab.signals.base import Signal, Window, rsi, sma


class BuyAndHold(Signal):
    name = "buy_and_hold"
    description = "Control. Always fully invested. Every hypothesis must beat this after costs."
    lookback = 1
    defaults = {}

    def target(self, w: Window) -> float:
        return 1.0


class SmaTrend(Signal):
    name = "sma_trend"
    description = ("H1 trend following: hold the stock while its fast moving average is above "
                   "the slow one (the 50/200-day 'golden cross' regime), else stay in cash.")
    lookback = 200
    defaults = {"fast": 50, "slow": 200}

    def __init__(self, **params):
        super().__init__(**params)
        if not 1 <= self.params["fast"] < self.params["slow"] <= 250:
            raise ValueError("sma_trend: need 1 <= fast < slow <= 250")
        self.lookback = self.params["slow"]

    def target(self, w: Window) -> float:
        return 1.0 if sma(w.close, self.params["fast"]) > sma(w.close, self.params["slow"]) else 0.0


class RsiReversion(Signal):
    name = "rsi_reversion"
    description = ("H2 short-term mean reversion (Connors-style): buy when the 2-day RSI is deeply "
                   "oversold while the stock is above its 200-day average; sell once RSI recovers.")
    lookback = 200
    defaults = {"period": 2, "entry": 10, "exit": 70, "trend": 200}

    def __init__(self, **params):
        super().__init__(**params)
        p = self.params
        if not (1 <= p["period"] <= 50 and 0 < p["entry"] < p["exit"] < 100 and 1 <= p["trend"] <= 250):
            raise ValueError("rsi_reversion: invalid parameters")
        self.lookback = max(p["trend"], p["period"] * 10)

    def target(self, w: Window) -> float:
        p = self.params
        r = rsi(w.close, p["period"])
        if self.last == 0.0:
            in_uptrend = w.close[-1] > sma(w.close, p["trend"])
            return 1.0 if (in_uptrend and r < p["entry"]) else 0.0
        return 0.0 if r > p["exit"] else 1.0


BUILTIN = {cls.name: cls for cls in (BuyAndHold, SmaTrend, RsiReversion)}
