"""Read-only comparison of two separately supplied paper account reports.

The Nautilus local sandbox cache and a *claimed* external read-only
account report may be compared only with exact identities, complete
coverage, matched event-prefix checkpoints and close observation times.
This module does not fetch any broker data, authenticate a provider,
authorize orders, infer equity/margin or automatically post economic P&L.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lab.paper_account_snapshot import PaperAccountSnapshotJournal

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,110}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_CCY = re.compile(r"[A-Z][A-Z0-9]{2,7}\Z")
_MAX_SKEW_NS = 5_000_000_000
_DEFAULT_SKEW_NS = 1_000_000_000
_BALANCE_FIELDS = {"total", "available", "locked"}


@dataclass(frozen=True)
class ReadOnlyAccountReport:
    """Untrusted data supplied by a separately operated read-only collector.

    A collector_id is an audit label, not proof of an independent source.
    Field completeness and a matching capture hash are assertions until a
    separately trusted collector and immutable origin records are established.
    """

    report_id: str
    collector_id: str
    source_kind: str
    session_id: str
    account_id: str
    instrument_id: str
    observed_ns: int
    snapshot_id: str
    capture_count: int
    capture_sha256: str
    complete_positions: bool
    complete_orders: bool
    complete_balances: bool
    positions: dict[str, str]
    open_orders: dict[str, str]
    balances: dict[str, dict[str, str]]


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate external account report key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError(f"nonfinite JSON constant in external account report: {value}")


def load_read_only_report(path: Path) -> ReadOnlyAccountReport:
    """Strict, size-bounded, read-only input. File content is UNTRUSTED.

    This function never contacts a provider or treats self-described
    source labels as proof of a separately authorized report origin.
    """
    original = Path(path)
    if original.is_symlink():
        raise ValueError("external account report cannot be a symbolic link")
    fd = os.open(original, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1_048_576:
            raise ValueError("external account report is not a bounded regular file")
        raw = os.read(fd, 1_048_577)
    finally:
        os.close(fd)
    if not raw or len(raw) > 1_048_576:
        raise ValueError("external account report is empty or too large")
    try:
        obj = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("external account report must contain UTF-8 JSON") from exc
    expected = {f.name for f in fields(ReadOnlyAccountReport)}
    if not isinstance(obj, dict) or set(obj) != expected:
        raise ValueError("external account report schema mismatch")
    return ReadOnlyAccountReport(**obj)


def _identifier(text: object, name: str) -> str:
    if not isinstance(text, str) or not _ID.fullmatch(text):
        raise ValueError(f"invalid {name}")
    return text


def _number(raw: object, name: str, *, nonnegative: bool = False) -> Decimal:
    if not isinstance(raw, str):
        raise ValueError(f"{name} must be an exact decimal string")
    try:
        result = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{name} is not a decimal") from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise ValueError(f"{name} must be finite and valid")
    return result


def _positions(raw: object, iid: str) -> dict[str, Decimal]:
    if not isinstance(raw, dict):
        raise ValueError("position inventory must be a complete mapping")
    out: dict[str, Decimal] = {}
    for symbol, qty in raw.items():
        if not isinstance(symbol, str) or symbol != iid:
            raise ValueError("position inventory includes an out-of-scope instrument")
        amount = _number(qty, "position quantity")
        if amount != 0:
            out[symbol] = amount
    return out


def _orders(raw: object) -> dict[str, Decimal]:
    if not isinstance(raw, dict):
        raise ValueError("outstanding orders must be a complete mapping")
    out = {}
    for order_id, leaves in raw.items():
        _identifier(order_id, "outstanding client order ID")
        amount = _number(leaves, "outstanding order leaves", nonnegative=True)
        if amount <= 0:
            raise ValueError("outstanding order leaves must be positive")
        out[order_id] = amount
    return out


def _balances(raw: object) -> dict[str, dict[str, Decimal]]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("currency balances are incomplete")
    balances: dict[str, dict[str, Decimal]] = {}
    for ccy, entry in raw.items():
        if not isinstance(ccy, str) or not _CCY.fullmatch(ccy):
            raise ValueError("invalid balance currency")
        if not isinstance(entry, dict) or set(entry) != _BALANCE_FIELDS:
            raise ValueError("balance must contain total/available/locked")
        amounts = {k: _number(v, f"{ccy} {k}", nonnegative=True) for k, v in entry.items()}
        if amounts["total"] != amounts["available"] + amounts["locked"]:
            raise ValueError(f"{ccy} balance arithmetic mismatch")
        balances[ccy] = amounts
    return balances


def compare_read_only_report(
    journal: PaperAccountSnapshotJournal,
    report: ReadOnlyAccountReport,
    *,
    max_skew_ns: int = _DEFAULT_SKEW_NS,
) -> dict:
    """Require exact cross-source *claims*; never certify source authenticity.

    A valid comparison does not establish provider independence, actual
    custody, complete event delivery, margin/equity, or funding accounting.
    No comparison is valid if the local snapshot is not the journal's latest
    replay-verified entry.
    """
    if not isinstance(journal, PaperAccountSnapshotJournal) or not isinstance(report, ReadOnlyAccountReport):
        raise ValueError("invalid account journal or read-only report type")
    if type(max_skew_ns) is not int or not 0 <= max_skew_ns <= _MAX_SKEW_NS:
        raise ValueError("time skew tolerance must be a bounded nonnegative integer nanosecond value")
    _identifier(report.report_id, "read-only report ID")
    _identifier(report.collector_id, "collector ID")
    _identifier(report.session_id, "report session")
    _identifier(report.account_id, "report account")
    _identifier(report.instrument_id, "report instrument")
    _identifier(report.snapshot_id, "report snapshot")
    if report.source_kind != "separate-readonly-report":
        raise ValueError("report must declare a separate read-only source type")
    if report.collector_id in {"sandbox-cache", "nautilus-cache", "local-cache"}:
        raise ValueError("report cannot relabel local cache as separate evidence")
    if type(report.observed_ns) is not int or report.observed_ns <= 0:
        raise ValueError("report observation time must be a positive nanosecond timestamp")
    if type(report.capture_count) is not int or report.capture_count < 0:
        raise ValueError("invalid external capture prefix count")
    if not isinstance(report.capture_sha256, str) or not _SHA.fullmatch(report.capture_sha256):
        raise ValueError("invalid external capture checkpoint")
    if (report.complete_positions, report.complete_orders, report.complete_balances) != (True, True, True):
        raise ValueError("independent report must explicitly attest complete positions, orders and balances")
    if any(type(v) is not bool for v in (report.complete_positions, report.complete_orders, report.complete_balances)):
        raise ValueError("report completeness fields must be booleans")

    # This call verifies every source receipt and every stored local
    # snapshot/checkpoint before any comparison. No trust in a cached report.
    snapshots = journal.snapshots()
    if not snapshots:
        raise ValueError("no completed sandbox account snapshot for crosscheck")
    local = snapshots[-1]
    capture = journal.capture
    if (
        report.session_id != capture.session_id
        or report.account_id != journal.account_id
        or report.instrument_id != capture.instrument_id
        or report.snapshot_id != local.snapshot_id
    ):
        raise ValueError("independent report scope or snapshot identity mismatch")
    if report.capture_count != local.capture_count or report.capture_sha256 != local.capture_sha256:
        raise ValueError("independent report does not bind to the same event prefix")
    if abs(report.observed_ns - local.captured_ns) > max_skew_ns:
        raise ValueError("read-only report and local snapshot observations are too far apart")
    if report.collector_id == local.source_id:
        raise ValueError("read-only collector cannot reuse the local cache source identifier")

    local_positions = _positions(local.positions, capture.instrument_id)
    report_positions = _positions(report.positions, capture.instrument_id)
    if local_positions != report_positions:
        raise ValueError("independent position inventory differs from local sandbox account")

    local_orders = _orders(local.open_orders)
    report_orders = _orders(report.open_orders)
    if local_orders != report_orders:
        raise ValueError("independent outstanding orders or remaining quantities differ")

    local_balances = _balances(local.balances)
    report_balances = _balances(report.balances)
    if local_balances != report_balances:
        raise ValueError("independent currency balance claims differ")

    return {
        "status": "match_unverified_source",
        "report_id": report.report_id,
        "collector_id": report.collector_id,
        "snapshot_id": local.snapshot_id,
        "capture_count": local.capture_count,
        "capture_sha256": local.capture_sha256,
        "observation_skew_ns": abs(report.observed_ns - local.captured_ns),
        "position_claims_match": True,
        "open_order_claims_match": True,
        "cash_balance_claims_match": True,
        "source_authenticated": False,
        "source_independence_established": False,
        "order_state_complete": False,
        "equity_reconciled": False,
        "margin_qualified": False,
        "risk_ready": False,
        "economic_posting_allowed": False,
    }
