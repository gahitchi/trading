"""Fail-closed, read-only account-specific fee/funding settlement claims.

A second collector's report can be compared with a hash-verified sandbox
fill prefix. Collector labels and completeness flags are NOT authentication.
Even exact fee matches and claimed funding payments never qualify cash,
marked equity, allocation, real brokerage or a live trading risk gate.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lab.paper_account_snapshot import PaperAccountSnapshotJournal
from lab.persistence import parse_json

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,110}\Z")
_CURRENCY = re.compile(r"[A-Z][A-Z0-9]{2,7}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REPORT_BYTES = 1_048_576
_MAX_SKEW_NS = 5_000_000_000
_DEFAULT_SKEW_NS = 1_000_000_000


@dataclass(frozen=True)
class ReadOnlySettlementReport:
    report_id: str
    collector_id: str
    source_kind: str
    session_id: str
    account_id: str
    instrument_id: str
    snapshot_id: str
    capture_count: int
    capture_sha256: str
    period_start_ns: int
    observed_ns: int
    complete_fees: bool
    complete_funding: bool
    fee_items: list[dict]  # trade_id, event_ns, currency, amount; nonnegative costs
    funding_items: list[dict]  # payment_id, event_ns, currency, amount; signed cash claims


def _id(value: object, name: str):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def _currency(value: object):
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise ValueError("invalid settlement currency")
    return value


def _time(value: object, name: str, *, allow_zero: bool = False):
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise ValueError(f"invalid {name} nanosecond time")
    return value


def _money(value: object, name: str, *, nonnegative: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an exact decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"invalid {name}") from exc
    if not amount.is_finite() or (nonnegative and amount < 0):
        raise ValueError(f"invalid {name}")
    return amount


def _fee_from_sandbox(receipt: dict) -> tuple[Decimal, str]:
    commission = receipt.get("commission")
    if not isinstance(commission, str):
        raise ValueError("sandbox fill has no fee evidence; cannot claim complete fees")
    parts = commission.split(" ")
    if len(parts) != 2:
        raise ValueError("sandbox fill commission must contain amount and currency")
    return _money(parts[0], "sandbox commission", nonnegative=True), _currency(parts[1])


def load_read_only_settlement(path: Path) -> ReadOnlySettlementReport:
    """Read an existing bounded JSON report without modifying evidence files."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("settlement report path must not be a symlink")
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_REPORT_BYTES:
            raise ValueError("settlement report must be a bounded regular file")
        raw = os.read(fd, _MAX_REPORT_BYTES + 1)
    finally:
        os.close(fd)
    if not 0 < len(raw) <= _MAX_REPORT_BYTES:
        raise ValueError("settlement report is empty or too large")
    try:
        value = parse_json(raw.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ValueError("settlement report has invalid or duplicate-key JSON") from exc
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(ReadOnlySettlementReport)}:
        raise ValueError("settlement report schema mismatch")
    return ReadOnlySettlementReport(**value)


def compare_settlement_claims(
    account: PaperAccountSnapshotJournal,
    report: ReadOnlySettlementReport,
    *,
    max_skew_ns: int = _DEFAULT_SKEW_NS,
) -> dict:
    """Compare fee receipts and inspect claimed funding. NEVER post cash.

    Funding payments have no independently trusted wallet/book source yet.
    Identical commission amounts are diagnostic cross-source claims only,
    not evidence of the external collector's identity or independence.
    """
    if not isinstance(account, PaperAccountSnapshotJournal) or not isinstance(report, ReadOnlySettlementReport):
        raise ValueError("account journal and settlement report types required")
    if type(max_skew_ns) is not int or not 0 <= max_skew_ns <= _MAX_SKEW_NS:
        raise ValueError("settlement observation skew must be a bounded integer nanosecond duration")
    for val, name in (
        (report.report_id, "settlement report ID"),
        (report.collector_id, "settlement collector ID"),
        (report.session_id, "settlement session"),
        (report.account_id, "settlement account"),
        (report.instrument_id, "settlement instrument"),
        (report.snapshot_id, "settlement snapshot"),
    ):
        _id(val, name)
    if report.source_kind != "separate-readonly-settlement":
        raise ValueError("settlement report must declare the separate read-only report source kind")
    if report.collector_id in {"sandbox-cache", "local-cache", "nautilus-cache"}:
        raise ValueError("settlement collector cannot be the local sandbox cache")
    if type(report.complete_fees) is not bool or type(report.complete_funding) is not bool:
        raise ValueError("settlement completeness assertions must be booleans")
    if not report.complete_fees or not report.complete_funding:
        raise ValueError("settlement report must assert complete fee and funding coverage")
    if type(report.capture_count) is not int or report.capture_count < 0:
        raise ValueError("invalid settlement capture count")
    if not isinstance(report.capture_sha256, str) or not _SHA.fullmatch(report.capture_sha256):
        raise ValueError("invalid settlement capture digest")
    _time(report.period_start_ns, "settlement period start", allow_zero=True)
    _time(report.observed_ns, "settlement observation")
    if report.period_start_ns >= report.observed_ns:
        raise ValueError("settlement coverage interval is inverted or empty")

    # Verify full capture and account snapshots on EVERY read. No report
    # approval can bypass the exact source hash at the latest snapshot.
    snapshots = account.snapshots()
    if not snapshots:
        raise ValueError("no complete local account snapshot for settlement comparison")
    local = snapshots[-1]
    if (
        report.session_id != account.capture.session_id
        or report.account_id != account.account_id
        or report.instrument_id != account.capture.instrument_id
        or report.snapshot_id != local.snapshot_id
    ):
        raise ValueError("settlement account/session/snapshot scope mismatch")
    if report.capture_count != local.capture_count or report.capture_sha256 != local.capture_sha256:
        raise ValueError("settlement report does not bind to latest verified event prefix")
    if abs(report.observed_ns - local.captured_ns) > max_skew_ns:
        raise ValueError("settlement observation differs from account snapshot time")
    if report.period_start_ns > local.captured_ns:
        raise ValueError("settlement period begins after account snapshot")
    if report.collector_id == local.source_id:
        raise ValueError("settlement collector cannot reuse local cache source identifier")
    if not isinstance(report.fee_items, list) or not isinstance(report.funding_items, list):
        raise ValueError("settlement fee/funding claims must be complete lists")
    if len(report.fee_items) > 100_000 or len(report.funding_items) > 100_000:
        raise ValueError("settlement report has too many entries")

    prefix, digest = account.capture.evidence_prefix(report.capture_count)
    if digest != report.capture_sha256:
        raise ValueError("settlement report event prefix hash mismatch")
    fills = {}
    for receipt in prefix:
        if receipt["kind"] == "OrderFillVoided":
            raise ValueError("voided fills require corrections and independent settlement reconciliation")
        if receipt["kind"] != "OrderFilled":
            continue
        if receipt["account_id"] != report.account_id:
            raise ValueError("sandbox fill belongs to another settlement account")
        trade_id = _id(receipt["trade_id"], "sandbox trade ID")
        if trade_id in fills:
            raise ValueError("duplicate sandbox fill trade ID")
        received = _time(receipt["received_ns"], "sandbox fill receipt", allow_zero=True)
        if received > local.captured_ns:
            raise ValueError("sandbox fill was received after account snapshot")
        fills[trade_id] = (
            _time(receipt["ts_event_ns"], "sandbox fill event", allow_zero=True),
            *_fee_from_sandbox(receipt),
        )
    claimed_fees = {}
    for row in report.fee_items:
        if not isinstance(row, dict) or set(row) != {"trade_id", "event_ns", "currency", "amount"}:
            raise ValueError("fee entry has invalid schema")
        trade = _id(row["trade_id"], "settlement trade ID")
        if trade in claimed_fees:
            raise ValueError("duplicate settlement fee for trade")
        time = _time(row["event_ns"], "fee event", allow_zero=True)
        if time > report.observed_ns:
            raise ValueError("fee event is future-dated")
        claimed_fees[trade] = (
            time,
            _money(row["amount"], "fee amount", nonnegative=True),
            _currency(row["currency"]),
        )
    if claimed_fees != fills:
        raise ValueError("settlement trade fees do not match complete captured fill commissions")

    # No exchange account cash delta is independently observed here.
    # We check internal structure and coverage only, not actual payment.
    seen_payments = set()
    signed_by_currency: dict[str, Decimal] = {}
    for row in report.funding_items:
        if not isinstance(row, dict) or set(row) != {"payment_id", "event_ns", "currency", "amount"}:
            raise ValueError("funding payment entry has invalid schema")
        payment = _id(row["payment_id"], "funding payment ID")
        if payment in seen_payments:
            raise ValueError("duplicate reported funding payment")
        seen_payments.add(payment)
        at_ns = _time(row["event_ns"], "funding payment event", allow_zero=True)
        if not report.period_start_ns <= at_ns <= min(report.observed_ns, local.captured_ns):
            raise ValueError("funding payment outside covered snapshot interval")
        ccy = _currency(row["currency"])
        amount = _money(row["amount"], "funding payment")
        if amount == 0:
            raise ValueError("zero funding payment must not be invented as a settlement")
        signed_by_currency[ccy] = signed_by_currency.get(ccy, Decimal(0)) + amount

    # Explicitly no monetary authorization. Aggregates are descriptive
    # unverified claims, not a journal adjustment, tax value or payout.
    return {
        "status": "fee_claims_match_funding_unverified",
        "snapshot_id": local.snapshot_id,
        "session_id": report.session_id,
        "account_id": report.account_id,
        "capture_count": report.capture_count,
        "capture_sha256": digest,
        "fee_claim_count": len(claimed_fees),
        "funding_claim_count": len(seen_payments),
        "funding_claimed_by_currency": {k: str(v) for k, v in sorted(signed_by_currency.items())},
        "source_authenticated": False,
        "collector_independence_verified": False,
        "funding_payment_verified": False,
        "funding_coverage_verified": False,
        "settlement_balances_reconciled": False,
        "equity_reconciled": False,
        "margin_qualified": False,
        "risk_ready": False,
        "economic_posting_allowed": False,
    }
