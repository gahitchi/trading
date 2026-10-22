"""Deterministic complete-snapshot rules; no broker/event-loop involved."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from lab.contracts import (
    ContractError,
    ExpectedRoster,
    MarketBarFinalized,
    SessionKey,
    SignalDisposition,
    SignalEvaluated,
)
from lab.session_snapshot import SessionSnapshotBuilder, SnapshotIncomplete, SnapshotState

SESSION = SessionKey("XNYS", date(2026, 10, 22))
NEXT = SessionKey("XNYS", date(2026, 10, 23))
CUT = datetime(2026, 10, 22, 21, 0, tzinfo=UTC)
OBS = datetime(2026, 10, 22, 20, 5, tzinfo=UTC)
HASH = "a" * 64


def roster():
    return ExpectedRoster(
        session=SESSION,
        instruments=("SPY.ARCX", "QQQ.XNAS"),
        sleeve_instruments=(("s:SPY", "SPY.ARCX"), ("s:QQQ", "QQQ.XNAS")),
    )


def bar(inst: str, **changes):
    data = {
        "session": SESSION,
        "instrument_id": inst,
        "currency": "USD",
        "interval_start_utc": datetime(2026, 10, 22, 13, 30, tzinfo=UTC),
        "interval_end_utc": datetime(2026, 10, 22, 20, 0, tzinfo=UTC),
        "observed_at_utc": OBS,
        "open": Decimal("100"),
        "high": Decimal("102"),
        "low": Decimal("99"),
        "close": Decimal("101"),
        "volume": Decimal("1000000"),
        "provider": "synthetic-unit-test",
        "source_sha256": HASH,
    }
    data.update(changes)
    return MarketBarFinalized(**data)


def signal(sleeve: str, inst: str, **changes):
    data = {
        "session": SESSION,
        "sleeve_id": sleeve,
        "instrument_id": inst,
        "strategy_sha256": HASH,
        "disposition": SignalDisposition.TARGET,
        "target": Decimal("0.5"),
        "input_sha256": HASH,
    }
    data.update(changes)
    return SignalEvaluated(**data)


def complete(builder):
    builder.ingest_bar(bar("SPY.ARCX"))
    builder.ingest_bar(bar("QQQ.XNAS"))
    builder.ingest_signal(signal("s:SPY", "SPY.ARCX"))
    builder.ingest_signal(signal("s:QQQ", "QQQ.XNAS"))


def test_no_seal_for_partial_market_roster():
    b = SessionSnapshotBuilder(roster(), deadline_utc=CUT)
    b.ingest_bar(bar("SPY.ARCX"))
    b.ingest_signal(signal("s:SPY", "SPY.ARCX"))
    assert b.missing_bars == ("QQQ.XNAS",)
    assert b.missing_sleeves == ("s:QQQ",)
    with pytest.raises(SnapshotIncomplete, match="missing final bars"):
        b.seal(now_utc=OBS)
    assert b.state is SnapshotState.COLLECTING


def test_complete_snapshot_is_order_independent_and_immutable():
    a = SessionSnapshotBuilder(roster(), deadline_utc=CUT)
    b = SessionSnapshotBuilder(roster(), deadline_utc=CUT)
    complete(a)
    b.ingest_signal(signal("s:QQQ", "QQQ.XNAS"))
    b.ingest_bar(bar("QQQ.XNAS"))
    b.ingest_bar(bar("SPY.ARCX"))
    b.ingest_signal(signal("s:SPY", "SPY.ARCX"))
    one = a.seal(now_utc=OBS + timedelta(minutes=2))
    two = b.seal(now_utc=OBS + timedelta(minutes=5))
    assert one.digest == two.digest
    assert len(one.digest) == 64
    assert len(one.bars) == 2 and len(one.signals) == 2
    assert a.seal(now_utc=CUT) is one
    with pytest.raises(FrozenInstanceError):
        one.digest = HASH
    with pytest.raises(ContractError, match="terminal"):
        a.ingest_bar(bar("SPY.ARCX"))


def test_identical_duplicate_is_idempotent_but_conflict_poisoned():
    b = SessionSnapshotBuilder(roster(), deadline_utc=CUT)
    assert b.ingest_bar(bar("SPY.ARCX")) is True
    assert b.ingest_bar(bar("SPY.ARCX")) is False
    with pytest.raises(ContractError, match="conflicting"):
        b.ingest_bar(bar("SPY.ARCX", close=Decimal("100.75")))
    assert b.state is SnapshotState.INVALID
    with pytest.raises(ContractError, match="terminal"):
        b.seal(now_utc=OBS)


def test_conflicting_signal_invalidates_session():
    b = SessionSnapshotBuilder(roster(), deadline_utc=CUT)
    assert b.ingest_signal(signal("s:SPY", "SPY.ARCX"))
    assert not b.ingest_signal(signal("s:SPY", "SPY.ARCX"))
    with pytest.raises(ContractError, match="conflicting signal"):
        b.ingest_signal(signal("s:SPY", "SPY.ARCX", target=Decimal("0")))
    assert b.state is SnapshotState.INVALID


@pytest.mark.parametrize(
    "message",
    [
        signal("s:SPY", "SPY.ARCX", session=NEXT),
        signal("s:SPY", "NOT_THE_INSTRUMENT"),
        signal("unknown", "SPY.ARCX"),
        signal("s:SPY", "SPY.ARCX", disposition=SignalDisposition.ERROR, target=None, reason="error"),
        signal(
            "s:SPY",
            "SPY.ARCX",
            disposition=SignalDisposition.NOT_APPLICABLE,
            target=None,
            reason="membership not verified",
        ),
    ],
)
def test_out_of_scope_or_nontrading_signals_fail_closed(message):
    b = SessionSnapshotBuilder(roster(), deadline_utc=CUT)
    with pytest.raises(ContractError):
        b.ingest_signal(message)
    assert b.state is SnapshotState.INVALID


def test_stale_market_date_or_unrostered_instrument_fail_closed():
    for bad in (bar("SPY.ARCX", session=NEXT), bar("NOT_IN_UNIVERSE")):
        b = SessionSnapshotBuilder(roster(), deadline_utc=CUT)
        with pytest.raises(ContractError):
            b.ingest_bar(bad)
        assert b.state is SnapshotState.INVALID


def test_deadline_expiry_cannot_be_bypassed_by_late_data():
    b = SessionSnapshotBuilder(roster(), deadline_utc=CUT)
    assert b.expire(now_utc=CUT - timedelta(seconds=1)) is False
    assert b.expire(now_utc=CUT + timedelta(seconds=1)) is True
    assert b.state is SnapshotState.MISSED
    with pytest.raises(ContractError, match="terminal"):
        b.ingest_bar(bar("SPY.ARCX"))


def test_complete_but_not_sealed_before_deadline_is_still_missed():
    b = SessionSnapshotBuilder(roster(), deadline_utc=CUT)
    complete(b)
    with pytest.raises(ContractError, match="deadline elapsed"):
        b.seal(now_utc=CUT + timedelta(seconds=1))
    assert b.state is SnapshotState.MISSED


def test_cannot_seal_before_data_arrived():
    b = SessionSnapshotBuilder(roster(), deadline_utc=CUT)
    complete(b)
    with pytest.raises(ContractError, match="observed"):
        b.seal(now_utc=OBS - timedelta(seconds=1))
    assert b.state is SnapshotState.COLLECTING
    assert b.seal(now_utc=OBS)


def test_observation_after_cutoff_invalidates_collection():
    b = SessionSnapshotBuilder(roster(), deadline_utc=CUT)
    late = bar("SPY.ARCX", observed_at_utc=CUT + timedelta(seconds=1))
    with pytest.raises(ContractError, match="beyond"):
        b.ingest_bar(late)


def test_different_roster_changes_snapshot_digest():
    b = SessionSnapshotBuilder(roster(), deadline_utc=CUT)
    complete(b)
    first = b.seal(now_utc=OBS)
    another_roster = replace(roster(), version=1)
    assert another_roster.digest == first.roster_sha256
    different = ExpectedRoster(
        session=SESSION,
        instruments=("SPY.ARCX",),
        sleeve_instruments=(("s:SPY", "SPY.ARCX"),),
    )
    c = SessionSnapshotBuilder(different, deadline_utc=CUT)
    c.ingest_bar(bar("SPY.ARCX"))
    c.ingest_signal(signal("s:SPY", "SPY.ARCX"))
    assert c.seal(now_utc=OBS).digest != first.digest


def test_datetime_gate_requires_explicit_utc():
    with pytest.raises(ContractError, match="UTC"):
        SessionSnapshotBuilder(roster(), deadline_utc=datetime(2026, 10, 22, 21))
