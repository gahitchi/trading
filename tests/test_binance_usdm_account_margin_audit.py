"""Offline synthetic Binance demo account-to-maintenance cross-check tests."""

from copy import deepcopy
from decimal import Decimal

import pytest

from lab.binance_usdm_account_margin_audit import audit_demo_account_margin
from lab.binance_usdm_margin_rules import source_sha256
from tests.test_binance_usdm_margin_rules import brackets

ANCHOR = 1_767_225_780_000
PATHS = {
    "position": "/fapi/v3/positionRisk",
    "symbol_config": "/fapi/v1/symbolConfig",
    "position_mode": "/fapi/v1/positionSide/dual",
    "asset_mode": "/fapi/v1/multiAssetsMargin",
    "brackets": "/fapi/v1/leverageBracket",
}


def snapshot():
    responses = {
        "position": [
            {
                "symbol": "BTCUSDT",
                "positionSide": "BOTH",
                "marginAsset": "USDT",
                "positionAmt": "-2",
                "entryPrice": "600",
                "markPrice": "600",
                "unRealizedProfit": "0",
                "isolatedWallet": "300",
                "isolatedMargin": "300",
                "positionInitialMargin": "120",
                "maintMargin": "7",
                "notional": "-1200",
                "updateTime": ANCHOR - 120_000,
            }
        ],
        "symbol_config": [
            {
                "symbol": "BTCUSDT",
                "marginType": "ISOLATED",
                "isAutoAddMargin": False,
                "leverage": 10,
                "maxNotionalValue": "5000",
            }
        ],
        "position_mode": {"dualSidePosition": False},
        "asset_mode": {"multiAssetsMargin": False},
        "brackets": brackets(),
    }
    return {
        "version": 1,
        "environment": "binance-usdm-demo",
        "symbol": "BTCUSDT",
        "collected_start_ms": ANCHOR,
        "collected_end_ms": ANCHOR + 3000,
        "sources": {
            name: {"endpoint": PATHS[name], "captured_ms": ANCHOR + i * 500, "payload": responses[name]}
            for i, name in enumerate(PATHS)
        },
    }


def run(bundle=None, *, expected=None, expected_brackets=None, tolerance="0.01"):
    bundle = snapshot() if bundle is None else bundle
    return audit_demo_account_margin(
        bundle,
        expected_bundle_sha256=expected or source_sha256(bundle),
        expected_bracket_sha256=expected_brackets or source_sha256(bundle["sources"]["brackets"]["payload"]),
        tolerance_usdt=tolerance,
    )


def test_valid_pinned_demo_margin_claim_reconciles_without_authenticating_provider():
    r = run()
    assert r.status == "CONSISTENT_UNAUTHENTICATED_RESEARCH_ONLY"
    assert r.reported_maintenance == r.modeled_maintenance == 7
    assert r.maintenance_bracket == 2
    assert r.current_equity == 300
    assert r.current_maintenance_excess == 293
    assert r.quantity == 2 and r.selected_leverage == 10
    assert not r.provider_authenticated and not r.account_state_atomic
    assert not r.execution_authorized and not r.liquidation_qualified


def test_current_mark_must_reconcile_with_short_unrealized_and_notional():
    b = snapshot()
    pos = b["sources"]["position"]["payload"][0]
    pos["markPrice"] = "620"
    pos["unRealizedProfit"] = "-40"
    pos["notional"] = "-1240"
    pos["maintMargin"] = "7.4"
    pos["positionInitialMargin"] = "124"
    r = run(b)
    assert r.snapshot_mark_price == 620 and r.current_equity == 260
    assert r.modeled_maintenance == r.reported_maintenance == Decimal("7.4")


def test_position_reported_maintenance_cannot_override_tier_math():
    b = snapshot()
    b["sources"]["position"]["payload"][0]["maintMargin"] = "6.0"
    with pytest.raises(ValueError, match="maintenance disagrees"):
        run(b)


def test_short_unrealized_or_notional_mismatch_rejected():
    b = snapshot()
    b["sources"]["position"]["payload"][0]["unRealizedProfit"] = "4"
    with pytest.raises(ValueError, match="unrealized PnL"):
        run(b)
    b = snapshot()
    b["sources"]["position"]["payload"][0]["notional"] = "100"
    with pytest.raises(ValueError, match="notional disagrees"):
        run(b)


def test_cross_or_auto_margin_and_hedge_positions_rejected():
    b = snapshot()
    b["sources"]["symbol_config"]["payload"][0]["marginType"] = "CROSSED"
    with pytest.raises(ValueError, match="isolated or one-way"):
        run(b)
    b = snapshot()
    b["sources"]["symbol_config"]["payload"][0]["isAutoAddMargin"] = True
    with pytest.raises(ValueError, match="isolated or one-way"):
        run(b)
    b = snapshot()
    b["sources"]["position"]["payload"][0]["positionSide"] = "SHORT"
    with pytest.raises(ValueError, match="isolated or one-way"):
        run(b)


def test_position_and_multi_asset_modes_are_independently_mandatory():
    b = snapshot()
    b["sources"]["position_mode"]["payload"] = {"dualSidePosition": True}
    with pytest.raises(ValueError, match="one-way position mode"):
        run(b)
    b = snapshot()
    b["sources"]["asset_mode"]["payload"] = {"multiAssetsMargin": True}
    with pytest.raises(ValueError, match="single-asset mode"):
        run(b)


def test_timestamps_do_not_treat_position_last_update_as_response_age():
    b = snapshot()
    b["sources"]["position"]["payload"][0]["updateTime"] = ANCHOR - 2_000_000
    assert run(b).selected_leverage == 10
    b["sources"]["position"]["payload"][0]["updateTime"] = ANCHOR + 5_000
    with pytest.raises(ValueError, match="last-update"):
        run(b)
    b = snapshot()
    b["sources"]["brackets"]["captured_ms"] = ANCHOR + 20_000
    with pytest.raises(ValueError, match="timestamp mismatch"):
        run(b)


def test_collection_window_and_endpoint_identity_rejected_on_disagreement():
    b = snapshot()
    b["collected_end_ms"] = ANCHOR + 10_000
    with pytest.raises(ValueError, match="collection intervals"):
        run(b)
    b = snapshot()
    b["sources"]["position"]["endpoint"] = "/fapi/v2/positionRisk"
    with pytest.raises(ValueError, match="endpoint identity"):
        run(b)


def test_bundle_and_separate_bracket_hashes_must_match():
    b = snapshot()
    with pytest.raises(ValueError, match="account snapshot SHA-256"):
        run(b, expected="0" * 64)
    with pytest.raises(ValueError, match="bracket SHA-256"):
        run(b, expected_brackets="f" * 64)
    original = source_sha256(b)
    altered = deepcopy(b)
    altered["sources"]["position"]["payload"][0]["markPrice"] = "700"
    with pytest.raises(ValueError, match="account snapshot SHA-256"):
        run(altered, expected=original)


def test_credentials_and_real_account_environment_rejected():
    b = snapshot()
    b["sources"]["position"]["payload"][0]["api_key"] = "fake"
    with pytest.raises(ValueError, match="prohibited credential"):
        run(b)
    b = snapshot()
    b["environment"] = "binance-usdm-mainnet"
    with pytest.raises(ValueError, match="only Binance demo"):
        run(b)


def test_empty_position_long_or_duplicate_symbol_rows_rejected():
    b = snapshot()
    b["sources"]["position"]["payload"][0]["positionAmt"] = "0"
    with pytest.raises(ValueError, match="one live one-way SHORT"):
        run(b)
    b = snapshot()
    b["sources"]["position"]["payload"][0]["positionAmt"] = "2"
    with pytest.raises(ValueError, match="one live one-way SHORT"):
        run(b)
    b = snapshot()
    b["sources"]["position"]["payload"].append(b["sources"]["position"]["payload"][0])
    with pytest.raises(ValueError, match="exactly one"):
        run(b)


def test_leverage_and_max_notional_exceeding_config_rejected():
    b = snapshot()
    b["sources"]["symbol_config"]["payload"][0]["leverage"] = 11
    with pytest.raises(ValueError, match="leverage/notional"):
        run(b)
    b = snapshot()
    b["sources"]["symbol_config"]["payload"][0]["maxNotionalValue"] = "1000"
    with pytest.raises(ValueError, match="leverage/notional"):
        run(b)


def test_tolerance_is_bounded_and_source_remains_unmodified():
    b = snapshot()
    saved = deepcopy(b)
    run(b)
    assert b == saved
    with pytest.raises(ValueError, match="unsupported tolerance"):
        run(b, tolerance="1")


def test_position_initial_margin_must_match_leverage():
    b = snapshot()
    b["sources"]["position"]["payload"][0]["positionInitialMargin"] = "50"
    with pytest.raises(ValueError, match="initial margin disagrees"):
        run(b)


def test_nonpositive_maintenance_excess_is_not_reported_as_consistent():
    b = snapshot()
    b["sources"]["position"]["payload"][0]["isolatedWallet"] = "1"
    result = run(b)
    assert result.current_maintenance_excess == -6
    assert result.status == "MAINTENANCE_EXCESS_NONPOSITIVE_UNQUALIFIED"
    assert not result.execution_authorized
