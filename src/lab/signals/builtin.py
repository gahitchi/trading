"""Hand-written signals: the baseline and two classic hypotheses."""

from __future__ import annotations

from lab.signals.base import CrossSectionalSignal, Signal, Window, rsi, sma


class BuyAndHold(Signal):
    name = "buy_and_hold"
    description = "Control. Always fully invested. Every hypothesis must beat this after costs."
    lookback = 1
    defaults = {}

    def target(self, w: Window) -> float:
        return 1.0


class SmaTrend(Signal):
    name = "sma_trend"
    description = (
        "H1 trend following: hold the stock while its fast moving average is above "
        "the slow one (the 50/200-day 'golden cross' regime), else stay in cash, or with short=1 "
        "go short (the long/short version of time-series momentum, Moskowitz, Ooi & Pedersen 2012)."
    )
    lookback = 200
    defaults = {"fast": 50, "slow": 200, "short": 0}

    def __init__(self, **params):
        super().__init__(**params)
        if not 1 <= self.params["fast"] < self.params["slow"] <= 250:
            raise ValueError("sma_trend: need 1 <= fast < slow <= 250")
        if self.params["short"] not in (0, 1):
            raise ValueError("sma_trend: short must be 0 or 1")
        self.lookback = self.params["slow"]

    def label(self) -> str:
        # The long-only version keeps its historical label (reports and trial registries key on it).
        p = {k: v for k, v in self.params.items() if not (k == "short" and v == 0)}
        return f"{self.name}(" + ",".join(f"{k}={v}" for k, v in sorted(p.items())) + ")"

    def target(self, w: Window) -> float:
        if sma(w.close, self.params["fast"]) > sma(w.close, self.params["slow"]):
            return 1.0
        return -1.0 if self.params["short"] else 0.0


class RsiReversion(Signal):
    name = "rsi_reversion"
    description = (
        "H2 short-term mean reversion (Connors-style): buy when the 2-day RSI is deeply "
        "oversold while the stock is above its 200-day average; sell once RSI recovers."
    )
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


class Momentum(CrossSectionalSignal):
    name = "momentum"
    description = (
        "H3 cross-sectional momentum (Jegadeesh & Titman 1993): hold, in equal weights, the `top` stocks "
        "with the best return over the past 12 months excluding the most recent month; only stocks with "
        "a positive past return qualify, otherwise that share stays in cash."
    )
    lookback = 253
    defaults = {"top": 3, "months": 12, "skip_months": 1}

    def __init__(self, **params):
        super().__init__(**params)
        p = self.params
        if not (p["top"] >= 1 and 1 <= p["months"] <= 24 and 0 <= p["skip_months"] < p["months"]):
            raise ValueError("momentum: invalid parameters")
        self.lookback = 21 * p["months"] + 1

    def weights(self, windows):
        p = self.params
        skip = 21 * p["skip_months"]
        scores = {}
        for t, w in windows.items():
            past, recent = w.close[0], w.close[-1 - skip]
            if past > 0:
                scores[t] = recent / past - 1
        winners = [t for t, s in sorted(scores.items(), key=lambda kv: -kv[1]) if s > 0][: p["top"]]
        return {t: 1.0 / p["top"] for t in winners}


class MomentumLongShort(CrossSectionalSignal):
    name = "momentum_ls"
    description = (
        "H4 cross-sectional momentum as a dollar-neutral long/short book (Jegadeesh & Titman 1993): long the "
        "`top` best and short the `bottom` worst stocks by 12-month return excluding the last month, half the "
        "strategy's capital on each side. Needs top + bottom eligible stocks, else stays in cash."
    )
    lookback = 253
    defaults = {"top": 3, "bottom": 3, "months": 12, "skip_months": 1}

    def __init__(self, **params):
        super().__init__(**params)
        p = self.params
        if not (p["top"] >= 1 and p["bottom"] >= 1 and 1 <= p["months"] <= 24 and 0 <= p["skip_months"] < p["months"]):
            raise ValueError("momentum_ls: invalid parameters")
        self.lookback = 21 * p["months"] + 1

    def weights(self, windows):
        p = self.params
        skip = 21 * p["skip_months"]
        scores = {t: w.close[-1 - skip] / w.close[0] - 1 for t, w in windows.items() if w.close[0] > 0}
        if len(scores) < p["top"] + p["bottom"]:
            return {}
        ranked = sorted(scores, key=lambda t: (-scores[t], t))
        longs, shorts = ranked[: p["top"]], ranked[-p["bottom"] :]
        return {**{t: 0.5 / p["top"] for t in longs}, **{t: -0.5 / p["bottom"] for t in shorts}}


CROSS_SECTIONAL = {cls.name: cls for cls in (Momentum, MomentumLongShort)}
BUILTIN = {cls.name: cls for cls in (BuyAndHold, SmaTrend, RsiReversion, Momentum, MomentumLongShort)}
