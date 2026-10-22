"""RiskAgent: enforces position, exposure and drawdown limits on every target."""

from __future__ import annotations

from nautilus_trader.common import DataActor, DataActorConfig

from lab.agents.messages import T_APPROVED, T_EQUITY, T_RISK, T_TARGETS, ApprovedTargets, RiskEvent


class RiskAgentConfig(DataActorConfig):
    def __init__(self, *, max_weight: float, max_gross: float, max_drawdown: float, **_kwargs):
        super().__init__()
        self.max_weight = max_weight
        self.max_gross = max_gross
        self.max_drawdown = max_drawdown


class RiskAgent(DataActor):
    """Nothing reaches the ExecutionAgent without passing through here.

    - each stock is capped at max_weight of equity
    - total exposure is scaled down to max_gross
    - circuit breaker: once equity falls max_drawdown below its peak, everything is
      flattened and the agent stays halted until restarted
    """

    def __init__(self, config: RiskAgentConfig):
        super().__init__(config)
        self.peak = 0.0
        self.halted = False
        self.last_weights: dict = {}
        self.adjustments = 0

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
            w = max(0.0, float(w))  # long-only
            if w > c.max_weight + 1e-12:
                notes.append(f"{t} capped {w:.3f}->{c.max_weight:.3f}")
                w = c.max_weight
            out[t] = w
        gross = sum(out.values())
        if gross > c.max_gross + 1e-12:
            scale = c.max_gross / gross
            notes.append(f"gross {gross:.3f} scaled to {c.max_gross:.3f}")
            out = {t: w * scale for t, w in out.items()}
        if notes:
            self.adjustments += 1
        return ApprovedTargets(out, ts, notes=tuple(notes))

    def _on_targets(self, msg):
        self.last_weights = dict(msg.weights)
        self.publish_message(T_APPROVED, self._approve(msg.weights, msg.ts))

    def _on_equity(self, snap):
        self.peak = max(self.peak, snap.equity)
        if self.halted or self.peak <= 0:
            return
        dd = 1.0 - snap.equity / self.peak
        if dd >= self.config.max_drawdown:
            self.halted = True
            detail = f"drawdown {dd:.1%} >= limit {self.config.max_drawdown:.0%}: flattening and halting"
            self.log.warning(detail)
            self.publish_message(T_RISK, RiskEvent(snap.ts, "circuit_breaker", detail))
            self.publish_message(T_APPROVED, self._approve(self.last_weights or dict.fromkeys(snap.positions, 0.0), snap.ts))
