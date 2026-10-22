"""RecorderAgent: keeps the record of everything the other agents said and did."""

from __future__ import annotations

import json
from pathlib import Path

from nautilus_trader.common import DataActor, DataActorConfig

from lab.agents.messages import (
    T_APPROVED,
    T_EQUITY,
    T_FILL,
    T_RISK,
    T_SIGNAL,
    T_TARGETS,
    to_dict,
)


class RecorderAgentConfig(DataActorConfig):
    def __init__(self, *, journal: str | None = None, keep_signals: bool = False, **_kwargs):
        """journal: append every message as a JSON line (paper mode)."""
        super().__init__()
        self.journal = journal
        self.keep_signals = keep_signals


class RecorderAgent(DataActor):
    def __init__(self, config: RecorderAgentConfig):
        super().__init__(config)
        self.equity = []
        self.fills = []
        self.risk = []
        self.signals = []
        self.signal_count = 0
        self._fh = None

    def on_start(self):
        if self.config.journal:
            Path(self.config.journal).parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.config.journal, "a", buffering=1)  # noqa: SIM115 - held open for the node's lifetime, closed in on_stop
        self.subscribe_topic(T_EQUITY, lambda m: self._keep(self.equity, m))
        self.subscribe_topic(T_FILL, lambda m: self._keep(self.fills, m))
        self.subscribe_topic(T_RISK, lambda m: self._keep(self.risk, m))
        self.subscribe_topic(T_SIGNAL, self._on_signal)
        if self._fh:
            self.subscribe_topic(T_TARGETS, lambda m: self._write(m))
            self.subscribe_topic(T_APPROVED, lambda m: self._write(m))

    def _on_signal(self, m):
        self.signal_count += 1
        if self.config.keep_signals:
            self.signals.append(m)
        self._write(m)

    def _keep(self, store, m):
        store.append(m)
        self._write(m)

    def _write(self, m):
        if self._fh:
            self._fh.write(json.dumps(to_dict(m), default=str) + "\n")

    def on_stop(self):
        if self._fh:
            self._fh.close()
            self._fh = None
