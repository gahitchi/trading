"""Portable broker-capture conversion checks; no credentials, exchange calls or orders."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from lab.kernel.order_lifecycle import BrokerEvidence, LifecycleError, OfflineOrderSession
from lab.kernel.paper_capture_bridge import b01_review, load_paper_capture
from lab.markets.domain import OrderIntent, Side
from lab.markets.journal import EventJournal
from lab.safety import SafetyError

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "IB-DUR123456"
INSTRUMENT = "GOOG.XNAS"
SOURCE = hashlib.sha256(b"simulated source").hexdigest()


def _write(folder: Path, name: str, data: object) -> None:
    (folder / name).write_text(json.dumps(data))


def fixture(folder: Path) -> None:
    _write(folder, "identity.json", {"paper_port": 4002, "captured_at": "2026-01-01T00:00:00+00:00"})
    _write(folder, "broker_account.json", {"account_id": ACCOUNT, "balances": []})
    _write(
        folder,
        "broker_positions.json",
        {
            "GOOG": {
                "net": 3.75,
                "net_exact": "3.75",
                "positions": [{"side": "LONG", "quantity": "3.75"}],
            },
        },
    )
    _write(
        folder,
        "broker_orders.json",
        [
            {
                "instrument_id": INSTRUMENT,
                "client_order_id": "A",
                "venue_order_id": "BK1",
                "status": "PARTIALLY_FILLED",
                "quantity": "3",
                "filled_qty": "1.25",
                "events": [{"type": "OrderFilled", "trade_id": "EX1", "last_qty": "1.25", "ts_event": 3}],
            }
        ],
    )
    _write(
        folder,
        "broker_capture_meta.json",
        {
            "capture_started_ns": 5,
            "capture_completed_ns": 10,
            "stream": "NAUTILUS_IBKR_READ_ONLY",
            "coverage_verified": False,
            "broker_authentication_verified": False,
            "causal_boundary_verified": False,
        },
    )


def load(folder: Path):
    return load_paper_capture(
        folder,
        expected_account=ACCOUNT,
        ticker="GOOG",
        instrument_id=INSTRUMENT,
        action_version="v1",
    )


class PaperCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        fixture(self.path)

    def change(self, name: str, update) -> None:
        path = self.path / name
        doc = json.loads(path.read_text())
        update(doc)
        _write(self.path, name, doc)

    def test_read_only_projection_is_exact_but_never_authorized(self):
        observed = load(self.path)
        result = observed.snapshot
        self.assertEqual(result.account_scope, ACCOUNT)
        self.assertEqual(result.position, Decimal("3.75"))
        self.assertEqual(result.orders[0].status, "open")
        self.assertEqual(result.orders[0].cumulative_filled, Decimal("1.25"))
        self.assertEqual(result.executions[0].execution_id, "EX1")
        self.assertEqual(result.executions[0].available_ns, 10)
        self.assertTrue(result.snapshot_id.startswith("CAP-"))
        self.assertEqual(observed.manifest_sha256, result.source_sha256)
        self.assertFalse(result.positions_complete)
        self.assertFalse(result.orders_complete)
        self.assertFalse(result.executions_complete)
        self.assertFalse(result.causal_boundary_complete)
        self.assertFalse(observed.broker_authenticated)
        self.assertFalse(observed.can_submit_orders)
        self.assertFalse(observed.can_release_reservations)
        self.assertEqual(load(self.path).manifest_sha256, observed.manifest_sha256)

    def test_unqualified_broker_match_still_blocks_release_and_dispatch(self):
        with EventJournal(self.path / "oms.sqlite3") as journal:
            session = OfflineOrderSession(
                journal,
                account_scope=ACCOUNT,
                instrument_id=INSTRUMENT,
                action_version="v1",
                starting_position=Decimal("5"),
                quantity_step=Decimal("0.25"),
            )
            session.plan(OrderIntent("A", INSTRUMENT, Side.SELL, Decimal("3"), 1, True), action_version="v1")
            session.prepare_dispatch("A", timestamp_ns=2)

            def evidence(event_id: str, at: int) -> BrokerEvidence:
                return BrokerEvidence(event_id, ACCOUNT, "fixture", SOURCE, at - 1, at, "v1")

            session.observe("A", "ack", evidence("ACK1", 3), broker_order_id="BK1")
            session.observe("A", "fill", evidence("FILL1", 4), quantity=Decimal("1.25"), execution_id="EX1")
            digest = session.state_digest()
            report = load(self.path).compare(session)
            self.assertFalse(report.internally_consistent)
            self.assertEqual(
                report.findings,
                (
                    "CAUSAL_FENCE_UNASSERTED",
                    "EXECUTION_COVERAGE_UNASSERTED",
                    "ORDER_COVERAGE_UNASSERTED",
                    "POSITION_COVERAGE_UNASSERTED",
                ),
            )
            self.assertFalse(report.source_authenticated)
            self.assertFalse(report.broker_authorization)
            self.assertFalse(report.release_reservations)
            self.assertEqual(session.state_digest(), digest)

    def test_reject_live_port_or_wrong_account(self):
        self.change("identity.json", lambda doc: doc.update(paper_port=4001))
        with self.assertRaises(SafetyError):
            load(self.path)
        fixture(self.path)
        self.change("broker_account.json", lambda doc: doc.update(account_id="IB-U123456"))
        with self.assertRaisesRegex(LifecycleError, "differs"):
            load(self.path)
        fixture(self.path)
        with self.assertRaises(SafetyError):
            load_paper_capture(
                self.path,
                expected_account="IB-U123456",
                ticker="GOOG",
                instrument_id=INSTRUMENT,
                action_version="v1",
            )

    def test_invalid_or_missing_exact_position_fails_closed(self):
        self.change("broker_positions.json", lambda doc: doc["GOOG"].pop("net_exact"))
        with self.assertRaisesRegex(LifecycleError, "exact"):
            load(self.path)
        fixture(self.path)
        self.change("broker_positions.json", lambda doc: doc["GOOG"].update(net_exact="3.75001"))
        with self.assertRaisesRegex(LifecycleError, "disagree"):
            load(self.path)
        fixture(self.path)
        self.change("broker_positions.json", lambda doc: doc["GOOG"].update(net_exact=float("nan")))
        with self.assertRaisesRegex(LifecycleError, "invalid capture JSON"):
            load(self.path)

    def test_missing_fill_history_or_overfill_never_gets_fabricated(self):
        self.change("broker_orders.json", lambda doc: doc[0].update(events=[]))
        with self.assertRaisesRegex(LifecycleError, "complete per-order execution"):
            load(self.path)
        fixture(self.path)
        self.change("broker_orders.json", lambda doc: doc[0].update(filled_qty="4"))
        with self.assertRaisesRegex(LifecycleError, "overfilled"):
            load(self.path)

    def test_order_identity_and_execution_evidence_must_not_be_fabricated(self):
        self.change("broker_orders.json", lambda doc: doc[0].update(venue_order_id=None))
        with self.assertRaises(LifecycleError):
            load(self.path)
        fixture(self.path)
        self.change("broker_orders.json", lambda doc: doc[0]["events"][0].update(trade_id=None))
        with self.assertRaises(LifecycleError):
            load(self.path)
        fixture(self.path)
        self.change("broker_orders.json", lambda doc: doc[0]["events"][0].update(ts_event=11))
        with self.assertRaisesRegex(LifecycleError, "beyond"):
            load(self.path)

    def test_reject_false_coverage_claims_or_capture_time_reversal(self):
        self.change("broker_capture_meta.json", lambda doc: doc.update(coverage_verified=True))
        with self.assertRaisesRegex(LifecycleError, "cannot claim"):
            load(self.path)
        fixture(self.path)
        self.change("broker_capture_meta.json", lambda doc: doc.update(capture_started_ns=11))
        with self.assertRaisesRegex(LifecycleError, "reversed"):
            load(self.path)

    def test_provenance_binds_file_changes_and_rejects_symlinks(self):
        first = load(self.path).manifest_sha256
        self.change("broker_account.json", lambda doc: doc.update(balances=[{"currency": "USD"}]))
        self.assertNotEqual(load(self.path).manifest_sha256, first)
        (self.path / "broker_orders.json").unlink()
        (self.path / "broker_orders.json").symlink_to(self.path / "broker_account.json")
        with self.assertRaisesRegex(LifecycleError, "regular file"):
            load(self.path)

    def test_private_cli_requires_matching_paper_config_and_never_discloses_account(self):
        config = self.path / "lab.toml"
        config.write_text('[paper]\naccount_id = "DUR123456"\nport = 4002\n[universe]\nGOOG = "XNAS"\n')

        def inspect() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    sys.executable,
                    "-S",
                    "-m",
                    "lab.kernel.paper_capture_bridge",
                    "--config",
                    str(config),
                    "--capture",
                    str(self.path),
                    "--ticker",
                    "GOOG",
                    "--action-version",
                    "v1",
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

        good = inspect()
        self.assertEqual(good.returncode, 0, good.stderr)
        report = json.loads(good.stdout)
        self.assertEqual(report["mode"], "UNQUALIFIED_PRIVATE_CAPTURE")
        self.assertEqual(report["history_coverage"], "UNVERIFIED")
        self.assertFalse(report["broker_authorization"])
        self.assertFalse(report["broker_authenticated"])
        self.assertFalse(report["release_reservations"])
        self.assertEqual(report["order_count"], 1)
        self.assertEqual(report["execution_count"], 1)
        self.assertNotIn(ACCOUNT, good.stdout + good.stderr)

        config.write_text(config.read_text().replace("port = 4002", "port = 7497"))
        wrong_port = inspect()
        self.assertEqual(wrong_port.returncode, 2)
        self.assertNotIn(ACCOUNT, wrong_port.stdout + wrong_port.stderr)

        config.write_text(config.read_text().replace("DUR123456", "U123456"))
        wrong_account = inspect()
        self.assertEqual(wrong_account.returncode, 2)
        self.assertNotIn(ACCOUNT, wrong_account.stdout + wrong_account.stderr)

    def test_inspector_uses_same_environment_paper_overrides_as_capture(self):
        config = self.path / "lab.toml"
        config.write_text('[paper]\\naccount_id = ""\\nport = 7497\\n[universe]\\nGOOG = "XNAS"\\n')

        def inspect(overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
            env = {
                **{key: value for key, value in os.environ.items() if key not in ("LAB_PAPER_ACCOUNT", "LAB_IB_PORT")},
                **overrides,
                "PYTHONPATH": str(ROOT / "src"),
            }
            return subprocess.run(
                [
                    sys.executable,
                    "-S",
                    "-m",
                    "lab.kernel.paper_capture_bridge",
                    "--config",
                    str(config),
                    "--capture",
                    str(self.path),
                    "--ticker",
                    "GOOG",
                    "--action-version",
                    "v1",
                    "--b01-report",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )

        good = inspect({"LAB_PAPER_ACCOUNT": "DUR123456", "LAB_IB_PORT": "4002"})
        self.assertEqual(good.returncode, 0, good.stderr)
        report = json.loads(good.stdout)
        self.assertEqual(report["qualification"], "BLOCKED")
        self.assertFalse(report["broker_authorization"])
        self.assertNotIn(ACCOUNT, good.stdout + good.stderr)

        missing = inspect({"LAB_IB_PORT": "4002"})
        self.assertEqual(missing.returncode, 2)
        self.assertNotIn(ACCOUNT, missing.stdout + missing.stderr)

        wrong_port = inspect({"LAB_PAPER_ACCOUNT": "DUR123456"})
        self.assertEqual(wrong_port.returncode, 2)
        self.assertNotIn(ACCOUNT, wrong_port.stdout + wrong_port.stderr)

        live_port = inspect({"LAB_PAPER_ACCOUNT": "DUR123456", "LAB_IB_PORT": "4001"})
        self.assertEqual(live_port.returncode, 2)
        self.assertNotIn(ACCOUNT, live_port.stdout + live_port.stderr)

    def test_explicit_pinned_gateway_port_must_match_capture(self):
        with self.assertRaisesRegex(LifecycleError, "gateway port"):
            load_paper_capture(
                self.path,
                expected_account=ACCOUNT,
                expected_port=7497,
                ticker="GOOG",
                instrument_id=INSTRUMENT,
                action_version="v1",
            )

    def test_b01_matching_cache_still_cannot_authorize_broker(self):
        self.change(
            "broker_account.json",
            lambda row: row.update(
                balances=[{"currency": "USD", "total": "1000.01", "free": "900.01", "locked": "100"}]
            ),
        )
        observation = load(self.path)
        review = b01_review(self.path, observation, now_ns=11, max_age_seconds=900)
        self.assertEqual(review["qualification"], "BLOCKED")
        self.assertEqual(review["stage"], "B01_READ_ONLY_EVIDENCE_REVIEW")
        self.assertEqual(review["observed_currency_count"], 1)
        self.assertEqual(review["observed_order_count"], 1)
        self.assertEqual(review["observed_execution_count"], 1)
        self.assertFalse(review["source_authenticated"])
        self.assertFalse(review["broker_authorization"])
        self.assertFalse(review["release_reservations"])
        self.assertIn("BROKER_SESSION_IDENTITY_UNVERIFIED", review["unresolved_evidence"])
        self.assertIn("CASH_AND_FEE_LEDGER_PARITY_UNVERIFIED", review["unresolved_evidence"])
        self.assertNotIn("USD_CURRENCY_BALANCE_NOT_OBSERVED", review["unresolved_evidence"])
        self.assertNotIn(ACCOUNT, json.dumps(review))

    def test_b01_rejects_mutated_evidence_and_nonnumeric_balances(self):
        observation = load(self.path)
        self.change("broker_account.json", lambda row: row.update(balances=[{"currency": "EUR"}]))
        with self.assertRaisesRegex(LifecycleError, "changed between"):
            b01_review(self.path, observation, now_ns=11)
        with self.assertRaisesRegex(LifecycleError, "decimal string"):
            b01_review(self.path, load(self.path), now_ns=11)
        fixture(self.path)
        self.change(
            "broker_account.json",
            lambda row: row.update(
                balances=[
                    {"currency": "USD", "total": "10", "free": "10", "locked": "0"},
                    {"currency": "USD", "total": "10", "free": "10", "locked": "0"},
                ]
            ),
        )
        with self.assertRaisesRegex(LifecycleError, "duplicate broker currency"):
            b01_review(self.path, load(self.path), now_ns=11)

    def test_b01_stale_and_future_captures_stay_blocked(self):
        observation = load(self.path)
        stale = b01_review(self.path, observation, now_ns=901_000_000_011, max_age_seconds=900)
        self.assertEqual(stale["qualification"], "BLOCKED")
        self.assertIn("CAPTURE_OLDER_THAN_OPERATOR_FRESHNESS_LIMIT", stale["unresolved_evidence"])
        future = b01_review(self.path, observation, now_ns=9, max_age_seconds=900)
        self.assertIn("CAPTURE_TIMESTAMP_IN_FUTURE", future["unresolved_evidence"])
        with self.assertRaisesRegex(LifecycleError, "freshness"):
            b01_review(self.path, observation, now_ns=11, max_age_seconds=0)

    def test_b01_cli_emits_only_blocked_nonidentifying_evidence(self):
        config = self.path / "lab.toml"
        config.write_text('[paper]\naccount_id = "DUR123456"\nport = 4002\n[universe]\nGOOG = "XNAS"\n')
        result = subprocess.run(
            [
                sys.executable,
                "-S",
                "-m",
                "lab.kernel.paper_capture_bridge",
                "--config",
                str(config),
                "--capture",
                str(self.path),
                "--ticker",
                "GOOG",
                "--action-version",
                "v1",
                "--b01-report",
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["qualification"], "BLOCKED")
        self.assertFalse(report["broker_authorization"])
        self.assertNotIn(ACCOUNT, result.stdout + result.stderr)
        self.assertNotIn("1000.01", result.stdout)

    def test_strict_stdlib_import_no_broker_or_native_runtime(self):
        script = (
            "import sys; from lab.kernel.paper_capture_bridge import load_paper_capture; "
            "assert 'nautilus_trader' not in sys.modules; "
            "assert 'numpy' not in sys.modules; print('read-only-capture')"
        )
        process = subprocess.run(
            [sys.executable, "-S", "-c", script],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), "read-only-capture")


if __name__ == "__main__":
    unittest.main()
