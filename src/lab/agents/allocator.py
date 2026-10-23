"""AllocatorAgent: combines sleeve targets into per-stock portfolio weights."""

from __future__ import annotations

import math
from collections import deque

import numpy as np
from nautilus_trader.common import DataActor, DataActorConfig

from lab.agents.messages import T_SIGNAL, T_TARGETS, TargetWeights


class AllocatorAgentConfig(DataActorConfig):
    def __init__(
        self,
        *,
        sleeves: dict,
        normalize_active: bool = False,
        absolute_groups=(),
        target_vol: float = 0.0,
        vol_window: int = 63,
        max_leverage: float = 1.0,
        leverage: float = 1.0,
        periods_per_year: float = 252.0,
        **_kwargs,
    ):
        """sleeves: sleeve id ("signal:TICKER") -> (ticker, weight). Weights are fractions of equity.

        normalize_active: each signal's total weight is shared equally among the stocks that
        are currently in the universe (point-in-time membership), instead of a fixed 1/N.
        absolute_groups: signals whose targets are already fractions of the strategy's capital
        (cross-sectional strategies); they are scaled by the group's weight only.
        """
        super().__init__()
        self.sleeves = sleeves
        self.normalize_active = normalize_active
        self.absolute_groups = set(absolute_groups)
        # Volatility targeting: scale all weights by min(max_leverage, target_vol / ex-ante portfolio vol),
        # estimated from the stocks' last `vol_window` bar returns (shorts included). 0 = off, and then
        # the weights are multiplied by the fixed `leverage`. The RiskAgent's limits apply afterwards.
        self.target_vol = target_vol
        self.vol_window = vol_window
        self.max_leverage = max_leverage
        self.leverage = leverage
        self.periods_per_year = periods_per_year  # bars per year: 252 daily, 252 x bars per session intraday


class AllocatorAgent(DataActor):
    def __init__(self, config: AllocatorAgentConfig):
        super().__init__(config)
        total = sum(w for _, w in config.sleeves.values())
        if total > 1.0 + 1e-9:
            raise ValueError(f"sleeve weights sum to {total:.3f} > 1 (leverage is set by the portfolio, not sleeves)")
        self.targets: dict[str, float] = {}
        self.active: set[str] = set()
        self.tickers = sorted({t for t, _ in config.sleeves.values()})
        self.closes = {t: deque(maxlen=config.vol_window + 1) for t in self.tickers}
        self.close_ts = dict.fromkeys(self.tickers, -1)
        self.scale = 1.0
        self.groups: dict[str, list[str]] = {}
        for sleeve in config.sleeves:
            self.groups.setdefault(sleeve.split(":", 1)[0], []).append(sleeve)

    def on_start(self):
        self.subscribe_topic(T_SIGNAL, self._on_signal)

    def weights(self) -> dict[str, float]:
        out = dict.fromkeys(self.tickers, 0.0)
        for group, members in self.groups.items():
            if group in self.config.absolute_groups:
                group_weight = sum(self.config.sleeves[s][1] for s in members)
                for s in members:
                    out[self.config.sleeves[s][0]] += group_weight * self.targets.get(s, 0.0)
            elif self.config.normalize_active:
                group_weight = sum(self.config.sleeves[s][1] for s in members)
                live = [s for s in members if s in self.active]
                for s in live:
                    out[self.config.sleeves[s][0]] += group_weight / len(live) * self.targets.get(s, 0.0)
            else:
                for s in members:
                    ticker, w = self.config.sleeves[s]
                    out[ticker] += w * self.targets.get(s, 0.0)
        if self.config.target_vol > 0:
            self.scale = self._vol_scale(out)
        else:
            self.scale = self.config.leverage
        if self.scale != 1.0:
            out = {t: w * self.scale for t, w in out.items()}
        return out

    def _vol_scale(self, weights: dict[str, float]) -> float:
        held = [t for t, w in weights.items() if w != 0 and len(self.closes[t]) > 20]
        if not held:
            return 1.0
        n = min(len(self.closes[t]) for t in held)
        prices = np.array([list(self.closes[t])[-n:] for t in held], dtype=float)
        rets = np.diff(np.log(prices), axis=1)
        cov = np.atleast_2d(np.cov(rets))
        w = np.array([weights[t] for t in held])
        vol = math.sqrt(max(float(w @ cov @ w), 0.0) * self.config.periods_per_year)
        return 1.0 if vol <= 0 else min(self.config.max_leverage, self.config.target_vol / vol)

    def _on_signal(self, msg):
        if msg.sleeve not in self.config.sleeves:
            return
        if msg.ts > self.close_ts[msg.ticker]:
            self.close_ts[msg.ticker] = msg.ts
            self.closes[msg.ticker].append(msg.price)
        self.targets[msg.sleeve] = msg.target
        if msg.active:
            self.active.add(msg.sleeve)
        else:
            self.active.discard(msg.sleeve)
        self.publish_message(T_TARGETS, TargetWeights(self.weights(), msg.ts))
