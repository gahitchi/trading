"""Session-bound evidence of completed broker history for the local app."""

import math
from collections import deque
from datetime import UTC, datetime

from nautilus_trader.common import DataActor, DataActorConfig

from lab.agents.messages import T_MARK, T_RISK, T_SIGNAL
from lab.persistence import atomic_json


class ReadinessConfig(DataActorConfig):
    def __init__(self, *, path, session, binding, sleeves, window, source, **kwargs):
        super().__init__()
        self.path, self.session, self.binding = path, session, binding
        self.sleeves, self.window, self.source = sleeves, window, source


class ReadinessAgent(DataActor):
    def __init__(self, config):
        super().__init__(config)
        self.signals = {}
        self.errors = set()
        self.history = {t: deque(maxlen=config.window + 1) for t in set(config.sleeves.values())}

    def on_start(self):
        self.subscribe_topic(T_MARK, self.mark)
        self.subscribe_topic(T_SIGNAL, self.signal)
        self.subscribe_topic(T_RISK, self.risk)
        self.save()

    def mark(self, msg):
        if msg.ticker not in self.history:
            return
        history = self.history[msg.ticker]
        if math.isfinite(msg.price) and msg.price > 0 and (not history or msg.ts > history[-1]):
            history.append(msg.ts)

    def signal(self, msg):
        if msg.sleeve in self.config.sleeves:
            if not math.isfinite(msg.price) or msg.price <= 0 or not math.isfinite(msg.target):
                self.errors.add("Invalid strategy data")
            else:
                self.signals[msg.sleeve] = {"timestamp": msg.ts, "price": msg.price, "active": msg.active}
            self.save()

    def risk(self, msg):
        if msg.kind == "signal_failed":
            self.errors.add("A strategy failed; inspect the engine log before restarting")
            self.save()

    def save(self):
        atomic_json(
            self.config.path,
            {
                "version": 1,
                "session": self.config.session,
                "binding": self.config.binding,
                "source": self.config.source,
                "at": datetime.now(UTC).isoformat(),
                "signals": self.signals,
                "history": {t: list(rows) for t, rows in self.history.items()},
                "errors": sorted(self.errors),
            },
        )
