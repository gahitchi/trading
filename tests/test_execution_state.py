"""Reject corrupt persisted authority before recovery; preserve legacy migration."""

import pytest

from lab.execution_state import validate_ledger


@pytest.mark.parametrize(
    "bad",
    [
        {"cash": True},
        {"cash": "1000"},
        {"cash": float("inf")},
        {"cash": 1, "version": True},
        {"cash": 1, "synced_cash": "false"},
        {"cash": 1, "positions": []},
        {"cash": 1, "positions": {"A": True}},
        {"cash": 1, "pending": []},
        {"cash": 1, "disposed": {"X": {"ticker": "A", "side": "BUY", "qty": 1}}},
        {"cash": 1, "terminal": {"X": {"ticker": "A", "side": "BUY", "qty": -1}}},
        {"cash": 1, "applied_trades": ["DUP", "DUP"]},
        {"cash": 1, "applied_trades": [["T", -1, "X"]]},
        {"cash": 1, "applied_trades": [["T", 1, []]]},
        {"cash": 1, "daily_caps": {"orders": 0.5}},
        {"cash": 1, "daily_caps": {"opened": -1}},
    ],
)
def test_corrupt_ledger_is_rejected(bad):
    with pytest.raises(ValueError):
        validate_ledger(bad)


def test_legacy_ledger_and_current_archive_are_supported():
    validate_ledger({"cash": 1000, "positions": {"A": -4}, "applied_trades": ["legacy"]})
    validate_ledger(
        {
            "version": 2,
            "cash": 1000,
            "terminal": {"X": {"ticker": "A", "side": "BUY", "qty": 4}},
            "applied_trades": [["T", 123, "X"]],
        }
    )
