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
        "broker_account.json",
        "broker_positions.json",
        "broker_orders.json",
        "broker_bars.json",
        "NOTE.json",
    } <= files
    orders = json.loads((d / "broker_orders.json").read_text())
    assert orders and any(e["type"] == "OrderFilled" and e.get("trade_id") for o in orders for e in o["events"])
    bars = json.loads((d / "broker_bars.json").read_text())
    assert bars and {"ts_event_utc", "ts_init_utc", "received_utc", "lab_normalized_ts_event_utc"} <= set(bars[0])
    assert json.loads((d / "broker_positions.json").read_text())["GOOG"]["net"] > 0
    assert PAPER_ACCOUNT in (d / "broker_account.json").read_text()  # the raw original keeps the real ID
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
    # The identifier must not survive as an independent token; an ISO
    # timestamp like 2026-10-26T20:00 contains "T2" as a harmless substring.
    leaked = [i for i in raw_ids if re.search(r"(?<![A-Za-z0-9])" + re.escape(i) + r"(?![A-Za-z0-9])", text)]
    assert raw_ids and not leaked  # IDs remain scrubbed without corrupting timestamps
    assert "CLIENT-" in text and "TRADE-" in text


def test_sanitizer_pseudonymizes_trade_ids_mentioned_in_other_files_free_text(tmp_path):
    """An ID known only from a *later* file must be scrubbed from earlier free text."""
    src, dst = tmp_path / "raw", tmp_path / "clean"
    src.mkdir()
    (src / "01-risk.json").write_text(
        json.dumps({"kind": "fill_recovered", "detail": "A BUY trade T2 filled while offline; order X-123"})
    )
    (src / "02-orders.json").write_text(
        json.dumps(
            {
                "broker": {"trade_id": "T2", "client_order_id": "X-123"},
                "pending": {"X-123": {"ticker": "A", "side": "BUY", "qty": 2}},
            }
        )
    )
    manifest = capture.sanitize(src, dst, tmp_path / "private" / "key")
    risk = json.loads((dst / "01-risk.json").read_text())
    orders = json.loads((dst / "02-orders.json").read_text())
    assert "T2" not in json.dumps(risk)
    assert "X-123" not in json.dumps(risk)
    assert orders["broker"]["trade_id"] in risk["detail"]
    assert orders["broker"]["client_order_id"] in risk["detail"]
    assert manifest["manual_review_required"] and manifest["reviewer_attestation"] is None


def test_sanitizer_handles_overlapping_execution_ids_in_text_and_jsonl(tmp_path):
    src, dst = tmp_path / "raw", tmp_path / "clean"
    src.mkdir()
    (src / "risk.jsonl").write_text(json.dumps({"detail": "Broker reports T23 and T2; ambiguous short IDs"}) + "\n")
    (src / "trades.json").write_text(json.dumps({"rows": [{"trade_id": "T23"}, {"trade_id": "T2"}]}))
    capture.sanitize(src, dst, tmp_path / "private" / "key")
    note = (dst / "risk.jsonl").read_text()
    rows = json.loads((dst / "trades.json").read_text())["rows"]
    assert "T2" not in note and "T23" not in note
    assert rows[0]["trade_id"] in note
    assert rows[1]["trade_id"] in note


def test_trade_redaction_preserves_iso_timestamp_and_hides_exact_id(tmp_path):
    src, dst = tmp_path / "raw", tmp_path / "clean"
    src.mkdir()
    original_timestamp = "2026-10-26T20:15:24Z"
    (src / "events.json").write_text(
        json.dumps(
            {
                "created_at": original_timestamp,
                "detail": "the broker reported trade T2 at " + original_timestamp,
                "fill": {"trade_id": "T2"},
            }
        )
    )
    capture.sanitize(src, dst, tmp_path / "key")
    event = json.loads((dst / "events.json").read_text())
    assert event["created_at"] == original_timestamp
    assert original_timestamp in event["detail"]
    assert event["fill"]["trade_id"] in event["detail"]
    assert not re.search(r"(?<![A-Za-z0-9])T2(?![A-Za-z0-9])", event["detail"])
