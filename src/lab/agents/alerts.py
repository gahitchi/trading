"""AlertAgent: turns what the other agents report into notifications for a human."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from nautilus_trader.common import DataActor, DataActorConfig

from lab.agents.messages import T_EQUITY, T_FILL, T_RISK

# Risk events worth waking someone up for (everything else goes to the daily report).
URGENT = {
    "circuit_breaker",
    "margin_call",
    "cash_mismatch",
    "paper_guard",
    "reconciliation_mismatch",
    "hard_halt",
    "kill_switch",
    "order_rejected",
    "order_denied",
    "ledger_reset",
    "blocked",
    "daily_cap",
    "order_capped",
    "order_expired",
    # transient conditions the system handled by itself; worth knowing, nothing to do
    "auto_resumed",
    "fill_recovered",
    "order_unknown",
    "foreign_fill",
    "stale_data",
    "breaker_cleared",
}


class AlertAgentConfig(DataActorConfig):
    def __init__(self, *, notifier, **_kwargs):
        super().__init__()
        self.notifier = notifier


class AlertAgent(DataActor):
    def __init__(self, config: AlertAgentConfig):
        super().__init__(config)
        self.notifier = config.notifier
        self.last_equity = None
        self.last_report_day = None
        self.pending_fills = []
        self.flush_scheduled = False

    def on_start(self):
        self.subscribe_topic(T_RISK, self._on_risk)
        self.subscribe_topic(T_EQUITY, self._on_equity)
        self.subscribe_topic(T_FILL, self._on_fill)

    def on_stop(self):
        self._flush_fills(None)

    def _on_risk(self, ev):
        if ev.kind in URGENT:
            self.notifier.send(f"⚠ {ev.kind}: {ev.detail}")
        elif ev.kind == "startup":
            self.notifier.send(f"started. {ev.detail}")

    def _on_equity(self, snap):
        day = datetime.fromtimestamp(snap.ts / 1e9, ZoneInfo("America/New_York")).date()
        if day == self.last_report_day:
            return
        self.last_report_day = day
        change = ""
        if self.last_equity:
            d = snap.equity / self.last_equity - 1
            change = f" ({d:+.2%} since last report)"
        self.last_equity = snap.equity
        held = ", ".join(f"{t} {q:g}" for t, q in snap.positions.items() if q) or "none"
        self.notifier.send(
            f"daily: equity {snap.equity:,.2f} USD{change}, invested {snap.gross:.0%}, "
            f"cash {snap.cash:,.2f}. Positions: {held}."
        )

    def _on_fill(self, f):
        self.pending_fills.append(f)
        if not self.flush_scheduled:
            self.flush_scheduled = True
            self.clock.set_time_alert_ns(
                "alerts-fills",
                self.clock.timestamp_ns() + int(timedelta(minutes=2).total_seconds() * 1e9),
                callback=self._flush_fills,
            )

    def _flush_fills(self, _event):
        fills, self.pending_fills, self.flush_scheduled = self.pending_fills, [], False
        if fills:
            lines = [f"{f.side} {f.qty:g} {f.ticker} @ {f.price:.2f} (fee {f.commission:.2f})" for f in fills]
            self.notifier.send("fills:\n" + "\n".join(lines))
