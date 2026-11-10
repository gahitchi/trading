"""No-network, synthetic-only risk evidence source acceptance tests."""

import json
from copy import deepcopy

import pytest

from lab.binance_usdm_margin_rules import source_sha256
from lab.binance_usdm_risk_bundle import (
    KIND,
    load_offline_risk_bundle,
    validate_offline_risk_bundle,
)
from tests.test_binance_usdm_margin_rules import START, bars, brackets


def position(*, mark="600"):
    return {
        "symbol": "BTCUSDT",
        "positionSide": "BOTH",
        "positionAmt": "-2",
        "entryPrice": "600",
        "markPrice": mark,
        "unRealizedProfit": str(2 * (600 - float(mark))),
        "isolatedWallet": "300",
        "marginAsset": "USDT",
        "updateTime": START,
    }


def bundle():
    opening = START + 10_000
    current = START + 3 * 60_000
    captured = current - 5_000
    config = {"symbol": "BTCUSDT", "marginType": "ISOLATED", "leverage": 10, "isAutoAddMargin": False}
    sources = {
        "opening_position": position(),
        "current_position": position(),
        "opening_brackets": brackets(),
        "current_brackets": brackets(),
        "opening_config": deepcopy(config),
        "current_config": deepcopy(config),
        "opening_asset_mode": {"multiAssetsMargin": False},
        "current_asset_mode": {"multiAssetsMargin": False},
        "opening_position_mode": {"dualSidePosition": False},
        "current_position_mode": {"dualSidePosition": False},
        "marks": {"endpoint": "/fapi/v1/markPriceKlines", "symbol": "BTCUSDT", "candles": bars()},
    }
    captures = {key: opening if key.startswith("opening_") else captured for key in sources}
    captures["marks"] = current
    return {
        "version": 1,
        "kind": KIND,
        "environment": "demo",
        "symbol": "BTCUSDT",
        "entry_minute_start_ms": START,
        "assessment_ms": current,
        "captures": captures,
        "sources": sources,
        "costs": {"funding_debit_reserve": "2", "close_fee_bps": "5", "buffer_bps": "5"},
    }


def check(b):
    return validate_offline_risk_bundle(b, expected_sha256=source_sha256(b))


def test_healthy_two_point_offline_snapshot_returns_unqualified_risk():
    b = bundle()
    result = check(b)
    assert result.risk.status == "NO_MARK_ENVELOPE_BREACH_OBSERVED_UNQUALIFIED"
    assert not result.source_authenticated and not result.execution_authorized
    assert not result.continuous_account_state_verified and not result.mark_price_stream_authenticated
    assert result.two_point_snapshot_consistent and result.environment == "demo"
    assert result.source_hashes["marks"] == source_sha256(b["sources"]["marks"])


def test_spike_in_mark_candles_is_reported_without_claiming_liquidation():
    b = bundle()
    b["sources"]["marks"]["candles"][1][2] = "800"
    result = check(b)
    assert result.risk.status == "MARK_ENVELOPE_BREACH_POSSIBLE"
    assert result.risk.execution_authorized is False


def test_external_pinned_digest_detects_rewritten_bundle():
    b = bundle()
    sha = source_sha256(b)
    b["sources"]["current_position"]["isolatedWallet"] = "900"
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        validate_offline_risk_bundle(b, expected_sha256=sha)


def test_missing_endpoint_or_credential_field_rejected():
    b = bundle()
    b["sources"].pop("marks")
    with pytest.raises(ValueError, match="incomplete account/mark"):
        check(b)
    b = bundle()
    b["sources"]["opening_position"]["API_key"] = "secret"
    with pytest.raises(ValueError, match="credential or secret"):
        check(b)


def test_changed_short_quantity_or_entry_price_rejected():
    for key, value in (("positionAmt", "-3"), ("entryPrice", "590")):
        b = bundle()
        b["sources"]["current_position"][key] = value
        if key == "positionAmt":
            b["sources"]["current_position"]["unRealizedProfit"] = "0"
        else:
            b["sources"]["current_position"]["unRealizedProfit"] = "-20"
        with pytest.raises(ValueError, match="quantity, entry, or isolated wallet changed"):
            check(b)


def test_changed_isolated_wallet_or_update_time_rejected():
    b = bundle()
    b["sources"]["current_position"]["isolatedWallet"] = "310"
    with pytest.raises(ValueError, match="isolated wallet changed"):
        check(b)
    b = bundle()
    b["sources"]["current_position"]["updateTime"] += 1
    with pytest.raises(ValueError, match="position update time changed"):
        check(b)


def test_leverage_mode_auto_add_hedge_or_multi_asset_rejected():
    changes = (
        ("current_config", "leverage", 11, "leverage changed"),
        ("opening_config", "marginType", "CROSSED", "unsupported margin mode"),
        ("current_config", "isAutoAddMargin", True, "auto-add isolated"),
        ("opening_position_mode", "dualSidePosition", True, "unsupported hedge"),
        ("current_asset_mode", "multiAssetsMargin", True, "multi-assets mode"),
    )
    for source, field, value, error in changes:
        b = bundle()
        b["sources"][source][field] = value
        with pytest.raises(ValueError, match=error):
            check(b)


def test_leverage_bracket_snapshot_changed_rejected():
    b = bundle()
    b["sources"]["current_brackets"]["brackets"][1]["maintMarginRatio"] = "0.011"
    with pytest.raises(ValueError, match="notional brackets differ"):
        check(b)


def test_wrong_mark_symbol_or_ordinary_last_kline_endpoint_rejected():
    for field, value in (("symbol", "ETHUSDT"), ("endpoint", "/fapi/v1/klines")):
        b = bundle()
        b["sources"]["marks"][field] = value
        with pytest.raises(ValueError, match="mark-price endpoint or symbol mismatch"):
            check(b)


def test_position_mark_must_match_observed_candle_envelope():
    b = bundle()
    b["sources"]["current_position"]["markPrice"] = "650"
    b["sources"]["current_position"]["unRealizedProfit"] = "-100.0"
    with pytest.raises(ValueError, match="inconsistent with observed mark candle"):
        check(b)


def test_position_unrealized_pnl_disagreement_rejected():
    b = bundle()
    b["sources"]["current_position"]["unRealizedProfit"] = "100"
    with pytest.raises(ValueError, match="unrealized PnL disagrees"):
        check(b)


def test_opening_snapshot_cannot_be_delayed_past_entry_minute():
    b = bundle()
    b["captures"]["opening_position"] = START + 60_000
    with pytest.raises(ValueError, match="opening account capture"):
        check(b)


def test_current_snapshot_cannot_be_stale_or_from_future():
    b = bundle()
    b["captures"]["current_position"] = START + 60_000
    with pytest.raises(ValueError, match="stale or after assessment"):
        check(b)
    b = bundle()
    b["captures"]["marks"] = b["assessment_ms"] + 1
    with pytest.raises(ValueError, match="stale or after assessment"):
        check(b)


def test_collection_span_must_be_bounded_and_aligned():
    b = bundle()
    b["assessment_ms"] = START + 17 * 60_000
    with pytest.raises(ValueError, match="1m-to-15m"):
        check(b)
    b = bundle()
    b["entry_minute_start_ms"] += 1
    with pytest.raises(ValueError, match="1m-to-15m"):
        check(b)


def test_bounded_file_loader_rejects_duplicate_json_keys(tmp_path):
    path = tmp_path / "risk.json"
    path.write_text('{"symbol":"BTCUSDT","symbol":"BTCUSDT"}')
    with pytest.raises(ValueError, match="malformed, duplicate"):
        load_offline_risk_bundle(path, expected_sha256="a" * 64)


def test_bounded_file_loader_rejects_symlinks_nonfiles_and_oversize(tmp_path):
    path = tmp_path / "risk.json"
    path.write_text(json.dumps(bundle()))
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        load_offline_risk_bundle(alias, expected_sha256="a" * 64)
    with pytest.raises(ValueError, match="regular file"):
        load_offline_risk_bundle(tmp_path, expected_sha256="a" * 64)
    path.write_bytes(b"0" * 2_097_153)
    with pytest.raises(ValueError, match="bounded regular file"):
        load_offline_risk_bundle(path, expected_sha256="a" * 64)


def test_bounded_file_loader_round_trip_leaves_source_unchanged(tmp_path):
    b = bundle()
    original = deepcopy(b)
    path = tmp_path / "risk.json"
    path.write_text(json.dumps(b))
    report = load_offline_risk_bundle(path, expected_sha256=source_sha256(b))
    assert report.risk.current_maintenance > 0
    assert b == original
    assert json.loads(path.read_text()) == original


def test_unknown_environment_and_missing_account_mode_refused():
    b = bundle()
    b["environment"] = "unknown"
    with pytest.raises(ValueError, match="unknown risk snapshot environment"):
        check(b)
    b = bundle()
    del b["sources"]["current_asset_mode"]
    with pytest.raises(ValueError, match="incomplete account/mark"):
        check(b)


def test_mark_candles_not_yet_closed_at_collection_time_are_refused():
    b = bundle()
    b["captures"]["marks"] = b["assessment_ms"] - 1_000
    with pytest.raises(ValueError, match="captured before their final completed close"):
        check(b)


def test_incomplete_last_candle_rejected_without_index_errors():
    b = bundle()
    b["sources"]["marks"]["candles"][-1] = [START]
    with pytest.raises(ValueError, match="missing or incomplete mark-price"):
        check(b)
