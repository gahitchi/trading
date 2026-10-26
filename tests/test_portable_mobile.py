"""Termux-compatible app tests; no trading engine, network or third-party wheels."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

from lab.mobile import MobileDiagnosticError, _page, doctor, inspect_snapshot, main, serve


def state(**changes):
    result = {"version": 1, "cash": 1000.0, "positions": {"PRIVATE": 2}, "pending": {}}
    result.update(changes)
    return result


class PocketAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def path(self, name="projection.json"):
        return self.directory / name

    def test_doctor_does_not_claim_engine_or_order_permissions(self):
        status = doctor()
        self.assertEqual(status["mode"], "OFFLINE_DIAGNOSTIC_ONLY")
        for key in (
            "native_nautilus_execution_qualified",
            "broker_connection_enabled",
            "order_submission_enabled",
            "live_money_enabled",
        ):
            self.assertIs(status[key], False)

    def test_json_projection_is_validated_but_never_authoritative(self):
        file = self.path()
        file.write_text(json.dumps(state()))
        out = inspect_snapshot(file)
        self.assertTrue(out["schema_valid"])
        self.assertFalse(out["authoritative"])
        self.assertFalse(out["broker_reconciled"])
        self.assertFalse(out["execution_permitted"])
        self.assertEqual(out["positions_recorded"], 1)
        self.assertEqual(out["pending_orders_recorded"], 0)
        self.assertEqual(len(out["artifact_sha256"]), 64)
        self.assertNotIn("PRIVATE", json.dumps(out))

    def test_malformed_ledger_duplicate_keys_and_nan_fail_closed(self):
        for raw in (
            '{"version":1,"cash":NaN}',
            '{"version":1,"cash":1,"cash":2}',
            '{"version":1,"cash":"100"}',
            '{"version":3,"cash":1000}',
        ):
            with self.subTest(raw=raw):
                file = self.path()
                file.write_text(raw)
                with self.assertRaises(MobileDiagnosticError):
                    inspect_snapshot(file)

    def test_snapshot_suffix_symlink_and_size_restrictions(self):
        file = self.path("wrong.txt")
        file.write_text(json.dumps(state()))
        with self.assertRaises(MobileDiagnosticError):
            inspect_snapshot(file)
        link = self.path("link.json")
        link.symlink_to(file)
        with self.assertRaises(MobileDiagnosticError):
            inspect_snapshot(link)
        other = self.path()
        with other.open("wb") as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(MobileDiagnosticError, "too large"):
            inspect_snapshot(other)

    def test_sqlite_snapshot_is_read_only_and_not_broker_qualified(self):
        file = self.path("ledger.sqlite3")
        with sqlite3.connect(file) as db:
            db.execute("PRAGMA user_version=1")
            db.execute("CREATE TABLE snapshot (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            record = state(
                version=3,
                authoritative_store=file.name,
                pending={"order-1": {"side": "SELL", "ticker": "PRIVATE", "qty": 1, "filled": 0}},
            )
            db.execute("INSERT INTO snapshot VALUES (1,?)", (json.dumps(record),))
        original = file.read_bytes()
        out = inspect_snapshot(file)
        self.assertTrue(out["authoritative"])
        self.assertFalse(out["broker_reconciled"])
        self.assertEqual(out["pending_orders_recorded"], 1)
        self.assertEqual(file.read_bytes(), original)
        self.assertNotIn("PRIVATE", json.dumps(out))

    def test_sqlite_wrong_binding_and_schema_are_rejected(self):
        file = self.path("ledger.sqlite3")
        with sqlite3.connect(file) as db:
            db.execute("PRAGMA user_version=1")
            db.execute("CREATE TABLE snapshot (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            db.execute(
                "INSERT INTO snapshot VALUES (1,?)",
                (json.dumps(state(version=3, authoritative_store="wrong.sqlite3")),),
            )
        with self.assertRaisesRegex(MobileDiagnosticError, "cannot verify"):
            inspect_snapshot(file)
        with sqlite3.connect(file) as db:
            db.execute("PRAGMA user_version=2")
        with self.assertRaisesRegex(MobileDiagnosticError, "cannot verify"):
            inspect_snapshot(file)

    def test_dashboard_redacts_account_data(self):
        file = self.path()
        file.write_text(json.dumps(state(account="MY_PRIVATE_ACCOUNT")))
        output = _page(inspect_snapshot(file)).decode()
        self.assertIn("Execution authority", output)
        self.assertIn("DISABLED", output)
        self.assertNotIn("MY_PRIVATE_ACCOUNT", output)
        self.assertNotIn("PRIVATE", output)
        self.assertNotIn("<script", output)

    def test_server_only_binds_loopback_with_read_only_handler(self):
        fake = MagicMock()
        fake.__enter__.return_value = MagicMock(server_port=8768)
        with patch("lab.mobile.ThreadingHTTPServer", return_value=fake) as builder:
            serve(port=8768)
            handler = builder.call_args.args[1]
            self.assertTrue(hasattr(handler, "do_POST"))
            builder.assert_called_once_with(("127.0.0.1", 8768), ANY)
            fake.__enter__.return_value.serve_forever.assert_called_once()
        with self.assertRaises(MobileDiagnosticError):
            serve(port=65536)

    def test_command_line_reports_doctor_and_blocks_bad_export(self):
        with patch("lab.mobile.print") as console:
            self.assertEqual(main(["doctor"]), 0)
            self.assertIn("OFFLINE_DIAGNOSTIC_ONLY", console.call_args.args[0])
        with patch("lab.mobile.print"):
            self.assertEqual(main(["inspect", str(self.path("missing.json"))]), 2)


if __name__ == "__main__":
    unittest.main()
