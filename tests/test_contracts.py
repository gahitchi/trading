"""Pure domain contracts: validation precedes adapter and broker integration."""

from dataclasses import FrozenInstanceError
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

DAY = SessionKey("XNYS", date(2026, 10, 22))
HASH = "a" * 64
OTHER = "b" * 64


def roster(**changes):
    values = {
        "session": DAY,
        "instruments": ("SPY.ARCX", "QQQ.XNAS"),
        "sleeve_instruments": (("momentum:SPY", "SPY.ARCX"), ("momentum:QQQ", "QQQ.XNAS")),
    }
    values.update(changes)
    return ExpectedRoster(**values)


def bar(**changes):
    values = {
        "session": DAY,
        "instrument_id": "SPY.ARCX",
        "currency": "USD",
        "interval_start_utc": datetime(2026, 10, 22, 13, 30, tzinfo=UTC),
        "interval_end_utc": datetime(2026, 10, 22, 20, 0, tzinfo=UTC),
        "observed_at_utc": datetime(2026, 10, 22, 20, 5, tzinfo=UTC),
        "open": Decimal("100.00"),
        "high": Decimal("101.00"),
        "low": Decimal("99.00"),
        "close": Decimal("100.50"),
        "volume": Decimal("1000000"),
        "provider": "example",
        "source_sha256": HASH,
    }
    values.update(changes)
    return MarketBarFinalized(**values)


def signal(disposition=SignalDisposition.TARGET, target=Decimal("0.5"), **changes):
    values = {
        "session": DAY,
        "sleeve_id": "trend:SPY",
        "instrument_id": "SPY.ARCX",
        "strategy_sha256": HASH,
        "disposition": disposition,
        "target": target,
        "input_sha256": OTHER,
    }
    values.update(changes)
    return SignalEvaluated(**values)


def test_roster_order_does_not_change_content_digest():
    one = roster()
    two = roster(
        instruments=("QQQ.XNAS", "SPY.ARCX"),
        sleeve_instruments=(("momentum:QQQ", "QQQ.XNAS"), ("momentum:SPY", "SPY.ARCX")),
    )
    assert one.digest == two.digest
    assert len(one.digest) == 64
    with pytest.raises(FrozenInstanceError):
        one.instruments = ("A",)


@pytest.mark.parametrize(
    "change",
    [
        {"instruments": ("SPY.ARCX", "SPY.ARCX")},
        {"instruments": ()},
        {"sleeve_instruments": (("one", "NOT_IN_ROSTER"),)},
        {"sleeve_instruments": (("one", "SPY.ARCX"), ("one", "QQQ.XNAS"))},
        {"sleeve_instruments": ()},
    ],
)
def test_invalid_roster_rejected(change):
    with pytest.raises(ContractError):
        roster(**change)


def test_market_bar_is_immutable_and_uses_decimal():
    good = bar()
    assert good.high == Decimal("101.00")
    with pytest.raises(FrozenInstanceError):
        good.close = Decimal("0")


@pytest.mark.parametrize(
    "change",
    [
        {"open": 100.0},
        {"close": Decimal("NaN")},
        {"high": Decimal("100.10")},
        {"low": Decimal("101")},
        {"volume": Decimal("-1")},
        {"currency": "usd"},
        {"source_sha256": "short"},
        {"interval_start_utc": datetime(2026, 10, 22, 13, 30)},
        {"observed_at_utc": datetime(2026, 10, 22, 19, 0, tzinfo=UTC)},
        {"interval_end_utc": datetime(2026, 10, 22, 13, 0, tzinfo=UTC)},
    ],
)
def test_invalid_bar_rejected(change):
    with pytest.raises(ContractError):
        bar(**change)


def test_signal_status_is_distinct_from_missing_or_error():
    assert signal().target == Decimal("0.5")
    assert signal(disposition=SignalDisposition.FLAT, target=Decimal("0")).target == 0
    assert (
        signal(
            disposition=SignalDisposition.ERROR,
            target=None,
            input_sha256=None,
            reason="missing vendor close",
        ).target
        is None
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target": Decimal("1.001")},
        {"target": 0.5},
        {"target": Decimal("-1")},
        {"disposition": SignalDisposition.FLAT, "target": Decimal("0.5")},
        {"disposition": SignalDisposition.ERROR, "target": Decimal("0.0")},
        {"disposition": SignalDisposition.ERROR, "target": None, "reason": ""},
        {"strategy_sha256": "unchecked"},
    ],
)
def test_signal_cannot_silently_coerce_invalid_values(kwargs):
    with pytest.raises(ContractError):
        signal(**kwargs)


def test_utc_awareness_not_calendar_guessing():
    assert bar().observed_at_utc.tzinfo == UTC
    with pytest.raises(ContractError):
        SessionKey("us", date(2026, 10, 22))
    with pytest.raises(ContractError):
        bar(observed_at_utc=datetime(2026, 10, 22, 20, 5, tzinfo=UTC) - timedelta(hours=1))
