"""Sequential offline demo export diagnostics, no exchange connectivity."""

import json
from copy import deepcopy
from decimal import Decimal

import pytest

from lab.binance_demo_continuity import compare_demo_export_continuity
from tests.test_binance_demo_export import T, T_MS, bundle

MS = 1_000_000


def saved(tmp_path, *, update=None):
    before = bundle()
    after = deepcopy(before)
    after["collected_start_ns"] = T + 10_000_000_000
    after["collected_end_ns"] = T + 13_000_000_000
    after["income_end_ms"] = T_MS + 10_000
    after["income"].append(
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "0.0300",
            "asset": "USDT",
            "time": T_MS + 6000,
            "tranId": 111,
        }
    )
    after["balances"][0]["balance"] = "1000.03"
    after["account"]["assets"][0]["walletBalance"] = "1000.03"
    after["account"]["totalWalletBalance"] = "1000.03"
    after["account"]["totalMarginBalance"] = "1001.28"
    if update:
        update(before, after)
    previous = tmp_path / "previous.json"
    current = tmp_path / "current.json"
    previous.write_text(json.dumps(before), encoding="utf-8")
    current.write_text(json.dumps(after), encoding="utf-8")
    return previous, current


def test_clean_income_arithmetic_does_not_certify_account_cash(tmp_path):
    older, newer = saved(tmp_path)
    output = compare_demo_export_continuity(older, newer)
    assert output["status"] == "two_demo_exports_compared_unverified"
    assert output["historical_income_rows_compared"] == 2
    assert output["interior_income_claim_count"] == 1
    assert output["income_interior_first_ms"] == T_MS + 3001
    assert output["income_interior_last_ms"] == T_MS + 9999
    assert output["wallet_balance_delta_by_asset"] == {"USDT": "0.03000000"}
    assert output["interior_income_claims_by_type_asset"] == {"FUNDING_FEE:USDT": "0.0300"}
    assert output["wallet_delta_minus_interior_income_by_asset"]["USDT"] == "0E-8"
    assert output["wallet_income_claims_arithmetically_equal"] is True
    assert len(output["previous_source_sha256"]) == 64
    assert len(output["current_source_sha256"]) == 64
    assert output["previous_source_sha256"] != output["current_source_sha256"]
    for field in (
        "provider_identity_authenticated",
        "income_coverage_independently_verified",
        "account_observations_atomic",
        "account_cash_settlement_reconciled",
        "nautilus_simulator_account_matched",
        "equity_reconciled",
        "funding_payment_verified",
        "margin_qualified",
        "risk_ready",
        "economic_posting_allowed",
    ):
        assert output[field] is False


def test_balance_income_residual_is_flagged_not_hidden(tmp_path):
    def change(_old, new):
        new["balances"][0]["balance"] = "1000.07"
        new["account"]["assets"][0]["walletBalance"] = "1000.07"
        new["account"]["totalWalletBalance"] = "1000.07"
        new["account"]["totalMarginBalance"] = "1001.32"

    old, new = saved(tmp_path, update=change)
    r = compare_demo_export_continuity(old, new)
    assert r["wallet_income_claims_arithmetically_equal"] is False
    assert Decimal(r["wallet_delta_minus_interior_income_by_asset"]["USDT"]) == Decimal("0.04")
    assert r["economic_posting_allowed"] is False


def test_positions_and_order_changes_are_diagnostic_not_fills(tmp_path):
    def change(_old, new):
        new["account"]["positions"][0]["positionAmt"] = "-0.30"
        new["position_risk"][0]["positionAmt"] = "-0.30"
        new["open_orders"][0]["executedQty"] = "0.20"
        new["open_orders"].append(
            {
                "symbol": "ETHUSDT", "clientOrderId": "co-1", "status": "NEW",
                "side": "SELL", "origQty": "1", "executedQty": "0",
            }
        )

    a, b = saved(tmp_path, update=change)
    result = compare_demo_export_continuity(a, b)
    assert result["position_quantity_delta_by_symbol"] == {"BTCUSDT": "-0.05"}
    assert result["common_order_remaining_delta"] == {"BTCUSDT:co-1": "-0.050"}
    assert result["orders_added"] == ["ETHUSDT:co-1"]
    assert result["orders_removed"] == []
    assert not result["risk_ready"]


def test_removed_order_does_not_infer_successful_fill(tmp_path):
    a, b = saved(tmp_path, update=lambda old, new: new.update(open_orders=[]))
    result = compare_demo_export_continuity(a, b)
    assert result["orders_removed"] == ["BTCUSDT:co-1"]
    assert result["position_quantity_delta_by_symbol"] == {}
    assert not result["funding_payment_verified"]


def test_overlapping_income_transaction_must_remain_unchanged(tmp_path):
    def changed(_old, new):
        new["income"][0]["income"] = "-0.02400"

    a, b = saved(tmp_path, update=changed)
    with pytest.raises(ValueError, match="restated"):
        compare_demo_export_continuity(a, b)


def test_previous_income_missing_inside_new_query_window_blocks(tmp_path):
    def changed(_old, new):
        new["income"] = new["income"][2:]

    a, b = saved(tmp_path, update=changed)
    with pytest.raises(ValueError, match="previously observed income missing"):
        compare_demo_export_continuity(a, b)


def test_previous_rows_outside_new_query_range_need_not_be_present(tmp_path):
    def change(_old, new):
        new["income_start_ms"] = T_MS + 3000
        new["income"] = new["income"][2:]

    a, b = saved(tmp_path, update=change)
    out = compare_demo_export_continuity(a, b)
    assert out["historical_income_rows_compared"] == 0
    assert out["interior_income_claim_count"] == 1
    assert not out["income_coverage_independently_verified"]


def test_insufficient_later_income_window_is_rejected(tmp_path):
    def change(_old, new):
        new["income_start_ms"] = T_MS + 3002
        new["income"] = new["income"][2:]

    a, b = saved(tmp_path, update=change)
    with pytest.raises(ValueError, match="full between-observation interval"):
        compare_demo_export_continuity(a, b)


def test_partial_last_income_coverage_fails_closed(tmp_path):
    def change(_old, new):
        new["income_end_ms"] = T_MS + 9998

    a, b = saved(tmp_path, update=change)
    with pytest.raises(ValueError, match="full between-observation interval"):
        compare_demo_export_continuity(a, b)


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        (lambda _old, new: new.update(session_id="another-session"), "same session"),
        (lambda _old, new: new["balances"][0].update(accountAlias="other-alias"), "account alias changed"),
        (lambda _old, new: new.update(collected_start_ns=T + 2_000_000_000), "intervals overlap"),
        (lambda _old, new: new.update(collected_start_ns=T + 3_000_500_000), "millisecond-separated"),
    ],
)
def test_wrong_account_or_nonsequential_observations_rejected(tmp_path, update, reason):
    a, b = saved(tmp_path, update=update)
    with pytest.raises(ValueError, match=reason):
        compare_demo_export_continuity(a, b)


def test_duplicated_exact_export_does_not_count_as_two_observations(tmp_path):
    a, b = saved(tmp_path)
    b.write_bytes(a.read_bytes())
    with pytest.raises(ValueError, match="identical export"):
        compare_demo_export_continuity(a, b)


def test_credentials_and_duplicate_json_in_either_snapshot_fail(tmp_path):
    a, b = saved(tmp_path)
    d = json.loads(a.read_text())
    d["balances"][0]["apiKey"] = "do-not-keep"
    a.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ValueError, match="credential"):
        compare_demo_export_continuity(a, b)
    a, b = saved(tmp_path)
    b.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate-key"):
        compare_demo_export_continuity(a, b)


def test_boundary_income_is_excluded_from_safe_interior(tmp_path):
    def add_boundary(_old, new):
        new["income"].extend(
            [
                {
                    "symbol": "BTCUSDT", "incomeType": "FUNDING_FEE", "income": "1.0",
                    "asset": "USDT", "time": T_MS + 3000, "tranId": 211,
                },
                {
                    "symbol": "BTCUSDT", "incomeType": "FUNDING_FEE", "income": "2.0",
                    "asset": "USDT", "time": T_MS + 10000, "tranId": 212,
                },
            ]
        )

    a, b = saved(tmp_path, update=add_boundary)
    result = compare_demo_export_continuity(a, b)
    assert result["interior_income_claim_count"] == 1
    assert result["excluded_income_claim_count"] == 4
    assert result["interior_income_claims_by_type_asset"] == {"FUNDING_FEE:USDT": "0.0300"}
    assert result["economic_posting_allowed"] is False


def test_no_source_bytes_are_modified_by_comparison(tmp_path):
    a, b = saved(tmp_path)
    saved_a, saved_b = a.read_bytes(), b.read_bytes()
    compare_demo_export_continuity(a, b)
    assert (a.read_bytes(), b.read_bytes()) == (saved_a, saved_b)
