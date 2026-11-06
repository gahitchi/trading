"""Offline Binance demo GET export parsing; no credentials or network."""

import json
from copy import deepcopy

import pytest

from lab.binance_demo_export import inspect_demo_export, normalize_demo_export

T = 1_800_000_000_000_000_000
M = 1_000_000
T_MS = T // M
HASH = "a" * 64


def bundle():
    return {
        "schema_version": 1,
        "source": "binance-usdm-demo-manual-export",
        "scope": "all-symbols-all-income-types",
        "session_id": "demo-2026",
        "collected_start_ns": T,
        "collected_end_ns": T + 3_000_000_000,
        "income_start_ms": T_MS - 3600_000,
        "income_end_ms": T_MS,
        "account": {
            "multiAssetsMargin": False,
            "totalWalletBalance": "1000.00000000",
            "totalUnrealizedProfit": "1.25000000",
            "totalMarginBalance": "1001.25000000",
            "totalInitialMargin": "25.00",
            "totalMaintMargin": "10.00",
            "assets": [
                {"asset": "USDT", "walletBalance": "1000.00000000"}
            ],
            "positions": [
                {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "-0.25"},
                {"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "0"}
            ],
        },
        "balances": [
            {
                "accountAlias": "SgsR",
                "asset": "USDT",
                "balance": "1000.00000000",
                "crossWalletBalance": "970.00",
                "crossUnPnl": "1.25000000",
                "availableBalance": "950.00",
            }
        ],
        "position_risk": [
            {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "-0.250"},
        ],
        "open_orders": [
            {
                "symbol": "BTCUSDT",
                "clientOrderId": "co-1",
                "status": "PARTIALLY_FILLED",
                "side": "BUY",
                "origQty": "0.400",
                "executedQty": "0.150",
            },
        ],
        "income": [
            {
                "symbol": "BTCUSDT",
                "incomeType": "FUNDING_FEE",
                "income": "-0.02300",
                "asset": "USDT",
                "time": T_MS - 2000,
                "tranId": 100,
            },
            {
                "symbol": "BTCUSDT",
                "incomeType": "COMMISSION",
                "income": "-0.0500",
                "asset": "USDT",
                "time": T_MS - 1000,
                "tranId": 101,
            },
        ],
    }


def write_bundle(tmp_path, data=None):
    p = tmp_path / "demo.json"
    p.write_text(json.dumps(bundle() if data is None else data), encoding="utf-8")
    return p


def test_complete_provider_export_does_not_become_account_authority(tmp_path):
    path = write_bundle(tmp_path)
    report = inspect_demo_export(path)
    assert report["status"] == "demo_account_export_consistent_unverified"
    assert report["open_positions"] == {"BTCUSDT": "-0.250"}
    assert report["open_orders"] == {"co-1": {"symbol": "BTCUSDT", "remaining": "0.250"}}
    assert report["wallet_balances"] == {"USDT": "1000.00000000"}
    assert report["income_claims_by_type_currency"] == {
        "COMMISSION:USDT": "-0.0500",
        "FUNDING_FEE:USDT": "-0.02300",
    }
    assert report["account_alias_sha256"] != "SgsR"
    for field in (
        "provider_identity_authenticated",
        "independent_collection_verified",
        "complete_income_coverage_verified",
        "account_state_atomic",
        "nautilus_simulator_account_matched",
        "equity_reconciled",
        "margin_qualified",
        "risk_ready",
        "economic_posting_allowed",
    ):
        assert report[field] is False
    assert path.read_bytes() == json.dumps(bundle()).encode()


def test_numeric_strings_remain_exact_and_report_is_not_posted():
    report = normalize_demo_export(bundle(), source_sha256=HASH)
    assert report["source_sha256"] == HASH
    assert report["open_positions"]["BTCUSDT"] == "-0.250"
    assert not report["risk_ready"]
    assert not report["economic_posting_allowed"]


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("source", "binance-usdm-live", "provenance"),
        ("scope", "btc-only", "scope"),
        ("schema_version", True, "schema version"),
        ("schema_version", 3, "schema version"),
        ("session_id", "../../secrets", "session"),
        ("collected_start_ns", T + 8_000_000_000, "collection window"),
        ("collected_end_ns", T + 40_000_000_000, "collection window"),
        ("income_start_ms", T_MS + 3, "income query window"),
        ("income_end_ms", T_MS + 4000, "unobserved future"),
        ("income", "bad", "complete array"),
        ("balances", {}, "complete array"),
        ("position_risk", {}, "complete array"),
        ("open_orders", {}, "complete array"),
    ],
)
def test_invalid_export_scope_or_clock_fails_closed(key, value, message):
    d = bundle()
    d[key] = value
    with pytest.raises(ValueError, match=message):
        normalize_demo_export(d, source_sha256=HASH)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("multiAssetsMargin", True, "unsupported"),
        ("totalWalletBalance", "NaN", "bounded exact"),
        ("totalWalletBalance", 1000.0, "bounded exact"),
        ("totalWalletBalance", "1e1000", "bounded exact"),
        ("totalWalletBalance", "1000.02", "wallet plus unrealized"),
        ("totalMaintMargin", "-1", "invalid demo account"),
        ("assets", [], "missing USDT wallet"),
        ("positions", "missing", "complete array"),
    ],
)
def test_account_total_or_mode_disagreements_fail_closed(field, value, message):
    d = bundle()
    d["account"][field] = value
    with pytest.raises(ValueError, match=message):
        normalize_demo_export(d, source_sha256=HASH)


def test_mismatched_or_duplicate_assets_cannot_be_silent():
    d = bundle()
    d["balances"][0]["balance"] = "999.99"
    with pytest.raises(ValueError, match="disagreement"):
        normalize_demo_export(d, source_sha256=HASH)
    d = bundle()
    d["balances"].append(deepcopy(d["balances"][0]))
    with pytest.raises(ValueError, match="duplicate demo account balance"):
        normalize_demo_export(d, source_sha256=HASH)


@pytest.mark.parametrize(
    ("where", "row", "message"),
    [
        ("position_risk", {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "0.1"}, "disagree"),
        ("position_risk", {"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "0.25"}, "hedged"),
        ("position_risk", {"symbol": "BTCUSDC", "positionSide": "BOTH", "positionAmt": "0.1"}, "unsupported"),
        ("open_orders", {"symbol": "BTCUSDT", "clientOrderId": "co-2", "status": "FILLED", "side": "BUY", "origQty": "0.4", "executedQty": "0.1"}, "nonterminal"),
        ("open_orders", {"symbol": "BTCUSDT", "clientOrderId": "co-2", "status": "NEW", "side": "BUY", "origQty": "0.4", "executedQty": "0.6"}, "remaining"),
    ],
)
def test_position_and_order_state_fail_closed(where, row, message):
    d = bundle()
    d[where] = [row]
    with pytest.raises(ValueError, match=message):
        normalize_demo_export(d, source_sha256=HASH)


def test_duplicate_orders_block():
    d = bundle()
    d["open_orders"].append(deepcopy(d["open_orders"][0]))
    with pytest.raises(ValueError, match="duplicate demo client order"):
        normalize_demo_export(d, source_sha256=HASH)


def test_income_types_all_preserved_including_transfers_without_funding_inference():
    d = bundle()
    d["income"].append({
        "symbol": "",
        "incomeType": "UNEXPECTED_REBATE",
        "income": "0.010",
        "asset": "USDT",
        "time": T_MS - 3000,
        "tranId": 104,
    })
    result = normalize_demo_export(d, source_sha256=HASH)
    assert result["unknown_income_type_count"] == 1
    assert result["income_claims_by_type_currency"]["UNEXPECTED_REBATE:USDT"] == "0.010"
    assert not result["complete_income_coverage_verified"]


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"tranId": -1}, "transaction ID"),
        ({"time": T_MS + 100}, "outside requested coverage"),
        ({"income": 0.1}, "bounded exact"),
        ({"incomeType": ""}, "income type"),
        ({"asset": "usd"}, "asset"),
        ({"symbol": "BTCUSDC"}, "non-USDT"),
    ],
)
def test_malformed_income_entries_rejected(updates, message):
    d = bundle()
    d["income"][0].update(updates)
    with pytest.raises(ValueError, match=message):
        normalize_demo_export(d, source_sha256=HASH)


def test_duplicate_income_id_and_full_page_refused():
    d = bundle()
    d["income"].append(deepcopy(d["income"][0]))
    with pytest.raises(ValueError, match="duplicate demo income transaction"):
        normalize_demo_export(d, source_sha256=HASH)
    d = bundle()
    d["income"] = [
        {**d["income"][0], "tranId": i} for i in range(1000)
    ]
    with pytest.raises(ValueError, match="page limit"):
        normalize_demo_export(d, source_sha256=HASH)


def test_raw_credentials_must_never_be_accepted(tmp_path):
    for key in ("signature", "apiKey", "api_secret", "Authorization", "X-MBX-APIKEY"):
        d = bundle()
        d["account"][key] = "private"
        with pytest.raises(ValueError, match="credential|schema"):
            inspect_demo_export(write_bundle(tmp_path, d))


def test_duplicate_keys_nonfinite_json_and_symlinks_block(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(b'{"schema_version":1,"schema_version":2}')
    with pytest.raises(ValueError, match="duplicate-key"):
        inspect_demo_export(path)
    path.write_bytes(b'{"x":NaN}')
    with pytest.raises(ValueError, match="nonfinite"):
        inspect_demo_export(path)
    normal = write_bundle(tmp_path)
    link = tmp_path / "alias.json"
    link.symlink_to(normal)
    with pytest.raises(ValueError, match="symlink"):
        inspect_demo_export(link)


def test_oversize_and_nonobject_root_fail_closed(tmp_path):
    path = tmp_path / "too-big.json"
    path.write_bytes(b"x" * 4_194_305)
    with pytest.raises(ValueError, match="bounded"):
        inspect_demo_export(path)
    path.write_bytes(b"[]")
    with pytest.raises(ValueError, match="schema mismatch"):
        inspect_demo_export(path)

def test_demo_import_cli_is_offline_read_only(tmp_path, monkeypatch, capsys):
    pytest.importorskip("pandas")
    from lab.cli import build_parser, cmd_binance_demo_export

    p = write_bundle(tmp_path)
    before = p.read_bytes()
    args = build_parser().parse_args(["demo-account-import", "--file", str(p)])
    assert cmd_binance_demo_export(None, args) == 0
    displayed = capsys.readouterr().out
    assert '"risk_ready": false' in displayed
    assert '"nautilus_simulator_account_matched": false' in displayed
    assert "UNVERIFIED DEMO SOURCE" in displayed
    assert p.read_bytes() == before


def test_demo_import_cli_fails_for_missing_report_without_creating_it(tmp_path):
    pytest.importorskip("pandas")
    from lab.cli import build_parser, cmd_binance_demo_export

    missing = tmp_path / "not-created.json"
    args = build_parser().parse_args(["demo-account-import", "--file", str(missing)])
    with pytest.raises(SystemExit, match="BLOCKED"):
        cmd_binance_demo_export(None, args)
    assert not missing.exists()
