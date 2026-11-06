"""Offline two-export wallet delta diagnostics: no account or P&L authorization."""

import json
from copy import deepcopy
from decimal import Decimal

import pytest

from lab.binance_demo_wallet_audit import compare_demo_wallet_exports
from tests.test_binance_demo_export import T_MS, T, bundle

NS_MS = 1_000_000
SECOND = 1_000_000_000
BEFORE_END = T + 3 * SECOND
AFTER_START = T + 100 * SECOND


def files(tmp_path, *, gap_income="-0.0500", wallet="999.95000000", extra_income=None):
    first = bundle()
    second = bundle()
    second["collected_start_ns"] = AFTER_START
    second["collected_end_ns"] = AFTER_START + 3 * SECOND
    second["income_start_ms"] = BEFORE_END // NS_MS
    second["income_end_ms"] = AFTER_START // NS_MS
    second["income"] = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "COMMISSION",
            "income": gap_income,
            "asset": "USDT",
            "time": T_MS + 30_000,
            "tranId": 200,
        }
    ]
    if extra_income is not None:
        second["income"].append(extra_income)
    second["account"]["totalWalletBalance"] = wallet
    second["account"]["totalMarginBalance"] = str(Decimal(wallet) + Decimal("1.25"))
    second["account"]["assets"][0]["walletBalance"] = wallet
    second["balances"][0]["balance"] = wallet
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    tmp_path.mkdir(parents=True, exist_ok=True)
    before.write_text(json.dumps(first), encoding="utf-8")
    after.write_text(json.dumps(second), encoding="utf-8")
    return before, after, first, second


def test_exact_income_claims_and_wallet_delta_are_diagnostic_only(tmp_path):
    before, after, _first, _second = files(tmp_path)
    earlier = before.read_bytes()
    later = after.read_bytes()
    report = compare_demo_wallet_exports(before, after)
    assert report["status"] == "wallet_delta_consistent_unverified"
    assert report["currency_wallet_delta"] == {"USDT": "-0.05000000"}
    assert report["currency_income_claims"] == {"USDT": "-0.0500"}
    assert report["currency_unexplained_residual"] == {"USDT": "0.00000000"}
    assert report["wallet_delta_matches_claimed_income"] is True
    assert report["income_claim_count"] == 1
    assert before.read_bytes() == earlier and after.read_bytes() == later
    for flag in (
        "provider_identity_authenticated",
        "income_coverage_independently_verified",
        "account_snapshot_atomic",
        "actual_cash_settlements_verified",
        "equity_reconciled",
        "margin_qualified",
        "risk_ready",
        "economic_posting_allowed",
    ):
        assert report[flag] is False


def test_unexplained_loss_must_not_be_silently_filled_with_profit(tmp_path):
    before, after, _first, _second = files(tmp_path, wallet="999.9000")
    report = compare_demo_wallet_exports(before, after)
    assert report["status"] == "wallet_delta_unexplained"
    assert report["wallet_delta_matches_claimed_income"] is False
    assert Decimal(report["currency_unexplained_residual"]["USDT"]) == Decimal("-0.0500")
    assert not report["economic_posting_allowed"]


def test_unexplained_gain_must_not_be_booked_as_realized_pnl(tmp_path):
    before, after, _first, _second = files(tmp_path, wallet="1000.0100")
    report = compare_demo_wallet_exports(before, after)
    assert Decimal(report["currency_unexplained_residual"]["USDT"]) == Decimal("0.0600")
    assert not report["economic_posting_allowed"]


def test_positive_and_negative_income_by_category_and_currency(tmp_path):
    before, after, _first, _second = files(
        tmp_path,
        wallet="999.9750",
        extra_income={
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "0.0250",
            "asset": "USDT",
            "time": T_MS + 40_000,
            "tranId": 201,
        },
    )
    report = compare_demo_wallet_exports(before, after)
    assert report["wallet_delta_matches_claimed_income"] is True
    assert report["income_claim_count"] == 2
    assert report["currency_income_claims"]["USDT"] == "-0.0250"
    assert not report["actual_cash_settlements_verified"]


def test_unknown_income_types_reported_but_never_promoted(tmp_path):
    before, after, _first, _second = files(
        tmp_path,
        wallet="999.9500",
        extra_income={
            "symbol": "",
            "incomeType": "UNCLASSIFIED",
            "income": "0.00",
            "asset": "USDT",
            "time": T_MS + 40_000,
            "tranId": 201,
        },
    )
    report = compare_demo_wallet_exports(before, after)
    assert report["unknown_income_types"] == ["UNCLASSIFIED"]
    assert report["wallet_delta_matches_claimed_income"]
    assert not report["equity_reconciled"]


@pytest.mark.parametrize(
    "change",
    [
        "different-session",
        "different-alias",
        "overlap",
        "coverage-start-gap",
        "coverage-end-gap",
        "lost-asset",
    ],
)
def test_invalid_alignment_or_incomplete_wallet_coverage_fails(tmp_path, change):
    before, after, first, second = files(tmp_path)
    if change == "different-session":
        second["session_id"] = "different-demo"
    elif change == "different-alias":
        second["balances"][0]["accountAlias"] = "DIFFERENT"
    elif change == "overlap":
        second["collected_start_ns"] = first["collected_end_ns"]
        second["collected_end_ns"] = second["collected_start_ns"] + SECOND
        second["income_end_ms"] = second["collected_start_ns"] // NS_MS
        second["income"] = []
    elif change == "coverage-start-gap":
        second["income_start_ms"] = first["collected_end_ns"] // NS_MS + 1
    elif change == "coverage-end-gap":
        second["income_end_ms"] = second["collected_start_ns"] // NS_MS - 1
        second["income"] = []
    elif change == "lost-asset":
        second["account"]["assets"].append({"asset": "BNB", "walletBalance": "5.0"})
        second["balances"].append(
            {
                "accountAlias": "SgsR",
                "asset": "BNB",
                "balance": "5.0",
                "crossWalletBalance": "5.0",
                "crossUnPnl": "0",
                "availableBalance": "5.0",
            }
        )
    after.write_text(json.dumps(second), encoding="utf-8")
    with pytest.raises(ValueError):
        compare_demo_wallet_exports(before, after)


def test_repeated_identical_export_is_not_second_observation(tmp_path):
    before, _after, _first, _second = files(tmp_path)
    with pytest.raises(ValueError, match="repeated"):
        compare_demo_wallet_exports(before, before)


@pytest.mark.parametrize(
    "event_ms",
    [
        T_MS + 1_000,  # prior account collection interval
        T_MS + 101_000,  # later account collection interval
    ],
)
def test_income_inside_nonatomic_collection_window_is_ambiguous(tmp_path, event_ms):
    before, after, _first, second = files(tmp_path)
    second["income_end_ms"] = T_MS + 103_000
    second["income"][0]["time"] = event_ms
    if event_ms == T_MS + 1_000:
        second["income_start_ms"] = T_MS
    after.write_text(json.dumps(second), encoding="utf-8")
    with pytest.raises(ValueError, match="non-atomic"):
        compare_demo_wallet_exports(before, after)


def test_overlapping_report_restates_old_transaction_and_must_fail(tmp_path):
    before, after, first, second = files(tmp_path)
    old = deepcopy(first["income"][0])
    old["income"] = "-0.0600"
    second["income_start_ms"] = first["income_start_ms"]
    second["income"].append(old)
    after.write_text(json.dumps(second), encoding="utf-8")
    with pytest.raises(ValueError, match="restated"):
        compare_demo_wallet_exports(before, after)


def test_unrelated_earlier_income_overlap_is_not_double_counted(tmp_path):
    before, after, first, second = files(tmp_path)
    second["income_start_ms"] = first["income_start_ms"]
    second["income"].extend(deepcopy(first["income"]))
    after.write_text(json.dumps(second), encoding="utf-8")
    report = compare_demo_wallet_exports(before, after)
    assert report["income_claim_count"] == 1
    assert report["currency_income_claims"]["USDT"] == "-0.0500"


def test_outside_wallet_asset_is_blocked(tmp_path):
    before, after, _first, second = files(tmp_path)
    second["income"].append(
        {
            "symbol": "",
            "incomeType": "TRANSFER",
            "income": "1.00",
            "asset": "BNB",
            "time": T_MS + 40_000,
            "tranId": 999,
        }
    )
    after.write_text(json.dumps(second), encoding="utf-8")
    with pytest.raises(ValueError, match="currency has no matching wallet"):
        compare_demo_wallet_exports(before, after)


def test_input_is_path_not_nested_arbitrary_object(tmp_path):
    before, after, *_ = files(tmp_path)
    with pytest.raises((TypeError, ValueError)):
        compare_demo_wallet_exports(before, {"after": after})

def test_demo_wallet_cli_is_read_only_and_never_posts_pnl(tmp_path, capsys):
    pytest.importorskip("pandas")
    from lab.cli import build_parser, cmd_demo_wallet_audit

    before, after, _first, _second = files(tmp_path)
    originals = {path: path.read_bytes() for path in (before, after)}
    args = build_parser().parse_args(
        [
            "demo-wallet-audit",
            "--before",
            str(before),
            "--after",
            str(after),
        ]
    )
    assert cmd_demo_wallet_audit(None, args) == 0
    out = capsys.readouterr().out
    assert '"economic_posting_allowed": false' in out
    assert '"risk_ready": false' in out
    assert '"wallet_delta_matches_claimed_income": true' in out
    assert "UNVERIFIED WALLET DELTA" in out
    assert originals == {path: path.read_bytes() for path in originals}


def test_demo_wallet_cli_denies_missing_file_without_creating_it(tmp_path):
    pytest.importorskip("pandas")
    from lab.cli import build_parser, cmd_demo_wallet_audit

    before, _after, _first, _second = files(tmp_path)
    missing = tmp_path / "not-created.json"
    args = build_parser().parse_args(
        [
            "demo-wallet-audit",
            "--before",
            str(before),
            "--after",
            str(missing),
        ]
    )
    with pytest.raises(SystemExit, match="BLOCKED"):
        cmd_demo_wallet_audit(None, args)
    assert not missing.exists()


def test_demo_wallet_cli_reports_residual_without_fabricated_income(tmp_path, capsys):
    pytest.importorskip("pandas")
    from lab.cli import build_parser, cmd_demo_wallet_audit

    before, after, _first, _second = files(tmp_path, wallet="1000.0100")
    args = build_parser().parse_args(
        [
            "demo-wallet-audit",
            "--before",
            str(before),
            "--after",
            str(after),
        ]
    )
    assert cmd_demo_wallet_audit(None, args) == 0
    out = capsys.readouterr().out
    assert '"wallet_delta_matches_claimed_income": false' in out
    assert '"economic_posting_allowed": false' in out
    assert "wallet_delta_unexplained" in out
