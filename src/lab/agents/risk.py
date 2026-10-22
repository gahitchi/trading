"""RiskAgent: enforces position, exposure and drawdown limits on every target."""

from __future__ import annotations

from nautilus_trader.common import DataActor, DataActorConfig

from lab.agents.messages import T_APPROVED, T_EQUITY, T_RISK, T_TARGETS, ApprovedTargets, RiskEvent
from lab.persistence import StateError, atomic_json_write, read_risk_checkpoint, risk_checkpoint


class RiskAgentConfig(DataActorConfig):
    def __init__(self, *, max_weight: float, max_gross: float, max_drawdown: float, state_path: str | None = None, **_kwargs):
        super().__init__()
        self.max_weight = max_weight
        self.max_gross = max_gross
        self.max_drawdown = max_drawdown
        self.state_path = state_path


class RiskAgent(DataActor):
    """Nothing reaches the ExecutionAgent without passing through here.

    - each stock is capped at max_weight of equity
    - total exposure is scaled down to max_gross
    - circuit breaker: once equity falls max_drawdown below its peak, everything is
      flattened and the agent stays halted across restarts until operator recovery
    """

    def __init__(self, config: RiskAgentConfig):
        super().__init__(config)
        self.peak = 0.0
        self.halted = False
        self.halt_reason = ""
        self.state_error = ""
        self.last_weights: dict = {}
        self.adjustments = 0
        if config.state_path:
            try:
                state = read_risk_checkpoint(config.state_path)
                self.peak = state["peak"]
                self.halted = state["halted"]
                self.halt_reason = state["reason"]
            except StateError as exc:
                self.halted = True
                self.halt_reason = self.state_error = str(exc)

    def on_start(self):
        self.subscribe_topic(T_TARGETS, self._on_targets)
        self.subscribe_topic(T_EQUITY, self._on_equity)
        if self.state_error:
            self.publish_message(T_RISK, RiskEvent(self.clock.timestamp_ns(), "risk_checkpoint_corrupt", self.state_error))

    def _persist(self):
        if self.config.state_path and not self.state_error:
            atomic_json_write(self.config.state_path, risk_checkpoint(self.peak, self.halted, self.halt_reason))

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
        was_halted = self.halted
        if not self.halted and snap.equity > self.peak:
            self.peak = snap.equity
        if not self.halted and self.peak > 0:
            dd = 1.0 - snap.equity / self.peak
            if dd >= self.config.max_drawdown:
                self.halted = True
                self.halt_reason = (
                    f"drawdown {dd:.1%} >= limit {self.config.max_drawdown:.0%}: flattening and halting"
                )
        try:
            # Persist BEFORE the next approval/order can leave this process.
            self._persist()
        except (OSError, ValueError, StateError) as exc:
            self.halted = True
            self.state_error = f"cannot persist risk checkpoint: {exc}"
            self.halt_reason = self.state_error

        if self.halted and not was_halted:
            kind = "risk_state_io_error" if self.state_error else "circuit_breaker"
            self.log.warning(self.halt_reason)
            self.publish_message(T_RISK, RiskEvent(snap.ts, kind, self.halt_reason))
            self.publish_message(
                T_APPROVED, self._approve(self.last_weights or dict.fromkeys(snap.positions, 0.0), snap.ts)
            )
