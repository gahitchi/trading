"""Native Nautilus fixture CLI uses real engine without trading authority."""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal

import pytest

from lab.markets.__main__ import native_demo


def test_native_fixture_engine_smoke_is_credential_free_and_not_a_strategy() -> None:
    result = native_demo()
    assert result["engine"] == "nautilus_backtest_native"
    assert result["mode"] == "offline_sim"
    assert result["instrument"] == "BTC-USD-SWAP.SIM"
    assert result["trade_observations"] == 6
    assert result["mark_observations"] == 6
    assert result["native_data_engine_used"] is True
    assert result["fees_charged_verified"] is False
    assert result["orders_routed"] is False
    assert result["broker_execution_enabled"] is False
    assert result["strategies_qualified"] is False
    assert result["qualified_for_live_trading"] is False
    assert Decimal(result["taker_fee_rate"]) > Decimal("0")


def test_native_fixture_cli_is_json_and_never_opens_a_broker() -> None:
    cmd = [sys.executable, "-m", "lab.markets", "native-demo"]
    done = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=40)
    payload = json.loads(done.stdout)
    assert payload["mark_observations"] == 6
    assert payload["trade_observations"] == 6
    assert payload["input_kind"] == "synthetic_fixture"
    assert not payload["orders_routed"]


def test_native_fixture_balance_is_exact_and_strictly_positive() -> None:
    with pytest.raises(TypeError, match="exact Decimal"):
        native_demo(1_000.0)
    with pytest.raises(ValueError, match="positive"):
        native_demo(Decimal("0"))
