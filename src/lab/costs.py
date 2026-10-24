"""Trading cost models: commissions, spread and market impact, and order latency.

Simulated fills happen at the real opening print. What a real order pays beyond the print is charged with the
commission (Gemini audit F-R06), per fill:

  half-spread    Corwin & Schultz (2012) high-low estimator over the last `cost_window` sessions, floored at
                 `min_half_spread_bps`
  market impact  eta x daily volatility x sqrt(order shares / average daily volume) (square-root law)

Every input is known at the decision: the statistics of the sessions completed before the order. Per-share
commissions are computed on the real (unadjusted) share count when the data's split history is known (F-R07):
50,000 split-adjusted shares of a pre-split stock were 40 real ones.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
from nautilus_trader.execution import DefaultFillModel, FeeModel, StaticLatencyModel
from nautilus_trader.model import Money

from lab.config import CostSettings
from lab.universe import NY, USD


def ibkr_fixed_commission(
    shares: float, price: float, *, per_share: float = 0.005, minimum: float = 1.0, max_pct: float = 0.01
) -> float:
    """IBKR Pro Fixed: $0.005/share, at least $1.00, at most 1% of trade value."""
    return min(max(shares * per_share, minimum), max_pct * shares * price)


def corwin_schultz(high: np.ndarray, low: np.ndarray) -> np.ndarray:
    """Relative bid-ask spread per two-day window (Corwin & Schultz, 2012), negative estimates set to 0.
    Element i uses sessions i-1 and i; element 0 is NaN."""
    hl = np.log(high / low) ** 2
    beta = hl[1:] + hl[:-1]
    gamma = np.log(np.maximum(high[1:], high[:-1]) / np.minimum(low[1:], low[:-1])) ** 2
    k = 3 - 2 * math.sqrt(2)
    alpha = (np.sqrt(2 * beta) - np.sqrt(beta)) / k - np.sqrt(gamma / k)
    spread = np.maximum(0.0, 2 * (np.exp(alpha) - 1) / (1 + np.exp(alpha)))
    return np.concatenate([[np.nan], spread])


@dataclass
class MarketProfile:
    """Per-session cost inputs of one stock, each known at that session's close."""

    closes_ns: list[int]
    half_spread: np.ndarray  # fraction of price
    volatility: np.ndarray  # daily, fraction of price
    adv: np.ndarray  # average daily volume, shares

    @classmethod
    def from_bars(cls, df: pd.DataFrame, c: CostSettings) -> MarketProfile:
        """Daily or intraday bars (intraday ones are aggregated into sessions first)."""
        idx = pd.DatetimeIndex(df.index)
        if (idx != idx.normalize()).any():
            day = idx.normalize()
            df = df.groupby(day).agg({"high": "max", "low": "min", "close": "last", "volume": "sum"})
        w = c.cost_window
        rets = np.log(df["close"]).diff()
        vol = rets.rolling(w, min_periods=2).std().to_numpy()
        adv = df["volume"].rolling(w, min_periods=1).mean().to_numpy()
        hs = pd.Series(corwin_schultz(df["high"].to_numpy(float), df["low"].to_numpy(float)) / 2)
        hs = hs.rolling(w, min_periods=1).mean().to_numpy()
        floor = c.min_half_spread_bps / 1e4
        closes = (pd.DatetimeIndex(df.index).normalize() + pd.Timedelta(hours=16)).tz_localize(NY).tz_convert("UTC")
        return cls(
            closes_ns=list(closes.as_unit("ns").asi8),
            half_spread=np.where(np.isfinite(hs), np.maximum(hs, floor), floor),
            volatility=np.nan_to_num(vol, nan=0.0),
            adv=np.nan_to_num(adv, nan=0.0),
        )

    def at(self, ts: int) -> tuple[float, float, float] | None:
        """(half-spread, daily volatility, average daily volume) from the last session closed by `ts`."""
        i = bisect.bisect_right(self.closes_ns, ts) - 1
        if i < 0:
            return None
        return float(self.half_spread[i]), float(self.volatility[i]), float(self.adv[i])


def split_factor(splits: list[tuple[int, float]], ts: int) -> float:
    """Product of the split ratios effective after `ts`: real shares = adjusted shares / factor."""
    return math.prod(ratio for at, ratio in splits if at > ts)


class LabFeeModel(FeeModel):
    """Charges, per simulated fill, the IBKR Pro Fixed commission on the real share count, plus the modelled
    half-spread and market impact when `slippage` is on. The parts are tallied separately for reports.

    Note: IBKR applies the $1 minimum per order. A large order filled in several
    pieces is charged the minimum per piece here, which slightly overstates costs.
    """

    def __init__(self, c: CostSettings, profiles: dict | None = None, splits: dict | None = None):
        self.c = c
        self.profiles = profiles or {}
        self.splits = splits or {}
        self.totals = {"commission": 0.0, "spread": 0.0, "impact": 0.0}

    def costs(self, ticker: str, qty: float, px: float, ts: int) -> dict:
        factor = split_factor(self.splits.get(ticker, []), ts)
        out = {
            "commission": ibkr_fixed_commission(
                qty / factor,
                px * factor,
                per_share=self.c.commission_per_share,
                minimum=self.c.commission_min,
                max_pct=self.c.commission_max_pct,
            ),
            "spread": 0.0,
            "impact": 0.0,
        }
        profile = self.profiles.get(ticker)
        stats = profile.at(ts) if profile is not None and self.c.slippage else None
        if stats is not None:
            half_spread, vol, adv = stats
            value = qty * px
            out["spread"] = value * half_spread
            if adv > 0:
                out["impact"] = value * self.c.impact_coefficient * vol * math.sqrt(qty / adv)
        elif self.c.slippage:
            out["spread"] = qty * px * self.c.min_half_spread_bps / 1e4
        return out

    def get_commission(self, order, fill_quantity, fill_px, instrument):
        parts = self.costs(str(instrument.id.symbol), float(fill_quantity), float(fill_px), int(order.ts_init))
        for k, v in parts.items():
            self.totals[k] += v
        return Money(round(sum(parts.values()), 2), USD)


def fee_model(c: CostSettings, profiles: dict | None = None, splits: dict | None = None) -> LabFeeModel:
    return LabFeeModel(c, profiles, splits)


def fill_model(c: CostSettings) -> DefaultFillModel:
    # Fills at the print itself: the spread and impact a real order pays are charged by LabFeeModel instead of
    # one tick of slippage regardless of price, size or liquidity.
    return DefaultFillModel(prob_fill_on_limit=1.0, prob_slippage=0.0, random_seed=42)


def latency_model() -> StaticLatencyModel:
    # One second between decision and arrival at the venue: an order decided at the
    # 16:00 close meets the next market event, which is the next session's open.
    return StaticLatencyModel(base_latency_nanos=1_000_000_000)
