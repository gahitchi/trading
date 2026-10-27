"""The read-only capture for the supervised IBKR session (C004): it records, never trades, never grades."""

import asyncio
import json
import re

import pytest

from lab import capture
from lab.signals.registry import SignalSpec
from lab.sim.clients import sim_clients
from lab.sim.harness import PAPER_ACCOUNT, LiveHarness

D0 = 556


def test_snapshot_records_the_broker_view_without_placing_orders(goog_lab, goog, tmp_path, tmp_path_factory):
    h = LiveHarness(goog_lab, [SignalSpec("buy_and_hold")], {"GOOG": goog}, tmp_path / "h", first_day=D0)
    h.session()
    before = json.dumps(h.broker().state["orders"], sort_keys=True)
    assert h.broker().positions()  # a filled order and a position to observe
    out = tmp_path_factory.mktemp("evidence")
    broker = h.broker()
    d = asyncio.run(
        capture.snapshot_async(h.cfg, ["GOOG"], out, "B03-daily", wait_s=3, clients=sim_clients(h.market, broker, {}))
    )
    assert json.dumps(h.broker().state["orders"], sort_keys=True) == before  # nothing sent, changed or cancelled
    files = {p.name for p in d.iterdir()}
    assert {
        "identity.json",
        "CAPTURE_STATUS.json",
        "broker_account.json",
        "broker_positions.json",
        "broker_orders.json",
        "broker_bars.json",
        "broker_capture_meta.json",
        "NOTE.json",
    } <= files
    orders = json.loads((d / "broker_orders.json").read_text())
    assert orders and any(e["type"] == "OrderFilled" and e.get("trade_id") for o in orders for e in o["events"])
    bars = json.loads((d / "broker_bars.json").read_text())
    assert bars and {"ts_event_utc", "ts_init_utc", "received_utc", "lab_normalized_ts_event_utc"} <= set(bars[0])
    assert json.loads((d / "broker_positions.json").read_text())["GOOG"]["net"] > 0
    exact = json.loads((d / "broker_positions.json").read_text())["GOOG"]["net_exact"]
    assert exact == "1" or float(exact) > 0  # decimal serialization, not a lossy float-only net
    capture_meta = json.loads((d / "broker_capture_meta.json").read_text())
    assert 0 < capture_meta["capture_started_ns"] <= capture_meta["capture_completed_ns"]
    assert capture_meta["coverage_verified"] is False
    assert capture_meta["broker_authentication_verified"] is False
    assert PAPER_ACCOUNT in (d / "broker_account.json").read_text()  # the raw original keeps the real ID
    status = json.loads((d / "CAPTURE_STATUS.json").read_text())
    assert status == {
        "state": "COMPLETE_UNVERIFIED",
        "broker_authenticated": False,
        "history_coverage_verified": False,
    }
    note = json.loads((d / "NOTE.json").read_text())["note"]
    assert "not a pass" in note
    ident = json.loads((d / "identity.json").read_text())
    assert ident["speed"] == "daily" and ident["data_delay_min"] == goog_lab.trading.data_delay_min

    # The runtime records of the same phase, without the private signing key.
    (h.runtime / "authority.key").write_bytes(b"x" * 32)
    r = capture.bundle(h.cfg, out, "B03-daily")
    assert any(p.name.startswith("journal-") for p in r.iterdir()) and not (r / "authority.key").exists()

    # Sanitized for the repository: no account number survives, pseudonyms agree across files.
    clean = tmp_path_factory.mktemp("sanitized")
    key = tmp_path_factory.mktemp("key") / "pseudonym.key"
    manifest = capture.sanitize(out / "B03-daily", clean, key)
    text = "".join(p.read_text() for p in clean.rglob("*.json*") if p.name != "MANIFEST.json")
    assert PAPER_ACCOUNT not in text
    pseudo = capture._pseudo(key.read_bytes(), "ACCT", PAPER_ACCOUNT)
    assert pseudo in (next(clean.rglob("broker_account.json"))).read_text()
    assert all(e.get("original_sha256") for e in manifest["files"]) and manifest["reviewer_attestation"] is None
    # Same key, same pseudonym: joins across phases hold.
    again = tmp_path_factory.mktemp("again")
    capture.sanitize(out / "B03-daily", again, key)
    assert pseudo in (next(again.rglob("broker_account.json"))).read_text()


def test_broker_connect_timeout_quarantines_partial_snapshot_without_authority(goog_lab, tmp_path, monkeypatch):
    from lab import paper

    def reject(*_args, **_kwargs):
        raise RuntimeError("data-connect timeout")

    monkeypatch.setattr(paper, "_base_node", reject)
    location = tmp_path / "private-failed-b01"
    with pytest.raises(RuntimeError, match="data-connect timeout"):
        capture.snapshot(goog_lab, ["GOOG"], location, "B01-readonly")
    captured = list((location / "B01-readonly").glob("snapshot-*"))
    assert len(captured) == 1
    folder = captured[0]
    status = json.loads((folder / "CAPTURE_STATUS.json").read_text())
    assert status["state"] == "INCOMPLETE"
    assert status["broker_authenticated"] is False
    assert status["history_coverage_verified"] is False
    assert (folder / "identity.json").is_file()
    assert not (folder / "NOTE.json").exists()
    assert not (folder / "broker_account.json").exists()


def test_capture_cli_handles_native_broker_startup_timeout_privately(monkeypatch, capsys, tmp_path):
    from types import SimpleNamespace

    from lab import config
    from tools import capture_ib_session

    monkeypatch.setattr(config, "load_config", lambda _path: SimpleNamespace(universe={"GOOG": "XNAS"}))

    def reject(*_args, **_kwargs):
        raise RuntimeError("data-connect timeout: account IB-DUR987654")

    monkeypatch.setattr(capture, "snapshot", reject)
    result = capture_ib_session.main(["snapshot", "B01-test", "--out", str(tmp_path), "--tickers", "GOOG"])
    output = capsys.readouterr()
    assert result == 2
    assert "broker data client did not become ready" in output.err
    assert "IB-DUR987654" not in output.out + output.err
    assert not output.out


def test_read_only_capture_preflight_redacts_broker_account_error(monkeypatch, capsys, tmp_path):
    from types import SimpleNamespace

    from lab import config
    from lab.safety import SafetyError
    from tools import capture_ib_session

    monkeypatch.setattr(config, "load_config", lambda _path: SimpleNamespace(universe={"GOOG": "XNAS"}))

    def refuse(*_args, **_kwargs):
        raise SafetyError("account IB-DUR987654 is not an IB paper account")

    monkeypatch.setattr(capture, "snapshot", refuse)
    status = capture_ib_session.main(["snapshot", "B01-test", "--out", str(tmp_path), "--tickers", "GOOG"])
    response = capsys.readouterr()
    assert status == 2
    assert "capture refused: missing or invalid PAPER account" in response.err
    assert "IB-DUR987654" not in response.err
    assert not response.out


def test_invalid_paper_account_creates_no_partial_capture(goog_lab, tmp_path):
    from dataclasses import replace

    from lab.safety import SafetyError

    cfg = replace(goog_lab, paper=replace(goog_lab.paper, account_id=""))
    output = tmp_path / "invalid-account-capture"
    with pytest.raises(SafetyError):
        capture.snapshot(cfg, ["GOOG"], output, "B01-readonly")
    assert not output.exists()


def test_raw_captures_never_go_inside_the_repository(goog_lab):
    with pytest.raises(ValueError, match="inside the repository"):
        capture.check_outside_repo(goog_lab, goog_lab.root / "evidence")


def test_secrets_are_redacted_and_ids_pseudonymized():
    key = b"k" * 32
    raw = {
        "password": "hunter2",
        "account_id": "IB-DU7654321",
        "note": "account DU7654321 at IBKR",
        "client_order_id": "abc-1",
        "nested": [{"trade_id": "T9", "token": "t"}],
    }
    out = capture._redact(key, raw)
    assert out["password"] == "[REDACTED]" and out["nested"][0]["token"] == "[REDACTED]"
    assert "DU7654321" not in json.dumps(out)
    assert out["account_id"] == capture._pseudo(key, "ACCT", "DU7654321") == out["note"].split()[1]
    assert out["client_order_id"].startswith("CLIENT-") and out["nested"][0]["trade_id"].startswith("TRADE-")


def test_bundle_exports_the_authoritative_sqlite_ledger_and_sanitize_pseudonymizes_it(
    goog_lab, goog, tmp_path, tmp_path_factory
):
    """ChatGPT C004-F1/F2/F3: the SQLite store is authoritative, so the shareable evidence needs a reviewable,
    pseudonymized export of it; the copy must be cold (runtime lock) and consistent (backup API)."""
    from lab.paper import acquire_runtime_lock

    h = LiveHarness(goog_lab, [SignalSpec("buy_and_hold")], {"GOOG": goog}, tmp_path / "h", first_day=D0)
    h.session()
    out = tmp_path_factory.mktemp("evidence")

    held = acquire_runtime_lock(h.cfg)  # a running paper node holds this lock
    try:
        with pytest.raises(Exception, match="another paper node"):
            capture.bundle(h.cfg, out, "B03")
    finally:
        held.close()

    d = capture.bundle(h.cfg, out, "B03")
    export = json.loads((d / "ledger.sqlite3.export.json").read_text())
    assert export["integrity"] == "ok" and {"snapshot", "history"} <= set(export["tables"])
    history = export["tables"]["history"]
    assert history["row_count"] == len(history["rows"]) > 0
    assert any(r["bucket"] == "applied" and r["trade_id"] for r in history["rows"])
    assert (d / "ledger.sqlite3").exists()  # the raw copy stays in the private store

    clean = tmp_path_factory.mktemp("clean")
    manifest = capture.sanitize(out / "B03", clean, tmp_path_factory.mktemp("k") / "key")
    assert manifest["manual_review_required"] is True and manifest["reviewer_attestation"] is None
    assert not list(clean.rglob("*.sqlite3"))  # raw databases are never copied
    text = "".join(p.read_text() for p in clean.rglob("*.json"))
    raw_ids = list(h.broker().state["orders"]) + [f["trade_id"] for f in h.broker().state["fills"]]
    # A raw trade ID can be as short as "T2". A substring check incorrectly
    # fails after 20:00 UTC because ISO timestamps contain "T20:...".
    residuals = [
        identifier
        for identifier in raw_ids
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(identifier) + r"(?![A-Za-z0-9])", text)
    ]
    assert raw_ids and not residuals  # broker IDs are not present as complete identifiers
    assert "CLIENT-" in text and "TRADE-" in text


def test_sanitize_replaces_short_cross_file_broker_ids_without_touching_unrelated_tokens(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "orders.json").write_text(json.dumps({"trade_id": "T2", "client_order_id": "co-1"}))
    (raw / "notes.json").write_text(
        json.dumps({"reason": "late fill T2 after dispose co-1", "control": "XT2 must stay"})
    )
    clean = tmp_path / "clean"
    capture.sanitize(raw, clean, tmp_path / "pseudonym.key")
    typed = json.loads((clean / "orders.json").read_text())
    notes = json.loads((clean / "notes.json").read_text())
    assert typed["trade_id"].startswith("TRADE-")
    assert typed["client_order_id"].startswith("CLIENT-")
    assert typed["trade_id"] in notes["reason"]
    assert typed["client_order_id"] in notes["reason"]
    assert "T2" not in notes["reason"]
    assert notes["control"] == "XT2 must stay"
