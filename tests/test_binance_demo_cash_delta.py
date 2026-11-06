"""Pure, read-only cross-restart demo wallet-vs-income reconciliation tests."""

import json
from copy import deepcopy

import pytest

from lab.binance_demo_cash_delta import compare_demo_cash_bundles, compare_demo_cash_files
from tests.test_binance_demo_export import HASH, T, T_MS, bundle


def pair():
    first = bundle()
    later = deepcopy(first)
    later["session_id"] = "demo-next-restart"
    later["collected_start_ns"] = T + 10_000_000_000
    later["collected_end_ns"] = T + 13_000_000_000
    later["income_end_ms"] = T_MS + 13_000
    new_income = [
        ("FUNDING_FEE", "-0.002", 110),
        ("COMMISSION", "-0.003", 111),
        ("REALIZED_PNL", "0.150", 112),
        ("TRANSFER", "-0.100", 113),
    ]
    for kind, amount, txn in new_income:
        later["income"].append({
            "symbol": "" if kind == "TRANSFER" else "BTCUSDT",
            "incomeType": kind,
            "income": amount,
            "asset": "USDT",
            "time": T_MS + 5000,
            "tranId": txn,
        })
    later["account"]["totalWalletBalance"] = "1000.045"
    later["account"]["totalMarginBalance"] = "1001.295"
    later["account"]["assets"][0]["walletBalance"] = "1000.045"
    later["balances"][0]["balance"] = "1000.045"
    return first, later


def compare(first, later, first_hash=HASH, second_hash="b" * 64):
    return compare_demo_cash_bundles(
        first, later, first_sha256=first_hash, second_sha256=second_hash
    )


def test_cross_restart_same_demo_alias_and_exact_cash_movements_match():
    first, second = pair()
    result = compare(first, second)
    assert result["status"] == "demo_wallet_income_delta_match_unverified"
    assert result["wallet_delta_by_currency"] == {"USDT": "0.04500000"}
    assert result["income_movements_by_type_currency"] == {
        "COMMISSION:USDT": "-0.003",
        "FUNDING_FEE:USDT": "-0.002",
        "REALIZED_PNL:USDT": "0.150",
        "TRANSFER:USDT": "-0.100",
    }
    assert result["first_session"] != result["second_session"]
    assert result["account_alias_sha256"]
    for flag in (
        "funding_fee_ledger_independently_authenticated",
        "source_authenticated",
        "income_coverage_verified",
        "cash_movements_complete",
        "snapshots_atomic",
        "nautilus_simulator_account_matched",
        "equity_reconciled",
        "margin_qualified",
        "risk_ready",
        "economic_posting_allowed",
    ):
        assert result[flag] is False


def test_two_existing_files_are_read_only(tmp_path):
    first, later = pair()
    original_path = tmp_path / "first.json"
    later_path = tmp_path / "later.json"
    original_path.write_text(json.dumps(first), encoding="utf-8")
    later_path.write_text(json.dumps(later), encoding="utf-8")
    before = {p: p.read_bytes() for p in (original_path, later_path)}
    output = compare_demo_cash_files(original_path, later_path)
    assert output["wallet_delta_by_currency"] == {"USDT": "0.04500000"}
    assert output["first_export_sha256"] != output["second_export_sha256"]
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda a, b: b["balances"][0].update(accountAlias="other"), "aliases"),
        (lambda a, b: b.update(collected_start_ns=T + 1_000_000_000), "overlap"),
        (lambda a, b: b.update(collected_start_ns=T + 3_000_000_000), "overlap"),
        (lambda a, b: b.update(collected_start_ns=T + 3_500_000_000), "too close"),
        (lambda a, b: a.update(income_end_ms=T_MS + 1000), "non-atomic"),
        (lambda a, b: b.update(income_start_ms=T_MS - 1000), "full earlier covered"),
        (lambda a, b: b.update(income_end_ms=T_MS + 11_000), "does not cover"),
        (lambda a, b: b["income"].pop(0), "historical demo income prefix"),
        (lambda a, b: b["income"][0].update(income="-0.024"), "historical demo income prefix"),
        (lambda a, b: b["income"].append({
            "symbol": "BTCUSDT", "incomeType": "FUNDING_FEE", "income": "0.01",
            "asset": "USDT", "time": T_MS - 1200, "tranId": 999,
        }), "historical demo income prefix"),
        (lambda a, b: b["income"][2].update(time=T_MS + 1000), "non-atomic account observation"),
        (lambda a, b: b["income"][2].update(time=T_MS + 3000), "non-atomic account observation"),
        (lambda a, b: b["income"][2].update(time=T_MS + 10_000), "non-atomic account observation"),
        (lambda a, b: b["income"][2].update(time=T_MS + 13_000), "non-atomic account observation"),
        (lambda a, b: b["income"][2].update(time=T_MS + 14_000), "outside requested coverage"),
        (lambda a, b: b["income"][2].update(income="-0.004"), "balance delta differs"),
        (lambda a, b: b["income"][2].update(incomeType="MYSTERY"), "unrecognized demo income types"),
        (lambda a, b: b["income"].append({
            "symbol": "BTCUSDT", "incomeType": "TRANSFER", "income": "0.015",
            "asset": "BNB", "time": T_MS + 4500, "tranId": 900,
        }), "asset outside"),
        (lambda a, b: b["account"]["assets"].append({
            "asset": "BNB", "walletBalance": "0"
        }) or b["balances"].append({
            "accountAlias": "SgsR", "asset": "BNB",
            "balance": "0", "crossWalletBalance": "0",
            "crossUnPnl": "0", "availableBalance": "0",
        }), "asset set changed"),
    ],
)
def test_fail_closed_on_missing_incomplete_or_ambiguous_movements(mutation, expected):
    first, later = pair()
    mutation(first, later)
    with pytest.raises(ValueError, match=expected):
        compare(first, later)


def test_mismatched_export_digests_fail_closed():
    first, later = pair()
    with pytest.raises(ValueError, match="distinct"):
        compare(first, later, first_hash=HASH, second_hash=HASH)
    with pytest.raises(ValueError, match="digest"):
        compare(first, later, second_hash="invalid")


def test_empty_income_with_identical_balances_is_only_unverified_claim():
    first, later = pair()
    later["income"] = deepcopy(first["income"])
    later["account"]["totalWalletBalance"] = first["account"]["totalWalletBalance"]
    later["account"]["totalMarginBalance"] = first["account"]["totalMarginBalance"]
    later["account"]["assets"][0]["walletBalance"] = first["account"]["assets"][0]["walletBalance"]
    later["balances"][0]["balance"] = first["balances"][0]["balance"]
    result = compare(first, later)
    assert result["wallet_delta_by_currency"] == {"USDT": "0E-8"}
    assert result["income_movements_by_type_currency"] == {}
    assert result["cash_movements_complete"] is False


def test_unrealized_mark_gain_is_not_wallet_income(tmp_path):
    first, later = pair()
    later["account"]["totalUnrealizedProfit"] = "100.00"
    later["account"]["totalMarginBalance"] = "1100.045"
    result = compare(first, later)
    assert result["wallet_delta_by_currency"] == {"USDT": "0.04500000"}
    assert result["equity_reconciled"] is False


def test_wallet_change_without_replayed_income_is_never_silently_profit():
    first, later = pair()
    later["income"] = deepcopy(first["income"])
    with pytest.raises(ValueError, match="balance delta differs"):
        compare(first, later)


def test_file_reader_rejects_symlink_duplicate_keys_and_missing_files(tmp_path):
    first, later = pair()
    p0 = tmp_path / "earlier.json"
    p1 = tmp_path / "later.json"
    p0.write_text(json.dumps(first), encoding="utf-8")
    p1.write_text(json.dumps(later), encoding="utf-8")
    alias = tmp_path / "alias.json"
    alias.symlink_to(p0)
    with pytest.raises(ValueError, match="symlink"):
        compare_demo_cash_files(alias, p1)
    p1.write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate-key"):
        compare_demo_cash_files(p0, p1)
    p1.unlink()
    with pytest.raises(FileNotFoundError):
        compare_demo_cash_files(p0, p1)
