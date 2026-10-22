"""AllocatorAgent: combines sleeve targets into per-stock portfolio weights."""

from __future__ import annotations

from nautilus_trader.common import DataActor, DataActorConfig

from lab.agents.messages import T_SIGNAL, T_TARGETS, TargetWeights


class AllocatorAgentConfig(DataActorConfig):
    def __init__(self, *, sleeves: dict, **_kwargs):
        """sleeves: sleeve id -> (ticker, weight). Weights are fractions of total equity."""
        super().__init__()
        self.sleeves = sleeves


class AllocatorAgent(DataActor):
    def __init__(self, config: AllocatorAgentConfig):
        super().__init__(config)
        total = sum(w for _, w in config.sleeves.values())
        if total > 1.0 + 1e-9:
            raise ValueError(f"sleeve weights sum to {total:.3f} > 1 (no leverage)")
        self.targets: dict[str, float] = {}
        self.tickers = sorted({t for t, _ in config.sleeves.values()})

    def on_start(self):
        self.subscribe_topic(T_SIGNAL, self._on_signal)

    def _on_signal(self, msg):
        if msg.sleeve not in self.config.sleeves:
            return
        self.targets[msg.sleeve] = msg.target
        weights = dict.fromkeys(self.tickers, 0.0)
        for sleeve, (ticker, w) in self.config.sleeves.items():
            weights[ticker] += w * self.targets.get(sleeve, 0.0)
        self.publish_message(T_TARGETS, TargetWeights(weights, msg.ts))
