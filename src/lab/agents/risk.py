"""RiskAgent: enforces position, exposure and drawdown limits on every target."""

from __future__ import annotations

import math
from pathlib import Path

from nautilus_trader.common import DataActor, DataActorConfig

from lab import margin as mg
from lab.agents.messages import T_APPROVED, T_EQUITY, T_RISK, T_TARGETS, ApprovedTargets, RiskEvent
from lab.config import MarginSettings
from lab.halt import halt_kinds, read_halt, set_halt
from lab.persistence import atomic_json, read_json


class RiskAgentConfig(DataActorConfig):
    def __init__(
        self,
        *,
        max_weight: float,
        max_gross: float,
        max_drawdown: float,
        max_short_gross: float = 0.0,
        min_net: float = 0.0,
        max_net: float = 1.0,
        margin: MarginSettings | None = None,
        state_path: str | None = None,
        halt_path: str | None = None,
        **_kwargs,
    ):
        """state_path: where the equity peak and breaker state persist (paper); halt_path: the hard-halt file."""
        super().__init__()
        self.max_weight = max_weight
        self.max_gross = max_gross
        self.max_drawdown = max_drawdown
        self.max_short_gross = max_short_gross
        self.min_net = min_net
        self.max_net = max_net
        self.margin = margin or MarginSettings()
        self.state_path = state_path
        self.halt_path = halt_path


class RiskAgent(DataActor):
    """Nothing reaches the ExecutionAgent without passing through here.

    Weights are signed (negative = short), as fractions of equity. In order:
    - each stock is capped at max_weight in absolute value; shorts are removed if max_short_gross is 0
    - the short book is scaled down to max_short_gross, then total |exposure| to max_gross
    - net exposure is brought into [min_net, max_net] by trimming the side in excess
    - the broker's initial margin for the targets must fit in equity with the cushion
    - margin: when equity nears the maintenance requirement of the current book, every target is scaled
      down (deleverage, by itself). Below maintenance itself it is a hard halt (margin_call): exposure
      may only shrink until an operator clears it
    - circuit breaker: once equity falls max_drawdown below its peak, everything is
      flattened and trading stays halted. In paper mode this is a hard halt: it survives
      restarts (the peak and the halt persist) and only `lab recover` clears it, after
      which the peak restarts from the current equity.
    """

    def __init__(self, config: RiskAgentConfig):
        super().__init__(config)
        self.peak = 0.0
        self.halted = False
        self.last_weights: dict = {}
        self.adjustments = 0
        self.delever = 1.0  # < 1 while equity is too close to the maintenance requirement
        self.margin_called = False  # equity fell below maintenance: exposure may only shrink
        if config.state_path and Path(config.state_path).exists():
            state = read_json(config.state_path)
            self.peak = float(state.get("peak", 0.0))
            if not math.isfinite(self.peak) or self.peak < 0:
                raise ValueError("invalid persisted risk peak")
            self.halted = bool(state.get("halted", False))
            self.margin_called = bool(state.get("margin_called", False))

    def _save(self):
        if self.config.state_path:
            p = Path(self.config.state_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(p, {"peak": self.peak, "halted": self.halted, "margin_called": self.margin_called})

    def _breaker_cleared(self) -> bool:
        """True once an operator has cleared the breaker's hard halt (lab recover)."""
        return self._cleared("circuit_breaker")

    def _cleared(self, kind: str) -> bool:
        """Paper: True once `lab recover` removed this kind of hard halt. Backtest (no halt file): never."""
        if not self.config.halt_path:
            return False
        halt = read_halt(self.config.halt_path)
        return kind not in halt_kinds(halt)

    def on_start(self):
        self.subscribe_topic(T_TARGETS, self._on_targets)
        self.subscribe_topic(T_EQUITY, self._on_equity)

    def _approve(self, weights: dict, ts: int) -> ApprovedTargets:
        if self.halted:
            return ApprovedTargets(dict.fromkeys(weights, 0.0), ts, halted=True, notes=("halted",))
        c = self.config
        notes = []
        out = {}
        for t, w in weights.items():
            try:
                w = float(w)
            except (ValueError, TypeError):
                w = float("nan")
            if not math.isfinite(w):
                notes.append(f"{t}: invalid target; set to zero")
                w = 0.0
            if w < 0 and c.max_short_gross <= 0:
                w = 0.0  # long-only configuration
            if abs(w) > c.max_weight + 1e-12:
                notes.append(f"{t} capped {w:+.3f}->{c.max_weight if w > 0 else -c.max_weight:+.3f}")
                w = c.max_weight if w > 0 else -c.max_weight
            out[t] = w
        shorts = -sum(w for w in out.values() if w < 0)
        if shorts > c.max_short_gross + 1e-12:
            k = c.max_short_gross / shorts
            notes.append(f"short book {shorts:.3f} scaled to {c.max_short_gross:.3f}")
            out = {t: w * k if w < 0 else w for t, w in out.items()}
        out = self._scale(out, notes)
        longs = sum(w for w in out.values() if w > 0)
        shorts = -sum(w for w in out.values() if w < 0)
        net = longs - shorts
        if net > c.max_net + 1e-12 and longs > 0:
            k = max(0.0, (c.max_net + shorts) / longs)
            notes.append(f"net {net:+.3f} above {c.max_net:+.3f}: longs scaled by {k:.3f}")
            out = {t: w * k if w > 0 else w for t, w in out.items()}
        elif net < c.min_net - 1e-12 and shorts > 0:
            k = max(0.0, (longs - c.min_net) / shorts)
            notes.append(f"net {net:+.3f} below {c.min_net:+.3f}: shorts scaled by {k:.3f}")
            out = {t: w * k if w < 0 else w for t, w in out.items()}
        need = mg.initial_requirement(out, c.margin)
        room = 1.0 / (1.0 + c.margin.cushion)
        if need > room + 1e-12:
            notes.append(f"initial margin {need:.1%} of equity scaled to {room:.1%}")
            out = {t: w * room / need for t, w in out.items()}
        if self.delever < 1.0:
            notes.append(f"deleveraging x{self.delever:.3f}: equity near the maintenance requirement")
            out = {t: w * self.delever for t, w in out.items()}
        if self.margin_called:
            notes.append("margin call: reduce only")
        if notes:
            self.adjustments += 1
        return ApprovedTargets(out, ts, notes=tuple(notes), reduce_only=self.margin_called)

    def _scale(self, out: dict, notes: list) -> dict:
        gross = sum(abs(w) for w in out.values())
        if gross > self.config.max_gross + 1e-12:
            notes.append(f"gross {gross:.3f} scaled to {self.config.max_gross:.3f}")
            return {t: w * self.config.max_gross / gross for t, w in out.items()}
        return out

    def _on_targets(self, msg):
        self.last_weights = dict(msg.weights)
        self.publish_message(T_APPROVED, self._approve(msg.weights, msg.ts))

    def _on_equity(self, snap):
        if self.margin_called and self._cleared("margin_call"):
            self.margin_called = False
            self.publish_message(T_RISK, RiskEvent(snap.ts, "margin_call_cleared", "operator cleared the margin call"))
        if self.halted and self._breaker_cleared():
            self.halted = False
            self.peak = snap.equity  # a fresh start: the drawdown limit counts from here
            self.publish_message(
                T_RISK,
                RiskEvent(
                    snap.ts, "breaker_cleared", f"operator cleared the breaker; peak reset to {snap.equity:,.2f}"
                ),
            )
        self.peak = max(self.peak, snap.equity)
        self._check_margin(snap)
        if self.halted or self.peak <= 0:
            self._save()
            return
        dd = 1.0 - snap.equity / self.peak
        if dd >= self.config.max_drawdown:
            self.halted = True
            detail = f"drawdown {dd:.1%} >= limit {self.config.max_drawdown:.0%}: flattening and halting"
            self.log.warning(detail)
            if self.config.halt_path:
                set_halt(self.config.halt_path, "circuit_breaker", detail)
            self.publish_message(T_RISK, RiskEvent(snap.ts, "circuit_breaker", detail))
            self.publish_message(
                T_APPROVED, self._approve(self.last_weights or dict.fromkeys(snap.positions, 0.0), snap.ts)
            )
        self._save()

    def _check_margin(self, snap):
        long_value, short_value = snap.long_value, snap.short_value
        if long_value + short_value <= 0 or snap.equity <= 0:
            self.delever = 1.0
            return
        m = self.config.margin
        need = mg.maintenance_requirement(long_value, short_value, m)
        scale = mg.deleverage_scale(snap.equity, long_value, short_value, m)
        republish = abs(scale - self.delever) > 1e-9
        if snap.equity < need and not self.margin_called:
            detail = f"equity {snap.equity:,.2f} below the maintenance requirement {need:,.2f}"
            self.margin_called = True
            republish = True
            if self.config.halt_path:
                set_halt(self.config.halt_path, "margin_call", detail)
            self.publish_message(T_RISK, RiskEvent(snap.ts, "margin_call", detail + ": exposure may only shrink"))
        if scale < 1.0 and scale < self.delever - 1e-6:
            self.publish_message(
                T_RISK,
                RiskEvent(
                    snap.ts,
                    "deleverage",
                    f"equity {snap.equity:,.2f} vs maintenance {need:,.2f} (+{m.cushion:.0%} cushion): "
                    f"targets scaled by {scale:.3f}",
                ),
            )
        self.delever = scale
        if republish and self.last_weights and not self.halted:
            # Applies to today's orders: the ExecutionAgent is about to size them from these approvals.
            self.publish_message(T_APPROVED, self._approve(self.last_weights, snap.ts))
