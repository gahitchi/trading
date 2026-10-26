"""Broker-attested, flat-book migration which adds London Gold to an existing PAPER runtime."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path

from lab.paper import _base_node, acquire_runtime_lock, runtime_dir
from lab.persistence import atomic_json
from lab.runtime_ops import gold_upgrade_plan, run
from lab.safety import is_paper_account, set_kill_switch


def broker_attestation(cfg, tickers, plan, *, wait_s=45, clients=None):
    """Connect with a DataActor, which has no order factory, and inspect the PAPER account."""
    from nautilus_trader.common import DataActor, DataActorConfig
    from nautilus_trader.model import Venue

    class AttestationActor(DataActor):
        def __init__(self):
            super().__init__(DataActorConfig())
            self.report = None

        def on_start(self):
            at = self.clock.timestamp_ns() + int(wait_s * 1e9)
            self.clock.set_time_alert_ns("gold-migration-attestation", at, callback=self._capture)

        def _capture(self, _event):
            account = self.cache.account_id(Venue("IB"))
            actual = str(account) if account is not None else ""
            positions = {
                str(position.instrument_id): float(position.signed_qty)
                for position in self.cache.positions_open()
                if float(position.signed_qty)
            }
            orders = [str(order.client_order_id) for order in self.cache.orders_open()]
            self.report = {
                "captured_at": datetime.now(UTC).isoformat(),
                "runtime_identity": plan["old"],
                "ledger": plan["ledger"],
                "account": actual.split("-", 1)[-1],
                "open_orders": orders,
                "positions": positions,
            }
            self.shutdown_system("gold migration attestation captured")

    actor = AttestationActor()
    lock = acquire_runtime_lock(cfg)
    try:
        node, _ = _base_node(cfg, tickers, clients, gold=True)
        node.add_actor(actor)
        try:
            node.run()
        finally:
            node.dispose()
    finally:
        lock.close()
    if actor.report is None:
        raise ValueError("IBKR did not provide a broker account snapshot")
    if not is_paper_account(actor.report["account"]):
        raise ValueError("IBKR did not report the configured PAPER account")
    return actor.report


def enable_gold(cfg, tickers, private_dir, reason, *, clients=None):
    """Pause, attest, back up and migrate an existing flat runtime without resetting its history."""
    rt = runtime_dir(cfg)
    set_kill_switch(rt / "KILL", "halt", reason, preserve_existing=True)
    plan = gold_upgrade_plan(rt, cfg)
    if plan["blockers"]:
        raise ValueError("; ".join(plan["blockers"]))
    report = broker_attestation(cfg, tickers, plan, clients=clients)
    private_dir = Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
    evidence = private_dir / f"gold-attestation-{stamp}.json"
    backup = private_dir / f"before-gold-{stamp}"
    atomic_json(evidence, report)
    try:
        return run(
            cfg,
            "enable-gold",
            evidence=evidence,
            reason="broker flat-book attestation; operator explicitly enabled London Gold",
            destination=backup,
        )
    finally:
        evidence.unlink(missing_ok=True)
