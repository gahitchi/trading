"""Strict, portable fee/funding claim checks; no source authentication or P&L."""

import json
from dataclasses import asdict, replace

import pytest

from lab.paper_account_snapshot import PaperAccountSnapshotJournal
from lab.paper_settlement_claims import (
    ReadOnlySettlementReport,
    compare_settlement_claims,
    load_read_only_settlement,
)
from lab.sandbox_capture import PaperOrderEventCapture
from tests.test_paper_account_snapshot import snapshot
from tests.test_sandbox_capture import IID, NS, event

SESSION = "session-1"
ACCOUNT = "sandbox-1"


def scenario(tmp_path, *, multiple=True, funding=True, commission="0.01 USDT"):
    source = PaperOrderEventCapture(
        tmp_path / "orders.jsonl", session_id=SESSION, instrument_id=IID
    )
    source.append(event(commission=commission), received_ns=NS + 2)
    fees = [{"trade_id": "trade-1", "event_ns": NS, "currency": "USDT", "amount": "0.010"}]
    quantity = "-0.25"
    if multiple:
        source.append(
            event(
                event_id="evt-2",
                trade_id="trade-2",
                client_order_id="CO-2",
                last_qty="0.10",
                ts_event=NS + 3,
                ts_init=NS + 3,
                commission="0.02 USDT",
            ),
            received_ns=NS + 4,
        )
        quantity = "-0.35"
        fees.append({"trade_id": "trade-2", "event_ns": NS + 3, "currency": "USDT", "amount": "0.020"})
    journal = PaperAccountSnapshotJournal(
        tmp_path / "account.jsonl", source, account_id=ACCOUNT
    )
    state = snapshot(source, identifier="snap-1", captured_ns=NS + 10, positions={IID: quantity})
    journal.append(state)
    rows = [{"payment_id": "pay-1", "event_ns": NS + 6, "currency": "USDT", "amount": "-0.0034"}] if funding else []
    report = ReadOnlySettlementReport(
        report_id="settle-1",
        collector_id="separate-collector-1",
        source_kind="separate-readonly-settlement",
        session_id=source.session_id,
        account_id=ACCOUNT,
        instrument_id=IID,
        snapshot_id=state.snapshot_id,
        capture_count=state.capture_count,
        capture_sha256=state.capture_sha256,
        period_start_ns=NS - 20,
        observed_ns=NS + 10,
        complete_fees=True,
        complete_funding=True,
        fee_items=fees,
        funding_items=rows,
    )
    return source, journal, report


def test_match_is_diagnostic_only_even_with_claimed_funding_payment(tmp_path):
    _src, journal, report = scenario(tmp_path)
    result = compare_settlement_claims(journal, report)
    assert result["status"] == "fee_claims_match_funding_unverified"
    assert result["fee_claim_count"] == 2
    assert result["funding_claim_count"] == 1
    assert result["funding_claimed_by_currency"] == {"USDT": "-0.0034"}
    for field in (
        "source_authenticated",
        "collector_independence_verified",
        "funding_payment_verified",
        "funding_coverage_verified",
        "settlement_balances_reconciled",
        "equity_reconciled",
        "margin_qualified",
        "risk_ready",
        "economic_posting_allowed",
    ):
        assert result[field] is False


def test_no_funding_claim_is_not_proof_of_zero_account_funding(tmp_path):
    _src, journal, report = scenario(tmp_path, funding=False)
    result = compare_settlement_claims(journal, report)
    assert result["funding_claim_count"] == 0
    assert result["funding_claimed_by_currency"] == {}
    assert result["funding_coverage_verified"] is False
    assert result["economic_posting_allowed"] is False


def test_comparisons_do_not_write_to_source_or_account(tmp_path):
    source, account, report = scenario(tmp_path)
    previous = {p: p.read_bytes() for p in (source.path, account.path)}
    assert compare_settlement_claims(account, report)["fee_claim_count"] == 2
    assert previous == {p: p.read_bytes() for p in previous}


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("session_id", "other", "scope mismatch"),
        ("account_id", "other", "scope mismatch"),
        ("instrument_id", "OTHER-PERP.BINANCE", "scope mismatch"),
        ("snapshot_id", "not-this", "scope mismatch"),
        ("capture_count", 0, "event prefix"),
        ("capture_sha256", "a" * 64, "event prefix"),
        ("source_kind", "sandbox-cache", "source kind"),
        ("collector_id", "nautilus-cache", "local sandbox"),
        ("collector_id", "cache:snap-1", "reuse local cache"),
        ("complete_fees", False, "complete fee and funding"),
        ("complete_funding", False, "complete fee and funding"),
        ("complete_fees", 1, "must be booleans"),
        ("period_start_ns", NS + 11, "inverted or empty"),
        ("period_start_ns", NS + 1, "coverage interval"),
        ("observed_ns", NS + 10 + 6_000_000_000, "observation differs"),
        ("fee_items", [], "do not match"),
        ("fee_items", [{"trade_id": "other", "event_ns": NS, "currency": "USDT", "amount": "0.01"}], "do not match"),
        ("fee_items", "not-list", "complete lists"),
        ("funding_items", "not-list", "complete lists"),
    ],
)
def test_missing_or_mismatched_claim_blocks(tmp_path, field, value, match):
    _src, journal, report = scenario(tmp_path)
    with pytest.raises(ValueError, match=match):
        compare_settlement_claims(journal, replace(report, **{field: value}))


@pytest.mark.parametrize(
    ("fee", "message"),
    [
        ({"trade_id": "trade-1", "event_ns": NS, "currency": "USDT", "amount": "0.02"}, "do not match"),
        ({"trade_id": "trade-1", "event_ns": NS + 1, "currency": "USDT", "amount": "0.01"}, "do not match"),
        ({"trade_id": "trade-1", "event_ns": NS, "currency": "BNB", "amount": "0.01"}, "do not match"),
        ({"trade_id": "trade-1", "event_ns": NS, "currency": "USDT", "amount": "-0.01"}, "invalid fee amount"),
        ({"trade_id": "trade-1", "event_ns": NS, "currency": "USDT", "amount": 0.01}, "bounded exact decimal string"),
        ({"trade_id": "trade-1", "event_ns": NS, "currency": "USDT", "amount": "NaN"}, "bounded exact decimal string"),
        ({"trade_id": "trade-1", "event_ns": NS, "currency": "USDT", "amount": "1e1000000"}, "bounded exact decimal string"),
        ({"trade_id": "trade-1", "event_ns": NS, "currency": "USDT", "amount": "0.01", "test": True}, "invalid schema"),
        ({"trade_id": "trade-1", "event_ns": NS + 20, "currency": "USDT", "amount": "0.01"}, "future-dated"),
    ],
)
def test_fee_mismatch_or_unbounded_number_rejected(tmp_path, fee, message):
    _src, journal, report = scenario(tmp_path, multiple=False)
    with pytest.raises(ValueError, match=message):
        compare_settlement_claims(journal, replace(report, fee_items=[fee]))


def test_duplicate_fees_or_missing_source_commission_fail_closed(tmp_path):
    _src, journal, report = scenario(tmp_path, multiple=False)
    with pytest.raises(ValueError, match="duplicate settlement fee"):
        compare_settlement_claims(journal, replace(report, fee_items=[*report.fee_items, *report.fee_items]))
    _src, journal, report = scenario(tmp_path / "missing", multiple=False, commission=None)
    with pytest.raises(ValueError, match="no fee evidence"):
        compare_settlement_claims(journal, report)


@pytest.mark.parametrize(
    ("row", "match"),
    [
        ({"payment_id": "pay-1", "event_ns": NS + 11, "currency": "USDT", "amount": "-0.0034"}, "outside covered"),
        ({"payment_id": "pay-1", "event_ns": NS - 21, "currency": "USDT", "amount": "-0.0034"}, "outside covered"),
        ({"payment_id": "pay-1", "event_ns": NS + 6, "currency": "USDT", "amount": "0"}, "zero funding"),
        ({"payment_id": "pay-1", "event_ns": NS + 6, "currency": "USDT", "amount": "NaN"}, "bounded exact decimal"),
        ({"payment_id": "pay-1", "event_ns": NS + 6, "currency": "usd", "amount": "0.01"}, "currency"),
        ({"payment_id": "pay-1", "event_ns": NS + 6, "currency": "USDT", "amount": 0.01}, "bounded exact decimal"),
        ({"payment_id": "pay-1", "event_ns": NS + 6, "currency": "USDT", "amount": "0.01", "extra": 0}, "invalid schema"),
    ],
)
def test_invalid_funding_cash_claims_rejected(tmp_path, row, match):
    _src, journal, report = scenario(tmp_path)
    with pytest.raises(ValueError, match=match):
        compare_settlement_claims(journal, replace(report, funding_items=[row]))


def test_funding_claim_ids_are_unique_and_signed_amounts_do_not_post(tmp_path):
    _src, journal, report = scenario(tmp_path)
    payment = report.funding_items[0]
    with pytest.raises(ValueError, match="duplicate reported funding"):
        compare_settlement_claims(journal, replace(report, funding_items=[payment, payment]))
    more = {"payment_id": "pay-2", "event_ns": NS + 7, "currency": "USDT", "amount": "0.0014"}
    out = compare_settlement_claims(journal, replace(report, funding_items=[payment, more]))
    assert out["funding_claimed_by_currency"] == {"USDT": "-0.0020"}
    assert out["economic_posting_allowed"] is False


def test_tampered_capture_or_missing_account_checkpoint_blocks_comparison(tmp_path):
    source, journal, report = scenario(tmp_path)
    raw = source.path.read_bytes()
    source.path.write_bytes(raw.replace(b"trade-1", b"trade-X"))
    with pytest.raises(ValueError, match="hash mismatch"):
        compare_settlement_claims(journal, report)
    source.path.write_bytes(raw)
    journal._head.unlink()
    with pytest.raises(ValueError, match="checkpoint missing"):
        compare_settlement_claims(journal, report)


def test_report_is_strict_json_and_does_not_authenticate_collector(tmp_path):
    _src, journal, report = scenario(tmp_path)
    path = tmp_path / "fee-funding.json"
    path.write_text(json.dumps(asdict(report)), encoding="utf-8")
    loaded = load_read_only_settlement(path)
    assert loaded == report
    assert compare_settlement_claims(journal, loaded)["source_authenticated"] is False


@pytest.mark.parametrize("blob", [
    "",
    '{"report_id":"abc","report_id":"def"}',
    '{"funding_items":[{"payment_id":"x","payment_id":"y"}]}',
    '{"field":NaN}',
    '{"field":Infinity}',
    '{"bad":"schema"}',
    "[]",
    "{invalid",
])
def test_report_parser_rejects_duplicates_unknown_fields_and_nonfinite_json(tmp_path, blob):
    p = tmp_path / "bad.json"
    p.write_text(blob, encoding="utf-8")
    with pytest.raises(ValueError, match=r"JSON|schema|bounded"):
        load_read_only_settlement(p)


def test_parser_refuses_symlink_and_large_file(tmp_path):
    p = tmp_path / "too-big.json"
    p.write_bytes(b"x" * 1_048_577)
    with pytest.raises(ValueError, match="bounded"):
        load_read_only_settlement(p)
    link = tmp_path / "link.json"
    link.symlink_to(p)
    with pytest.raises(ValueError, match="symlink"):
        load_read_only_settlement(link)

def test_settlement_cli_is_read_only_and_does_not_post_economics(tmp_path, capsys):
    pytest.importorskip("pandas")
    from lab.cli import build_parser, cmd_paper_settlement_check

    source, account, report = scenario(tmp_path)
    raw = tmp_path / "settlement-report.json"
    raw.write_text(json.dumps(asdict(report)), encoding="utf-8")
    before = {p: p.read_bytes() for p in (source.path, account.path, raw)}
    args = build_parser().parse_args([
        "paper-settlement-check",
        "--orders", str(source.path),
        "--snapshots", str(account.path),
        "--report", str(raw),
        "--session", SESSION,
        "--instrument", IID,
        "--account", ACCOUNT,
    ])
    assert cmd_paper_settlement_check(None, args) == 0
    output = capsys.readouterr().out
    assert '"funding_payment_verified": false' in output
    assert '"risk_ready": false' in output
    assert "no authenticated funding" in output
    assert before == {p: p.read_bytes() for p in before}


def test_cli_refuses_missing_archives_without_creating_files(tmp_path):
    pytest.importorskip("pandas")
    from lab.cli import build_parser, cmd_paper_settlement_check

    source, account, report = scenario(tmp_path)
    raw = tmp_path / "settlement-report.json"
    raw.write_text(json.dumps(asdict(report)), encoding="utf-8")
    missing = tmp_path / "missing-account.jsonl"
    cli = [
        "paper-settlement-check",
        "--orders", str(source.path),
        "--snapshots", str(missing),
        "--report", str(raw),
        "--session", SESSION,
        "--instrument", IID,
        "--account", ACCOUNT,
    ]
    with pytest.raises(SystemExit, match="preexisting"):
        cmd_paper_settlement_check(None, build_parser().parse_args(cli))
    assert not missing.exists()
    assert not missing.with_name(missing.name + ".head.json").exists()
    cli[cli.index(str(missing))] = str(account.path)
    account._head.unlink()
    with pytest.raises(SystemExit, match="preexisting"):
        cmd_paper_settlement_check(None, build_parser().parse_args(cli))


def test_cli_blocks_forged_fee_disagreement(tmp_path):
    pytest.importorskip("pandas")
    from lab.cli import build_parser, cmd_paper_settlement_check

    source, account, report = scenario(tmp_path)
    raw = tmp_path / "fake.json"
    raw.write_text(json.dumps(asdict(replace(report, fee_items=[]))), encoding="utf-8")
    args = build_parser().parse_args([
        "paper-settlement-check",
        "--orders", str(source.path),
        "--snapshots", str(account.path),
        "--report", str(raw),
        "--session", SESSION,
        "--instrument", IID,
        "--account", ACCOUNT,
    ])
    with pytest.raises(SystemExit, match="BLOCKED"):
        cmd_paper_settlement_check(None, args)
