"""Portable, fail-closed cross-source paper account claim comparisons."""

import json
from dataclasses import asdict, replace

import pytest

from lab.paper_account_crosscheck import ReadOnlyAccountReport, compare_read_only_report, load_read_only_report
from lab.paper_account_snapshot import PaperAccountSnapshotJournal
from lab.sandbox_capture import PaperOrderEventCapture
from tests.test_paper_account_snapshot import snapshot
from tests.test_sandbox_capture import IID, NS, event


def setup(tmp_path, *, open_order=True):
    capture = PaperOrderEventCapture(
        tmp_path / "orders.jsonl", session_id="paper-session", instrument_id=IID
    )
    capture.append(event(), received_ns=NS + 2)
    orders = {}
    if open_order:
        capture.append(
            event("OrderAccepted", event_id="evt-2", client_order_id="CO-2"),
            received_ns=NS + 3,
        )
        orders = {"CO-2": "0.75"}
    local = snapshot(
        capture,
        identifier="local-1",
        source_id="paper-session:cache:1",
        captured_ns=NS + 10,
        positions={IID: "-0.25"},
        open_orders=orders,
        balances={"USDT": {"total": "1000.00", "available": "990.00", "locked": "10.00"}},
    )
    journal = PaperAccountSnapshotJournal(
        tmp_path / "snapshots.jsonl", capture, account_id="sandbox-1"
    )
    journal.append(local)
    report = ReadOnlyAccountReport(
        report_id="read-only-1",
        collector_id="separate-collector-1",
        source_kind="separate-readonly-report",
        session_id=capture.session_id,
        account_id="sandbox-1",
        instrument_id=IID,
        observed_ns=NS + 10,
        snapshot_id=local.snapshot_id,
        capture_count=local.capture_count,
        capture_sha256=local.capture_sha256,
        complete_positions=True,
        complete_orders=True,
        complete_balances=True,
        positions={IID: "-0.250"},
        open_orders={"CO-2": "0.750"} if open_order else {},
        balances={"USDT": {"total": "1000", "available": "990", "locked": "10"}},
    )
    return capture, journal, report


def test_reconciled_claims_are_diagnostic_only(tmp_path):
    _capture, journal, report = setup(tmp_path)
    result = compare_read_only_report(journal, report)
    assert result["status"] == "match_unverified_source"
    assert result["open_order_claims_match"]
    assert result["cash_balance_claims_match"]
    assert result["position_claims_match"]
    assert result["observation_skew_ns"] == 0
    assert result["source_authenticated"] is False
    assert result["source_independence_established"] is False
    assert result["risk_ready"] is False
    assert result["economic_posting_allowed"] is False


def test_exact_decimal_equivalence_and_zero_position_omission(tmp_path):
    _capture, journal, report = setup(tmp_path, open_order=False)
    assert compare_read_only_report(journal, report)["position_claims_match"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("session_id", "other-session", "scope or snapshot identity"),
        ("account_id", "other-account", "scope or snapshot identity"),
        ("instrument_id", "ETHUSDT-PERP.BINANCE", "scope or snapshot identity"),
        ("snapshot_id", "other-snapshot", "scope or snapshot identity"),
        ("capture_count", 0, "same event prefix"),
        ("capture_sha256", "0" * 64, "same event prefix"),
        ("source_kind", "sandbox-cache", "separate read-only source"),
        ("collector_id", "sandbox-cache", "relabel local cache"),
        ("collector_id", "paper-session:cache:1", "reuse the local cache source"),
        ("complete_orders", False, "complete positions, orders and balances"),
        ("complete_positions", False, "complete positions, orders and balances"),
        ("complete_balances", False, "complete positions, orders and balances"),
        ("complete_positions", 1, "booleans"),
        ("observed_ns", NS - 3_000_000_000, "too far apart"),
        ("positions", {IID: "-0.24"}, "position inventory differs"),
        ("positions", {}, "position inventory differs"),
        ("positions", {IID: "-0.25", "ETHUSDT-PERP.BINANCE": "0.01"}, "out-of-scope"),
        ("positions", {IID: "NaN"}, "finite and valid"),
        ("positions", {IID: -0.25}, "exact decimal string"),
        ("open_orders", {}, "outstanding orders or remaining quantities differ"),
        ("open_orders", {"CO-2": "0.74"}, "outstanding orders or remaining quantities differ"),
        ("open_orders", {"CO-2": "0"}, "must be positive"),
        ("open_orders", {"CO-2": 0.75}, "exact decimal string"),
        ("balances", {"USDT": {"total": "1000", "available": "991", "locked": "10"}}, "balance arithmetic"),
        ("balances", {"USDT": {"total": "1001", "available": "991", "locked": "10"}}, "balance claims differ"),
        ("balances", {"USDT": {"total": "NaN", "available": "990", "locked": "10"}}, "finite and valid"),
        ("balances", {"USDT": {"total": "1000", "available": "990"}}, "total/available/locked"),
        ("balances", {}, "incomplete"),
    ],
)
def test_independent_claim_mismatch_blocks_comparison(tmp_path, field, value, message):
    _capture, journal, report = setup(tmp_path)
    with pytest.raises(ValueError, match=message):
        compare_read_only_report(journal, replace(report, **{field: value}))


def test_skew_boundary_and_absence_of_infinite_override(tmp_path):
    _capture, journal, report = setup(tmp_path)
    assert compare_read_only_report(
        journal, replace(report, observed_ns=NS + 10 + 1_000_000_000)
    )["observation_skew_ns"] == 1_000_000_000
    with pytest.raises(ValueError, match="too far apart"):
        compare_read_only_report(
            journal, replace(report, observed_ns=NS + 10 + 1_000_000_001)
        )
    with pytest.raises(ValueError, match="bounded"):
        compare_read_only_report(journal, report, max_skew_ns=50_000_000_000)
    with pytest.raises(ValueError, match="bounded"):
        compare_read_only_report(journal, report, max_skew_ns=True)


def test_only_latest_replay_verified_snapshot_can_be_compared(tmp_path):
    capture, journal, report = setup(tmp_path)
    capture.append(
        event(event_id="evt-3", client_order_id="CO-3", trade_id="trade-3", order_side="BUY", last_qty="0.1"),
        received_ns=NS + 20,
    )
    next_state = snapshot(
        capture,
        identifier="local-2",
        source_id="paper-session:cache:2",
        captured_ns=NS + 22,
        positions={IID: "-0.15"},
        open_orders={"CO-2": "0.75"},
    )
    journal.append(next_state)
    with pytest.raises(ValueError, match="scope or snapshot identity"):
        compare_read_only_report(journal, report)


def test_receipt_source_integrity_required_on_every_crosscheck(tmp_path):
    capture, journal, report = setup(tmp_path)
    raw = capture.path.read_bytes()
    capture.path.write_bytes(raw.replace(b"trade-1", b"trade-X"))
    with pytest.raises(ValueError, match="hash mismatch"):
        compare_read_only_report(journal, report)


def test_account_checkpoint_integrity_required_on_every_crosscheck(tmp_path):
    _capture, journal, report = setup(tmp_path)
    journal._head.unlink()
    with pytest.raises(ValueError, match="checkpoint missing"):
        compare_read_only_report(journal, report)


def test_two_sources_are_not_automatically_authenticated(tmp_path):
    _capture, journal, report = setup(tmp_path)
    for collector in ("independent-broker-report", "external-account-statement", "research-observer"):
        result = compare_read_only_report(journal, replace(report, collector_id=collector))
        assert not result["source_authenticated"]
        assert not result["source_independence_established"]
        assert not result["risk_ready"]


def test_no_local_snapshot_or_incomplete_local_snapshot_is_not_qualified(tmp_path):
    capture = PaperOrderEventCapture(
        tmp_path / "empty-orders.jsonl", session_id="paper-session", instrument_id=IID
    )
    journal = PaperAccountSnapshotJournal(
        tmp_path / "empty-snapshots.jsonl", capture, account_id="sandbox-1"
    )
    _a, _b, report = setup(tmp_path / "fixture")
    with pytest.raises(ValueError, match="no completed"):
        compare_read_only_report(journal, report)

def test_load_report_is_strict_json_and_does_not_authenticate_source(tmp_path):
    _capture, journal, report = setup(tmp_path)
    path = tmp_path / "read-only.json"
    path.write_text(json.dumps(asdict(report)), encoding="utf-8")
    loaded = load_read_only_report(path)
    assert loaded == report
    result = compare_read_only_report(journal, loaded)
    assert result["status"] == "match_unverified_source"
    assert not result["source_authenticated"]
    assert not result["economic_posting_allowed"]


@pytest.mark.parametrize("contents", [
    '{"report_id":"a","report_id":"b"}',
    '{"source_kind":"separate-readonly-report"}',
    '{"account_id":"NaN","quantity":NaN}',
    '[]',
    '{broken',
    '',
])
def test_external_report_parser_rejects_incomplete_duplicate_or_invalid_json(tmp_path, contents):
    path = tmp_path / "bad.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        load_read_only_report(path)


def test_external_report_reader_rejects_symlinks_and_oversize(tmp_path):
    path = tmp_path / "report.json"
    path.write_bytes(b"x" * 1_048_577)
    with pytest.raises(ValueError, match="bounded regular file"):
        load_read_only_report(path)
    link = tmp_path / "alias.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symbolic link"):
        load_read_only_report(link)


def test_external_report_reader_rejects_duplicate_nested_currency_balance(tmp_path):
    _capture, _journal, report = setup(tmp_path)
    path = tmp_path / "report.json"
    text = json.dumps(asdict(report))
    text = text.replace('"total": "1000"', '"total": "1000", "total": "1000"')
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_read_only_report(path)


def test_read_only_paper_crosscheck_cli(tmp_path, capsys):
    pytest.importorskip("pandas")
    from lab.cli import build_parser, cmd_paper_crosscheck

    capture, journal, report = setup(tmp_path)
    path = tmp_path / "external.json"
    path.write_text(json.dumps(asdict(report)), encoding="utf-8")
    argv = [
        "paper-crosscheck",
        "--orders", str(capture.path),
        "--snapshots", str(journal.path),
        "--external-report", str(path),
        "--session", capture.session_id,
        "--instrument", capture.instrument_id,
        "--account", journal.account_id,
    ]
    args = build_parser().parse_args(argv)
    assert cmd_paper_crosscheck(None, args) == 0
    output = capsys.readouterr().out
    assert "match_unverified_source" in output
    assert "NOT qualified" in output
    assert '"risk_ready": false' in output


def test_paper_crosscheck_cli_blocks_mismatched_report(tmp_path, capsys):
    pytest.importorskip("pandas")
    from lab.cli import build_parser, cmd_paper_crosscheck

    capture, journal, report = setup(tmp_path)
    path = tmp_path / "external.json"
    path.write_text(json.dumps(asdict(replace(report, positions={}))), encoding="utf-8")
    args = build_parser().parse_args([
        "paper-crosscheck",
        "--orders", str(capture.path),
        "--snapshots", str(journal.path),
        "--external-report", str(path),
        "--session", capture.session_id,
        "--instrument", capture.instrument_id,
        "--account", journal.account_id,
    ])
    with pytest.raises(SystemExit, match="BLOCKED"):
        cmd_paper_crosscheck(None, args)

def test_cli_must_not_create_missing_evidence_files(tmp_path):
    pytest.importorskip("pandas")
    from lab.cli import build_parser, cmd_paper_crosscheck

    capture, journal, report = setup(tmp_path)
    external = tmp_path / "external.json"
    external.write_text(json.dumps(asdict(report)), encoding="utf-8")
    missing = tmp_path / "new-account-log.jsonl"
    arguments = [
        "paper-crosscheck",
        "--orders", str(capture.path),
        "--snapshots", str(missing),
        "--external-report", str(external),
        "--session", capture.session_id,
        "--instrument", capture.instrument_id,
        "--account", journal.account_id,
    ]
    with pytest.raises(SystemExit, match="preexisting"):
        cmd_paper_crosscheck(None, build_parser().parse_args(arguments))
    assert not missing.exists()
    assert not missing.with_name(missing.name + ".head.json").exists()
    journal._head.unlink()
    arguments[arguments.index(str(missing))] = str(journal.path)
    with pytest.raises(SystemExit, match="preexisting"):
        cmd_paper_crosscheck(None, build_parser().parse_args(arguments))
    assert not journal._head.exists()
