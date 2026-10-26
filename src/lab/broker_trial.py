"""Broker-neutral, evidence-scoped *review* admission for paper venues.

Not an authenticated broker adapter, trading permission, or a market-data
quality certification. Records are untrusted local claims until independently
attested. They must never be used directly as an execution enablement signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class TrialAsset(StrEnum):
    """Broker trial asset category; aligned with future market domain values."""

    EQUITY = "equity"
    FOREX = "forex"
    CRYPTO = "crypto"


class TrialContract(StrEnum):
    """Broker trial settlement semantics; no dependency on experimental engine."""

    SHARE = "share"
    FOREX_CASH = "forex_cash"
    SPOT = "spot"
    LINEAR_DERIVATIVE = "linear_derivative"


_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,128}\Z")
_MAX_NS = 2**63 - 1


class ProbeStatus(StrEnum):
    OBSERVED = "observed"
    MISSING = "missing"
    CONFLICT = "conflict"


class ProbeKind(StrEnum):
    SESSION_IDENTITY = "session_identity"
    PAPER_ISOLATION = "paper_isolation"
    INSTRUMENT_RULES = "instrument_rules"
    DATA_ENTITLEMENTS = "data_entitlements"
    SOURCE_TIMESTAMPS = "source_timestamps"
    OPEN_ORDERS = "open_orders"
    HISTORICAL_ORDERS = "historical_orders"
    EXECUTIONS = "executions"
    POSITIONS = "positions"
    CASH_BALANCES = "cash_balances"
    COMMISSIONS = "commissions"
    CANCEL_FILL_RACE = "cancel_fill_race"
    RECONNECT_RECOVERY = "reconnect_recovery"
    CRASH_REPLAY = "crash_replay"
    INCOMPLETE_HISTORY = "incomplete_history"
    RISK_RESTRICTIONS = "risk_restrictions"
    EQUITY_CORPORATE_ACTIONS = "equity_corporate_actions"
    EQUITY_BORROW = "equity_borrow"
    FX_ROLLOVER = "fx_rollover"
    CRYPTO_BOOK_SEQUENCE = "crypto_book_sequence"
    DERIVATIVE_FUNDING = "derivative_funding"
    DERIVATIVE_LIQUIDATION = "derivative_liquidation"


_REQUIRED = frozenset(
    {
        ProbeKind.SESSION_IDENTITY,
        ProbeKind.PAPER_ISOLATION,
        ProbeKind.INSTRUMENT_RULES,
        ProbeKind.DATA_ENTITLEMENTS,
        ProbeKind.SOURCE_TIMESTAMPS,
        ProbeKind.OPEN_ORDERS,
        ProbeKind.HISTORICAL_ORDERS,
        ProbeKind.EXECUTIONS,
        ProbeKind.POSITIONS,
        ProbeKind.CASH_BALANCES,
        ProbeKind.COMMISSIONS,
        ProbeKind.CANCEL_FILL_RACE,
        ProbeKind.RECONNECT_RECOVERY,
        ProbeKind.CRASH_REPLAY,
        ProbeKind.INCOMPLETE_HISTORY,
        ProbeKind.RISK_RESTRICTIONS,
    }
)


def _identifier(value: str, name: str) -> None:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ValueError(f"invalid {name}")


def _nanoseconds(value: int, name: str) -> None:
    if type(value) is not int or not 0 <= value <= _MAX_NS:
        raise ValueError(f"invalid {name} nanoseconds")


@dataclass(frozen=True, slots=True)
class TrialScope:
    """Configured operator scope, not proof of a brokerage account."""

    venue: str
    account_scope: str
    environment: str
    instrument_id: str
    asset_class: TrialAsset
    contract_kind: TrialContract
    action_version: str
    epoch: str

    def __post_init__(self) -> None:
        for label in ("venue", "account_scope", "instrument_id", "action_version", "epoch"):
            _identifier(getattr(self, label), label)
        if type(self.environment) is not str or self.environment != "paper":
            raise ValueError("only paper venue probes can be assessed")
        if type(self.asset_class) is not TrialAsset or type(self.contract_kind) is not TrialContract:
            raise ValueError("exact instrument classification required")
        valid = {
            TrialAsset.EQUITY: {TrialContract.SHARE},
            TrialAsset.FOREX: {TrialContract.FOREX_CASH},
            TrialAsset.CRYPTO: {TrialContract.SPOT, TrialContract.LINEAR_DERIVATIVE},
        }
        if self.contract_kind not in valid[self.asset_class]:
            raise ValueError("instrument asset and contract kind mismatch")

    def required(self) -> frozenset[ProbeKind]:
        extra = {
            TrialAsset.EQUITY: {ProbeKind.EQUITY_CORPORATE_ACTIONS, ProbeKind.EQUITY_BORROW},
            TrialAsset.FOREX: {ProbeKind.FX_ROLLOVER},
            TrialAsset.CRYPTO: {ProbeKind.CRYPTO_BOOK_SEQUENCE},
        }[self.asset_class]
        if self.contract_kind is TrialContract.LINEAR_DERIVATIVE:
            extra = extra | {ProbeKind.DERIVATIVE_FUNDING, ProbeKind.DERIVATIVE_LIQUIDATION}
        return _REQUIRED | extra


@dataclass(frozen=True, slots=True)
class ProbeEvidence:
    """Single untrusted scoped observation; hashes are *not* signatures."""

    scope: TrialScope
    kind: ProbeKind
    status: ProbeStatus
    captured_ns: int
    artifact_sha256: str

    def __post_init__(self) -> None:
        if type(self.scope) is not TrialScope:
            raise ValueError("typed probe scope required")
        if type(self.kind) is not ProbeKind or type(self.status) is not ProbeStatus:
            raise ValueError("unknown probe kind/status")
        _nanoseconds(self.captured_ns, "capture")
        if type(self.artifact_sha256) is not str or _SHA.fullmatch(self.artifact_sha256) is None:
            raise ValueError("probe evidence needs lowercase SHA-256 artifact identity")


@dataclass(frozen=True, slots=True)
class TrialEvidenceAssessment:
    venue: str
    instrument_id: str
    missing: tuple[ProbeKind, ...]
    conflicting: tuple[ProbeKind, ...]
    stale: tuple[ProbeKind, ...]
    future_dated: tuple[ProbeKind, ...]
    duplicate: tuple[ProbeKind, ...]
    cross_scope: tuple[ProbeKind, ...]

    @property
    def claimed_coverage_complete(self) -> bool:
        """Only a checklist of local *claims*, never verified broker coverage."""
        return not (
            self.missing
            or self.conflicting
            or self.stale
            or self.future_dated
            or self.duplicate
            or self.cross_scope
        )

    @property
    def authenticated_broker(self) -> bool:
        return False

    @property
    def release_reservations(self) -> bool:
        return False

    @property
    def authorize_orders(self) -> bool:
        return False


def assess_trial_claims(
    expected: TrialScope,
    evidence: tuple[ProbeEvidence, ...],
    *,
    now_ns: int,
    maximum_age_ns: int,
) -> TrialEvidenceAssessment:
    """Audit claims against the pinned scope; never make brokerage decisions."""
    if type(expected) is not TrialScope or type(evidence) is not tuple:
        raise ValueError("typed scope and immutable evidence tuple required")
    if any(type(item) is not ProbeEvidence for item in evidence):
        raise ValueError("evidence must contain typed immutable probes")
    _nanoseconds(now_ns, "assessment")
    _nanoseconds(maximum_age_ns, "maximum age")
    if maximum_age_ns == 0:
        raise ValueError("maximum evidence age must be positive")
    by_kind: dict[ProbeKind, ProbeEvidence] = {}
    duplicate: set[ProbeKind] = set()
    cross_scope: set[ProbeKind] = set()
    future: set[ProbeKind] = set()
    stale: set[ProbeKind] = set()
    conflict: set[ProbeKind] = set()
    for item in evidence:
        if item.kind in by_kind:
            duplicate.add(item.kind)
        by_kind[item.kind] = item
        if item.scope != expected:
            cross_scope.add(item.kind)
        if item.captured_ns > now_ns:
            future.add(item.kind)
        elif now_ns - item.captured_ns > maximum_age_ns:
            stale.add(item.kind)
        if item.status is ProbeStatus.CONFLICT:
            conflict.add(item.kind)
    missing = {
        kind for kind in expected.required()
        if kind not in by_kind or by_kind[kind].status is ProbeStatus.MISSING
    }
    return TrialEvidenceAssessment(
        venue=expected.venue,
        instrument_id=expected.instrument_id,
        missing=tuple(sorted(missing, key=lambda item: item.value)),
        conflicting=tuple(sorted(conflict, key=lambda item: item.value)),
        stale=tuple(sorted(stale, key=lambda item: item.value)),
        future_dated=tuple(sorted(future, key=lambda item: item.value)),
        duplicate=tuple(sorted(duplicate, key=lambda item: item.value)),
        cross_scope=tuple(sorted(cross_scope, key=lambda item: item.value)),
    )
