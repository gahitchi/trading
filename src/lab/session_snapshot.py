"""Pure, fail-closed trading-session snapshot collector.

This class has no broker, event-loop, clock or filesystem dependencies. Sealing
provides an immutable *research/decision input*, NOT a trading authorization.
Exchange-calendar validity and data-provider finality remain external gates.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum

from lab.contracts import (
    ContractError,
    ExpectedRoster,
    MarketBarFinalized,
    SessionKey,
    SignalDisposition,
    SignalEvaluated,
    _instant,
)


class SnapshotIncomplete(ContractError):
    """At least one required validated observation has not been received."""


class SnapshotState(StrEnum):
    COLLECTING = "COLLECTING"
    SEALED = "SEALED"
    INVALID = "INVALID"
    MISSED = "MISSED"


def _payload_hash(value) -> str:
    """Hash an exact versioned dataclass payload; order-independent over input sets."""
    raw = json.dumps(asdict(value), sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """Content-addressed verified roster coverage (not broker authorization)."""

    session: SessionKey
    roster_sha256: str
    bars: tuple[MarketBarFinalized, ...]
    signals: tuple[SignalEvaluated, ...]
    digest: str
    sealed_at_utc: datetime


class SessionSnapshotBuilder:
    """Collect exactly one observation per expected instrument and sleeve.

    Protocol:
    - duplicates with identical content are idempotent;
    - conflicting duplicates, unexpected instruments, mixed sessions, ERROR or
      NOT_APPLICABLE results permanently invalidate the builder;
    - missing entries can arrive until the caller-provided UTC deadline;
    - after expiry, no snapshot can be sealed and no late event can revive it;
    - a sealed result cannot be mutated and repeat seal returns the same object.

    Calendar-derived deadline, finality verification and persisted single-session
    idempotency need separate adapters before wiring into paper execution.
    """

    def __init__(self, roster: ExpectedRoster, *, deadline_utc: datetime):
        self.roster = roster
        self.deadline_utc = _instant(deadline_utc, "deadline_utc")
        self._bars: dict[str, MarketBarFinalized] = {}
        self._signals: dict[str, SignalEvaluated] = {}
        self.state = SnapshotState.COLLECTING
        self.incident: str | None = None
        self._sealed: SessionSnapshot | None = None
        self._expected = dict(roster.sleeve_instruments)

    @property
    def missing_bars(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.roster.instruments) - set(self._bars)))

    @property
    def missing_sleeves(self) -> tuple[str, ...]:
        return tuple(sorted(set(self._expected) - set(self._signals)))

    @property
    def complete(self) -> bool:
        return self.state is SnapshotState.COLLECTING and not self.missing_bars and not self.missing_sleeves

    def _invalid(self, message: str):
        self.incident = message
        self.state = SnapshotState.INVALID
        raise ContractError(message)

    def _mutable(self):
        if self.state is not SnapshotState.COLLECTING:
            raise ContractError(f"session is terminal: {self.state}")

    def _verify_scope(self, session: SessionKey, subject: str):
        if session != self.roster.session:
            self._invalid(f"{subject}: unexpected exchange session {session} (expected {self.roster.session})")

    def ingest_bar(self, bar: MarketBarFinalized) -> bool:
        """Return True if new, False for a matching repeated event."""
        self._mutable()
        if not isinstance(bar, MarketBarFinalized):
            self._invalid("unverified market-bar payload")
        self._verify_scope(bar.session, "bar")
        if bar.instrument_id not in self.roster.instruments:
            self._invalid(f"unexpected instrument {bar.instrument_id}")
        if bar.observed_at_utc > self.deadline_utc:
            self._invalid(f"bar {bar.instrument_id} arrived beyond the sealing deadline")
        prior = self._bars.get(bar.instrument_id)
        if prior is not None:
            if _payload_hash(prior) != _payload_hash(bar):
                self._invalid(f"conflicting bar for {bar.instrument_id}")
            return False
        self._bars[bar.instrument_id] = bar
        return True

    def ingest_signal(self, signal: SignalEvaluated) -> bool:
        self._mutable()
        if not isinstance(signal, SignalEvaluated):
            self._invalid("unverified signal payload")
        self._verify_scope(signal.session, "signal")
        if signal.sleeve_id not in self._expected:
            self._invalid(f"unexpected sleeve {signal.sleeve_id}")
        if self._expected[signal.sleeve_id] != signal.instrument_id:
            self._invalid(f"sleeve {signal.sleeve_id} has an unexpected instrument")
        if signal.disposition not in (SignalDisposition.TARGET, SignalDisposition.FLAT):
            self._invalid(f"sleeve {signal.sleeve_id} cannot trade: {signal.disposition}")
        prior = self._signals.get(signal.sleeve_id)
        if prior is not None:
            if _payload_hash(prior) != _payload_hash(signal):
                self._invalid(f"conflicting signal for sleeve {signal.sleeve_id}")
            return False
        self._signals[signal.sleeve_id] = signal
        return True

    def seal(self, *, now_utc: datetime) -> SessionSnapshot:
        """Seal once; failure cannot accidentally produce a partial decision."""
        now = _instant(now_utc, "now_utc")
        if self._sealed is not None:
            return self._sealed
        self._mutable()
        if now > self.deadline_utc:
            self.state = SnapshotState.MISSED
            self.incident = "session deadline elapsed without sealing"
            raise ContractError(self.incident)
        if not self.complete:
            raise SnapshotIncomplete(
                f"missing final bars {self.missing_bars}; missing complete signals {self.missing_sleeves}"
            )
        if any(bar.observed_at_utc > now for bar in self._bars.values()):
            raise ContractError("cannot seal before the last bar has actually been observed")

        bars = tuple(self._bars[t] for t in sorted(self._bars))
        signals = tuple(self._signals[s] for s in sorted(self._signals))
        payload = {
            "schema_version": 1,
            "session": [self.roster.session.exchange_mic, self.roster.session.trading_day.isoformat()],
            "roster_sha256": self.roster.digest,
            "bars": [_payload_hash(b) for b in bars],
            "signals": [_payload_hash(s) for s in signals],
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self._sealed = SessionSnapshot(self.roster.session, self.roster.digest, bars, signals, digest, now)
        self.state = SnapshotState.SEALED
        return self._sealed

    def expire(self, *, now_utc: datetime) -> bool:
        """Explicit non-trading cutoff: returns True if the session becomes MISSED."""
        now = _instant(now_utc, "now_utc")
        if self.state is not SnapshotState.COLLECTING or now <= self.deadline_utc:
            return False
        self.state = SnapshotState.MISSED
        self.incident = (
            f"cutoff expired: missing bars {self.missing_bars}; missing signals {self.missing_sleeves}"
        )
        return True
