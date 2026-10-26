"""The real offline reference engine must run without site-packages on Termux.

Native Android/Bionic Python cannot import the project's native Nautilus,
PyArrow, pandas or NumPy stack; do not replace the engine to accommodate that.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lab.markets.domain import TradingMode, UnsupportedMode
from lab.markets.simulator import OfflineSimulator


REPO = Path(__file__).resolve().parents[1]


def stdlib_run(*arguments: str) -> dict:
    # -S removes third-party site packages and gives a genuine no-NumPy import
    # check on CI (also works on the operator's native Termux Python runtime).
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    result = subprocess.run(
        [sys.executable, "-S", "-m", *arguments],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise AssertionError(f"stdlib-only mobile subprocess failed: {result.stderr}\n{result.stdout}")
    return json.loads(result.stdout)


class PortableOfflineEngineTests(unittest.TestCase):
    def test_no_numpy_import_for_domain_and_simulator(self):
        result = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                "import sys; from lab.markets import OfflineSimulator, MinuteBuilder; "
                "assert 'numpy' not in sys.modules; print('portable')",
            ],
            cwd=REPO,
            env={**os.environ, "PYTHONPATH": str(REPO / "src")},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "portable")

    def test_existing_market_cli_runs_without_site_packages(self):
        report = stdlib_run("lab.markets", "demo", "--side", "buy")
        self.assertEqual(report["mode"], "offline_sim")
        self.assertEqual(report["input_kind"], "synthetic_fixture")
        self.assertIs(report["market_data_validated"], False)
        self.assertIs(report["qualified_for_live_trading"], False)
        self.assertGreater(report["fills"], 0)
        self.assertEqual(report["pending_orders"], 0)
        self.assertGreater(report["trades_received"], 2)

    def test_mobile_demo_uses_same_engine_not_new_fills(self):
        reference = stdlib_run("lab.markets", "demo", "--side", "buy")
        pocket = stdlib_run("lab.mobile", "demo")
        for key in ("instrument", "mode", "balance", "equity", "position", "fees", "fills", "realized_pnl"):
            self.assertEqual(pocket[key], reference[key], key)
        self.assertIs(pocket["broker_connection_enabled"], False)
        self.assertIs(pocket["nautilus_engine_used"], False)
        self.assertEqual(pocket["execution_authority"], "NONE_OFFLINE_ONLY")

    def test_market_journal_replay_without_site_packages(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = str(Path(folder) / "offline.sqlite3")
            original = stdlib_run("lab.markets", "demo", "--side", "buy", "--journal", journal)
            recovered = stdlib_run("lab.markets", "recover", "--journal", journal)
            for key in ("balance", "equity", "position", "fees", "fills", "realized_pnl"):
                self.assertEqual(recovered[key], original[key], key)
            self.assertTrue(recovered["recovery_verified"])
            self.assertIs(recovered["external_execution_enabled"], False)

    def test_native_live_and_broker_demo_modes_stay_refused(self):
        from lab.markets.__main__ import fixture_instrument

        for mode in (TradingMode.LIVE, TradingMode.BROKER_DEMO):
            with self.subTest(mode=mode), self.assertRaises(UnsupportedMode):
                OfflineSimulator(fixture_instrument(), mode=mode)


if __name__ == "__main__":
    unittest.main()
