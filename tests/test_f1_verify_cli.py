"""Standalone, no-network, no-write F1 accounting verifier acceptance tests."""

import json
import sqlite3
from decimal import Decimal

from lab.economic_store import EconomicEventStore
from lab.engine_events import FillFact
from lab.f1_verify import run


def D(value):
    return Decimal(value)


def cash_activity():
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "event_id": "fund",
            "record_id": "fund-001",
            "timestamp_ns": 10,
            "kind": "CAPITAL_IN",
            "source_kind": "BROKER_CASH_ACTIVITY",
            "settlement_status": "SETTLED",
            "currency": "USD",
            "amount": "1000",
        },
        sort_keys=True,
    ).encode()


def report(*, cash="799.75", positions=None, as_of_ns=30, account_id="SIM-001"):
    if positions is None:
        positions = [{"symbol": "SPY", "quantity": "2"}]
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": account_id,
            "record_id": "full-account-001",
            "as_of_ns": as_of_ns,
            "source_kind": "BROKER_ACCOUNT_STATEMENT",
            "currency": "USD",
            "cash": cash,
            "positions": positions,
            "complete_instrument_scope": True,
        },
        sort_keys=True,
    ).encode()


def fixture(tmp_path):
    path = tmp_path / "economic.db"
    store = EconomicEventStore(path, "SIM-001")
    store.append_cash_record(cash_activity(), source_namespace="broker")
    store.append(FillFact("fill-1", "execution:fill-1", 20, "SPY", "BUY", D("2"), D("100"), D("0.25")))
    statement_path = tmp_path / "external-account.json"
    statement_path.write_bytes(report())
    args = [
        "--journal",
        str(path),
        "--account",
        "SIM-001",
        "--statement",
        str(statement_path),
        "--source",
        "broker",
    ]
    return path, statement_path, args


def test_verifier_reads_existing_journal_and_reports_exact_machine_parity(tmp_path, capsys):
    path, _, args = fixture(tmp_path)
    before = path.read_bytes()
    assert run(args) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "MATCH"
    assert response["events_replayed"] == 2
    assert response["cash_delta_usd"] == "0"
    assert response["position_deltas"] == [{"symbol": "SPY", "quantity_delta": "0"}]
    assert response["independent_projection_matches"]
    assert not response["broker_source_authenticated"]
    assert not response["release_authorized"]
    assert not response["execution_authorized"]
    assert before == path.read_bytes()


def test_mismatch_is_nonzero_exit_and_never_mutates_cash(tmp_path, capsys):
    path, report_path, args = fixture(tmp_path)
    before = EconomicEventStore(path, "SIM-001", readonly=True).load()
    report_path.write_bytes(report(cash="800.75"))
    assert run(args) == 2
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "MISMATCH"
    assert response["cash_delta_usd"] == "1.00"
    after = EconomicEventStore(path, "SIM-001", readonly=True).load()
    assert before == after


def test_missing_database_or_missing_statement_cannot_be_created_by_inspector(tmp_path, capsys):
    missing = tmp_path / "do-not-create.db"
    statement = tmp_path / "account.json"
    statement.write_bytes(report())
    args = [
        "--journal",
        str(missing),
        "--account",
        "SIM-001",
        "--statement",
        str(statement),
        "--source",
        "broker",
    ]
    assert run(args) == 3
    assert not missing.exists()
    assert not (tmp_path / "do-not-create.db-wal").exists()
    assert "refused" in capsys.readouterr().err


def test_readonly_sqlite_mode_rejects_write_sql_and_append(tmp_path):
    from lab.economic_store import EconomicStoreError

    path, _, _ = fixture(tmp_path)
    store = EconomicEventStore(path, "SIM-001", readonly=True)
    with store._connect() as db:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        import pytest

        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM economic_events")
    import pytest

    with pytest.raises(EconomicStoreError, match="read-only"):
        store.append(FillFact("extra", "source:extra", 30, "SPY", "SELL", D("1"), D("90"), D("0")))


def test_corrupt_or_stale_statement_is_refused_without_auto_repair(tmp_path, capsys):
    path, statement, args = fixture(tmp_path)
    statement.write_bytes(report(as_of_ns=11))
    assert run(args) == 3
    assert "stale" in capsys.readouterr().err
    statement.write_bytes(report(cash="NaN"))
    assert run(args) == 3
    assert "refused" in capsys.readouterr().err
    assert EconomicEventStore(path, "SIM-001", readonly=True).load().ledger.position("SPY") == D("2")


def test_replay_readonly_rejects_corrupt_database_history(tmp_path, capsys):
    path, _, args = fixture(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE economic_events SET evidence_head='forged' WHERE seq=2")
    assert run(args) == 3
    assert "refused" in capsys.readouterr().err


def test_cli_never_requires_nautilus_native_objects(tmp_path, capsys):
    _, _, args = fixture(tmp_path)
    assert run(args) == 0
    assert capsys.readouterr().out.startswith('{"account_id":')


def broker_receipt():
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "record_id": "receipt-001",
            "event_id": "broker-fill-001",
            "timestamp_ns": 20,
            "kind": "BROKER_FILL",
            "status": "EXECUTED",
            "source_kind": "BROKER_EXECUTION_REPORT",
            "broker_order_id": "order-001",
            "symbol": "SPY",
            "side": "BUY",
            "quote_currency": "USD",
            "commission_currency": "USD",
            "quantity": "2",
            "price": "100",
            "commission": "0.25",
        },
        sort_keys=True,
    ).encode()


def order_report(executed="2", status="FILLED", remaining="0"):
    return json.dumps(
        {
            "schema_version": 1,
            "account_id": "SIM-001",
            "record_id": "orders-001",
            "as_of_ns": 30,
            "source_kind": "BROKER_ORDER_STATEMENT",
            "complete_order_scope": True,
            "orders": [
                {
                    "broker_order_id": "order-001",
                    "symbol": "SPY",
                    "side": "BUY",
                    "status": status,
                    "requested_quantity": "2",
                    "executed_quantity": executed,
                    "remaining_quantity": remaining,
                }
            ],
        },
        sort_keys=True,
    ).encode()


def test_broker_fill_journal_requires_independent_order_scope(tmp_path, capsys):
    path = tmp_path / "broker.db"
    store = EconomicEventStore(path, "SIM-001")
    store.append_cash_record(cash_activity(), source_namespace="broker")
    store.append_broker_fill_record(broker_receipt(), source_namespace="broker")
    statement = tmp_path / "account.json"
    statement.write_bytes(report())
    args = [
        "--journal",
        str(path),
        "--account",
        "SIM-001",
        "--statement",
        str(statement),
        "--source",
        "broker",
    ]
    assert run(args) == 3
    assert "order report" in capsys.readouterr().err
    assert store.load().ledger.position("SPY") == D("2")

    orders = tmp_path / "orders.json"
    orders.write_bytes(order_report())
    assert run([*args, "--orders", str(orders)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["order_parity_checked"] is True
    assert output["broker_fills_checked"] == 1
    assert output["order_deltas"][0]["quantity_delta"] == "0"
    assert output["broker_source_authenticated"] is False
    assert output["execution_authorized"] is False

    orders.write_bytes(order_report(executed="1.5", status="PARTIALLY_FILLED", remaining="0.5"))
    assert run([*args, "--orders", str(orders)]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "MISMATCH"
    assert output["order_deltas"][0]["quantity_delta"] == "-0.5"
    assert store.load().ledger.position("SPY") == D("2")


def test_malformed_external_order_scope_never_authorizes_numerical_parity(tmp_path, capsys):
    path = tmp_path / "broker.db"
    store = EconomicEventStore(path, "SIM-001")
    store.append_cash_record(cash_activity(), source_namespace="broker")
    store.append_broker_fill_record(broker_receipt(), source_namespace="broker")
    statement = tmp_path / "account.json"
    statement.write_bytes(report())
    orders = tmp_path / "orders.json"
    orders.write_bytes(order_report(status="CANCELED", remaining="1"))
    args = [
        "--journal",
        str(path),
        "--account",
        "SIM-001",
        "--statement",
        str(statement),
        "--source",
        "broker",
        "--orders",
        str(orders),
    ]
    assert run(args) == 3
    assert "refused" in capsys.readouterr().err
