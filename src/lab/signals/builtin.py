"""Hand-written signals: the baseline, classic daily hypotheses, and intraday techniques.

Intraday signals (`intraday = True`) need a quick trading speed ([trading] speed = "1h", "15m" or "5m"):
they read Window.minute and the session dates, and are refused at the daily speed."""

from __future__ import annotations

import numpy as np

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


# ----------------------------------------------------------------------------- intraday techniques


def _session(w: Window) -> np.ndarray:
    """Boolean mask of the bars in the window that belong to the current (latest) session."""
    return w.dates == w.dates[-1]


class OpeningRangeBreakout(Signal):
    name = "orb"
    intraday = True
    description = (
        "I1 opening range breakout (Crabel 1990; Zarattini & Aziz 2023): after the first `range_min` minutes, "
        "go long when a bar closes above the opening range's high, short (short=1) below its low. The stop is "
        "the range's midpoint; one trade per stock per session, flat by the close. `min_rvol` keeps only "
        "'stocks in play': opening-range volume at least that multiple of the earlier sessions' in the window."
    )
    lookback = 80  # bars: a full 5-minute session plus, at 15m and 1h, earlier sessions for relative volume
    defaults = {"range_min": 30, "short": 1, "min_rvol": 0.0}

    def __init__(self, **params):
        super().__init__(**params)
        p = self.params
        if not (5 <= p["range_min"] <= 120 and p["short"] in (0, 1) and 0 <= p["min_rvol"] <= 20):
            raise ValueError("orb: need 5 <= range_min <= 120, short 0 or 1, 0 <= min_rvol <= 20")
        self._traded: object = None  # the session date this stock already traded

    def target(self, w: Window) -> float:
        p = self.params
        today = _session(w)
        opening = today & (w.minute <= p["range_min"])
        if w.minute[-1] <= p["range_min"] or not opening.any():
            return 0.0  # the range is still forming (or the bars are too coarse to have one)
        hi, lo = w.high[opening].max(), w.low[opening].min()
        mid = (hi + lo) / 2
        c = w.close[-1]
        if self.last > 0:
            return 0.0 if c < mid else 1.0
        if self.last < 0:
            return 0.0 if c > mid else -1.0
        if self._traded == w.dates[-1]:
            return 0.0  # stopped out once today: no second attempt
        if p["min_rvol"] > 0:
            earlier = (~today) & (w.minute <= p["range_min"])
            days = np.unique(w.dates[earlier])
            if len(days) == 0:
                return 0.0
            base = w.volume[earlier].sum() / len(days)
            if base <= 0 or w.volume[opening].sum() < p["min_rvol"] * base:
                return 0.0
        if c > hi:
            self._traded = w.dates[-1]
            return 1.0
        if c < lo and p["short"]:
            self._traded = w.dates[-1]
            return -1.0
        return 0.0


class IntradayMomentum(Signal):
    name = "intraday_momentum"
    intraday = True
    description = (
        "I2 market intraday momentum (Gao, Han, Li & Zhou 2018): the return from the previous close to the "
        "first half hour predicts the last half hour. From the bar closing at `entry_min` minutes after the "
        "open (360 = 15:30), hold in the direction of that first-half-hour return when it exceeds `threshold` "
        "(short=1 allows the short side), flat otherwise and at every other time."
    )
    lookback = 80
    defaults = {"first_min": 30, "entry_min": 360, "threshold": 0.0, "short": 1}

    def __init__(self, **params):
        super().__init__(**params)
        p = self.params
        if not (15 <= p["first_min"] < p["entry_min"] < 390 and 0 <= p["threshold"] < 0.2 and p["short"] in (0, 1)):
            raise ValueError("intraday_momentum: need 15 <= first_min < entry_min < 390, 0 <= threshold < 0.2")

    def target(self, w: Window) -> float:
        p = self.params
        today = _session(w)
        if w.minute[-1] < p["entry_min"] or (~today).sum() == 0:
            return 0.0
        prev_close = w.close[~today][-1]
        first = today & (w.minute >= p["first_min"])
        if not first.any() or prev_close <= 0:
            return 0.0
        r = w.close[first][0] / prev_close - 1  # the first bar closing at or after first_min
        if r > p["threshold"]:
            return 1.0
        if r < -p["threshold"] and p["short"]:
            return -1.0
        return 0.0


class VwapReversion(Signal):
    name = "vwap_reversion"
    intraday = True
    description = (
        "I3 VWAP reversion: once `min_minutes` of the session have traded, fade a stretch away from the "
        "session's volume-weighted average price larger than `band` standard deviations of the session's own "
        "deviations (long below, short above with short=1); exit when the price is back at VWAP."
    )
    lookback = 80
    defaults = {"band": 2.0, "min_minutes": 60, "short": 1}

    def __init__(self, **params):
        super().__init__(**params)
        p = self.params
        if not (0.5 <= p["band"] <= 5 and 15 <= p["min_minutes"] <= 300 and p["short"] in (0, 1)):
            raise ValueError("vwap_reversion: need 0.5 <= band <= 5, 15 <= min_minutes <= 300, short 0 or 1")

    def target(self, w: Window) -> float:
        p = self.params
        today = _session(w)
        if w.minute[-1] < p["min_minutes"] or today.sum() < 4:
            return 0.0
        typical = (w.high[today] + w.low[today] + w.close[today]) / 3
        vol = w.volume[today]
        if vol.sum() <= 0:
            return 0.0
        vwap = np.cumsum(typical * vol) / np.cumsum(vol)
        dev = w.close[today] / vwap - 1
        scale = float(np.std(dev[:-1], ddof=1)) if len(dev) > 2 else 0.0
        d = dev[-1]
        if self.last > 0:
            return 0.0 if d >= 0 else 1.0
        if self.last < 0:
            return 0.0 if d <= 0 else -1.0
        if scale <= 0:
            return 0.0
        if d < -p["band"] * scale:
            return 1.0
        if d > p["band"] * scale and p["short"]:
            return -1.0
        return 0.0


def value_area(high, low, volume, bins: int = 50, share: float = 0.70):
    """(VAL, POC, VAH) of a volume profile built from bars: each bar's volume is spread evenly over its
    low-high range (bars carry no volume at each price, so this approximates the true profile; finer bars
    approximate it better). The value area grows from the point of control, adding the heavier neighbour,
    until it holds `share` of the volume (Steidlmayer's Market Profile convention)."""
    lo, hi = float(np.min(low)), float(np.max(high))
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo or float(np.sum(volume)) <= 0:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    profile = np.zeros(bins)
    for h, l_, v in zip(high, low, volume, strict=True):
        a = int(np.clip(np.searchsorted(edges, l_, side="right") - 1, 0, bins - 1))
        b = int(np.clip(np.searchsorted(edges, h, side="left") - 1, 0, bins - 1))
        profile[a : b + 1] += v / (b - a + 1)
    poc = int(np.argmax(profile))
    left = right = poc
    inside = profile[poc]
    total = profile.sum()
    while inside < share * total:
        down = profile[left - 1] if left > 0 else -1.0
        up = profile[right + 1] if right < bins - 1 else -1.0
        if up >= down:
            right += 1
            inside += up
        else:
            left -= 1
            inside += down
    return float(edges[left]), float((edges[poc] + edges[poc + 1]) / 2), float(edges[right + 1])


class ValueAreaReversion(Signal):
    name = "value_area"
    intraday = True
    description = (
        "I4 volume profile, failed auction (Steidlmayer's Market Profile; Dalton's '80% rule'): build the "
        "previous session's volume profile from its bars (point of control and the value area holding 70% of "
        "the volume). When today trades outside that value area and then closes back inside it for `confirm` "
        "bars, the move away was rejected: go toward the opposite edge (long when price came back from below, "
        "short from above with short=1). Exit at the opposite edge, or if price closes back outside where it "
        "came from (the stop). One trade per stock per session; flat by the close."
    )
    lookback = 160  # bars: the previous full session plus today's at 5m; many sessions of warm-up at 1h
    defaults = {"confirm": 2, "bins": 50, "short": 1}

    def __init__(self, **params):
        super().__init__(**params)
        p = self.params
        if not (1 <= p["confirm"] <= 6 and 10 <= p["bins"] <= 200 and p["short"] in (0, 1)):
            raise ValueError("value_area: need 1 <= confirm <= 6, 10 <= bins <= 200, short 0 or 1")
        self._traded = None  # the session this stock already traded

    def target(self, w: Window) -> float:
        p = self.params
        today = _session(w)
        earlier = w.dates[~today]
        if len(earlier) == 0:
            return 0.0
        prev = w.dates == earlier.max()
        va = value_area(w.high[prev], w.low[prev], w.volume[prev], p["bins"])
        if va is None:
            return 0.0
        val, _, vah = va
        c = w.close[-1]
        if self.last != 0:  # manage the open trade: target at the opposite edge, stop where it came from
            if self.last > 0:
                return 0.0 if (c >= vah or c < val) else 1.0
            return 0.0 if (c <= val or c > vah) else -1.0
        if self._traded == w.dates[-1]:
            return 0.0
        closes = w.close[today]
        if len(closes) <= p["confirm"]:
            return 0.0
        recent, before = closes[-p["confirm"] :], closes[: -p["confirm"]]
        if not np.all((recent >= val) & (recent <= vah)):
            return 0.0
        if np.any(before < val):
            self._traded = w.dates[-1]
            return 1.0
        if np.any(before > vah) and p["short"]:
            self._traded = w.dates[-1]
            return -1.0
        return 0.0


CROSS_SECTIONAL = {cls.name: cls for cls in (Momentum, MomentumLongShort)}
INTRADAY = {cls.name: cls for cls in (OpeningRangeBreakout, IntradayMomentum, VwapReversion, ValueAreaReversion)}
BUILTIN = {
    cls.name: cls for cls in (BuyAndHold, SmaTrend, RsiReversion, Momentum, MomentumLongShort, *INTRADAY.values())
}
