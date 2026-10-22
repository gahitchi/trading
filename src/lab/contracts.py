"""Immutable, dependency-free domain contracts for a trading-session pipeline.

These types validate the *shape* and basic arithmetic of an observation. They do
not prove vendor finality, membership accuracy, exchange-calendar membership or
broker readiness. Nothing in this module can submit an order.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

_HASH = re.compile(r"[0-9a-f]{64}")
_MIC = re.compile(r"[A-Z0-9]{4}")


class ContractError(ValueError):
    """A domain contract would allow an ambiguous or unsafe decision."""


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ContractError(f"{label} must be a nonblank normalized string")
    return value


def _hash(value: str, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ContractError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _decimal(value: Decimal, label: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ContractError(f"{label} must be a finite Decimal, not a binary float")
    if (positive and value <= 0) or (not positive and value < 0):
        raise ContractError(f"{label} must be {'positive' if positive else 'nonnegative'}")
    return value


def _instant(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ContractError(f"{label} must be an explicitly UTC-aware datetime")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class SessionKey:
    """Venue-local trading date, supplied by an exchange calendar, not inferred from bars."""

    exchange_mic: str
    trading_day: date

    def __post_init__(self):
        if not isinstance(self.exchange_mic, str) or _MIC.fullmatch(self.exchange_mic) is None:
            raise ContractError("exchange_mic must be a four-character uppercase MIC")
        if not isinstance(self.trading_day, date) or isinstance(self.trading_day, datetime):
            raise ContractError("trading_day must be a date")


@dataclass(frozen=True, slots=True)
class ExpectedRoster:
    """An exact session manifest with one stable instrument ID per sleeve."""

    session: SessionKey
    instruments: tuple[str, ...]
    sleeve_instruments: tuple[tuple[str, str], ...]
    version: int = 1

    def __post_init__(self):
        if not isinstance(self.session, SessionKey) or self.version != 1:
            raise ContractError("unsupported session/roster schema")
        instruments = tuple(self.instruments)
        if not instruments or len(instruments) != len(set(instruments)):
            raise ContractError("instrument roster must be nonempty with no duplicates")
        for instrument in instruments:
            _text(instrument, "instrument ID")
        entries = tuple(tuple(e) for e in self.sleeve_instruments)
        if not entries or any(len(e) != 2 for e in entries):
            raise ContractError("sleeves must be nonempty (sleeve ID, instrument ID) pairs")
        if len({s for s, _ in entries}) != len(entries):
            raise ContractError("sleeve IDs must be unique")
        for sleeve, instrument in entries:
            _text(sleeve, "sleeve ID")
            if instrument not in instruments:
                raise ContractError(f"sleeve {sleeve} refers to an unknown instrument")
        object.__setattr__(self, "instruments", tuple(sorted(instruments)))
        object.__setattr__(self, "sleeve_instruments", tuple(sorted(entries)))

    @property
    def digest(self) -> str:
        payload = {
            "version": self.version,
            "mic": self.session.exchange_mic,
            "session": self.session.trading_day.isoformat(),
            "instruments": self.instruments,
            "sleeves": self.sleeve_instruments,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class MarketBarFinalized:
    """Checked OHLCV observation whose *upstream* finality still requires evidence."""

    session: SessionKey
    instrument_id: str
    currency: str
    interval_start_utc: datetime
    interval_end_utc: datetime
    observed_at_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    provider: str
    source_sha256: str

    def __post_init__(self):
        if not isinstance(self.session, SessionKey):
            raise ContractError("session required")
        _text(self.instrument_id, "instrument ID")
        if not isinstance(self.currency, str) or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ContractError("currency must be an ISO 4217 three-letter code")
        start = _instant(self.interval_start_utc, "interval_start_utc")
        end = _instant(self.interval_end_utc, "interval_end_utc")
        observed = _instant(self.observed_at_utc, "observed_at_utc")
        if start >= end or observed < end:
            raise ContractError("observation interval and finalized-at ordering are invalid")
        for field in ("open", "high", "low", "close"):
            _decimal(getattr(self, field), field, positive=True)
        _decimal(self.volume, "volume")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ContractError("OHLC extremes do not cover the opening and closing prices")
        if self.low > self.high:
            raise ContractError("low price exceeds high price")
        _text(self.provider, "provider")
        _hash(self.source_sha256, "source_sha256")
        for name, value in (("interval_start_utc", start), ("interval_end_utc", end), ("observed_at_utc", observed)):
            object.__setattr__(self, name, value)


class SignalDisposition(StrEnum):
    TARGET = "TARGET"
    FLAT = "FLAT"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class SignalEvaluated:
    """A signal that cannot be conflated with a missing event or an error."""

    session: SessionKey
    sleeve_id: str
    instrument_id: str
    strategy_sha256: str
    disposition: SignalDisposition
    target: Decimal | None
    input_sha256: str | None
    reason: str = ""

    def __post_init__(self):
        if not isinstance(self.session, SessionKey):
            raise ContractError("session required")
        _text(self.sleeve_id, "sleeve ID")
        _text(self.instrument_id, "instrument ID")
        _hash(self.strategy_sha256, "strategy_sha256")
        if not isinstance(self.disposition, SignalDisposition):
            raise ContractError("invalid signal disposition")
        if self.disposition in (SignalDisposition.TARGET, SignalDisposition.FLAT):
            _hash(self.input_sha256, "input_sha256")
            _decimal(self.target, "target")
            if self.target > 1:
                raise ContractError("long-only target exceeds 1")
            if self.disposition is SignalDisposition.FLAT and self.target != 0:
                raise ContractError("FLAT must have an explicit zero target")
        else:
            if self.target is not None:
                raise ContractError("missing/error signals cannot carry a target")
            _text(self.reason, "non-trading disposition reason")
