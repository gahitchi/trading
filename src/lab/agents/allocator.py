"""AllocatorAgent: combines sleeve targets into per-stock portfolio weights."""

from __future__ import annotations

import math
from collections import deque
from itertools import pairwise
from pathlib import Path

import numpy as np
from nautilus_trader.common import DataActor, DataActorConfig

from lab.agents.messages import T_MARK, T_RISK, T_SIGNAL, T_TARGETS, TargetWeights
from lab.allocation import MIN_RISK_RETURNS, risk_weights
from lab.persistence import atomic_json, read_json


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
        state_path: str | None = None,
        bank=None,
        weighting: str = "equal",
        **_kwargs,
    ):
        """sleeves: sleeve id ("signal:TICKER") -> (ticker, weight). Weights are fractions of equity.

        normalize_active: each signal's total weight is shared equally among the stocks that
        are currently in the universe (point-in-time membership), instead of a fixed 1/N.
        absolute_groups: signals whose targets are already fractions of the strategy's capital
        (cross-sectional strategies); they are scaled by the group's weight only.
        weighting: how a per-stock strategy's capital is shared among its stocks (lab.allocation): "equal",
        "inverse_vol" or "hrp", estimated from the closes this agent has seen (point in time).
        """
        super().__init__()
        self.bank = bank
        self.weighting = weighting
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
        self.state_path = state_path
        self.periods_per_year = periods_per_year  # bars per year: 252 daily, 252 x bars per session intraday


class AllocatorAgent(DataActor):
    def __init__(self, config: AllocatorAgentConfig):
        super().__init__(config)
        total = sum(w for _, w in config.sleeves.values())
        if total > 1.0 + 1e-9:
            raise ValueError(f"sleeve weights sum to {total:.3f} > 1 (leverage is set by the portfolio, not sleeves)")
        self.retired = set()
        self.selection = (None, "disabled")
        self.targets: dict[str, float] = {}
        self.active: set[str] = set()
        self.tickers = sorted({t for t, _ in config.sleeves.values()})
        self.closes = {t: deque(maxlen=config.vol_window + 1) for t in self.tickers}
        self.close_ts = dict.fromkeys(self.tickers, -1)
        if config.state_path and Path(config.state_path).exists():
            saved = read_json(config.state_path)
            if saved.get("version") != 1 or saved.get("window") != config.vol_window:
                raise ValueError("allocator state version/window changed; explicit migration required")
            self.retired = set(saved.get("retired", []))
            for t in self.tickers:
                rows = saved.get("closes", {}).get(t, [])
                for ts, price in rows:
                    if not isinstance(ts, int) or not math.isfinite(price) or price <= 0 or ts <= self.close_ts[t]:
                        raise ValueError("invalid allocator history")
                    self.closes[t].append((ts, price))
                    self.close_ts[t] = ts
        self.scale = 1.0
        self.groups: dict[str, list[str]] = {}
        for sleeve in config.sleeves:
            self.groups.setdefault(sleeve.split(":", 1)[0], []).append(sleeve)

    def on_start(self):
        self.subscribe_topic(T_MARK, self._on_mark)
        if self.config.bank is not None:
            self.subscribe_topic(T_RISK, self._on_risk)
        self.subscribe_topic(T_SIGNAL, self._on_signal)

    def weights(self) -> dict[str, float]:
        out = dict.fromkeys(self.tickers, 0.0)
        bank = self.config.bank
        if bank is not None:
            self.selection = bank.select(self.closes, self.retired)
        for group, members in self.groups.items():
            if bank is not None and group != self.selection[0]:
                continue
            if group in self.config.absolute_groups:
                group_weight = sum(self.config.sleeves[s][1] for s in members)
                for s in members:
                    out[self.config.sleeves[s][0]] += group_weight * self.targets.get(s, 0.0)
            elif self.config.weighting != "equal":
                # Risk-based shares of the group's capital among its (currently active) stocks.
                group_weight = sum(self.config.sleeves[s][1] for s in members)
                live = [s for s in members if s in self.active] if self.config.normalize_active else members
                shares = risk_weights(
                    self.config.weighting,
                    {t: list(rows) for t, rows in self.closes.items()},
                    [self.config.sleeves[s][0] for s in live],
                    MIN_RISK_RETURNS,
                )
                for s in live:
                    ticker = self.config.sleeves[s][0]
                    out[ticker] += group_weight * shares[ticker] * self.targets.get(s, 0.0)
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
        held = [t for t, w in weights.items() if w != 0]
        if not held:
            return 1.0
        # Match BOTH endpoints. A two-day return is not interchangeable with
        # somebody else's one-day return. Missing/partial warm-up stays in cash.
        returns = {}
        for t in held:
            rows = list(self.closes[t])
            returns[t] = {(a[0], b[0]): math.log(b[1] / a[1]) for a, b in pairwise(rows)}
        shared = sorted(set.intersection(*(set(returns[t]) for t in held)))
        if len(shared) < min(20, self.config.vol_window):
            return 0.0
        rets = np.array([[returns[t][k] for k in shared] for t in held])
        cov = np.atleast_2d(np.cov(rets))
        w = np.array([weights[t] for t in held])
        vol = math.sqrt(max(float(w @ cov @ w), 0.0) * self.config.periods_per_year)
        return (
            min(1.0, self.config.max_leverage)
            if vol <= 0
            else min(self.config.max_leverage, self.config.target_vol / vol)
        )

    def _save(self):
        if self.config.state_path:
            atomic_json(
                self.config.state_path,
                {
                    "version": 1,
                    "window": self.config.vol_window,
                    "retired": sorted(self.retired),
                    "selection": self.selection,
                    "closes": {t: list(rows) for t, rows in self.closes.items()},
                },
            )

    def _on_risk(self, event):
        if event.kind == "signal_failed" and event.detail in self.groups:
            self.retired.add(event.detail)
            self._save()
            self.publish_message(T_TARGETS, TargetWeights(self.weights(), event.ts))

    def _on_signal(self, msg):
        if msg.sleeve not in self.config.sleeves:
            return
        self._on_mark(msg)
        self.targets[msg.sleeve] = msg.target
        if msg.active:
            self.active.add(msg.sleeve)
        else:
            self.active.discard(msg.sleeve)
        self.publish_message(T_TARGETS, TargetWeights(self.weights(), msg.ts))

    def _on_mark(self, msg):
        if msg.ticker in self.closes and msg.ts > self.close_ts[msg.ticker]:
            if not math.isfinite(msg.price) or msg.price <= 0:
                raise ValueError("allocator requires a finite positive mark")
            self.close_ts[msg.ticker] = msg.ts
            self.closes[msg.ticker].append((msg.ts, msg.price))
            self._save()
