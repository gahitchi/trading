"""Read-only, causal pairing of sandbox inventory with public market marks.

A successful pairing proves only that two independently stored local
archives have compatible identifiers and observation timestamps.
It is not proof of a venue fill, account funding payment, margin,
notional conversion, positive equity or safe risk capacity.
"""

from __future__ import annotations

from lab.paper_account_snapshot import PaperAccountSnapshotJournal
from lab.public_market_evidence import PublicMarketEvidenceJournal

_MAX_AGE_NS = 60_000_000_000
_DEFAULT_AGE_NS = 5_000_000_000
_MS_TO_NS = 1_000_000


def align_paper_account_mark(
    account: PaperAccountSnapshotJournal,
    market: PublicMarketEvidenceJournal,
    *,
    max_age_ns: int = _DEFAULT_AGE_NS,
) -> dict:
    """Use the latest fully observed public mark before the latest account snapshot.

    Snapshot clock and public collection clock are assumed to be the same
    epoch-nanosecond domain; this is an unverified deployment requirement.
    Strictly no interpolation, future sample selection, or forward fill
    across stale marks. The historical funding rate is *metadata only*.
    """
    if not isinstance(account, PaperAccountSnapshotJournal):
        raise ValueError("paper account journal required")
    if not isinstance(market, PublicMarketEvidenceJournal):
        raise ValueError("public market journal required")
    if type(max_age_ns) is not int or not 0 <= max_age_ns <= _MAX_AGE_NS:
        raise ValueError("mark age threshold must be a bounded nonnegative integer")

    # Both of these methods replay and verify every archived record and
    # durable checkpoint. A corrupt older record makes the whole check fail.
    snapshots = account.snapshots()
    if not snapshots:
        raise ValueError("paper account has no validated snapshot")
    samples = market.samples()
    if not samples:
        raise ValueError("public market archive has no validated mark")

    account_state = snapshots[-1]
    capture = account.capture
    if market.session_id != capture.session_id or account_state.snapshot_id == "":
        raise ValueError("market evidence and account session mismatch")
    if capture.instrument_id != f"{market.symbol}-PERP.BINANCE":
        raise ValueError("public market instrument is outside the account scope")
    if account_state.account_id != account.account_id:
        raise ValueError("account scope mismatch")
    if type(account_state.captured_ns) is not int or account_state.captured_ns <= 0:
        raise ValueError("account snapshot timestamp is not an epoch nanosecond integer")

    # The *complete paired public observation* (including historical
    # funding-rate response) must already have finished by the snapshot.
    # A later arrival can never be retroactively used for earlier equity.
    usable = [
        sample
        for sample in samples
        if sample.funding_end_ns <= account_state.captured_ns
        and sample.mark_end_ns <= account_state.captured_ns
        and sample.venue_mark_time_ms * _MS_TO_NS <= account_state.captured_ns
    ]
    if not usable:
        raise ValueError("no fully observed, nonfuture market mark at account snapshot time")

    sample = usable[-1]
    if (
        sample.session_id != capture.session_id
        or sample.instrument_id != capture.instrument_id
        or sample.symbol != market.symbol
    ):
        raise ValueError("selected mark sample is outside account/session scope")
    mark_age_ns = account_state.captured_ns - sample.venue_mark_time_ms * _MS_TO_NS
    collection_age_ns = account_state.captured_ns - sample.mark_end_ns
    if mark_age_ns < 0 or collection_age_ns < 0:
        raise ValueError("selected public mark is future-dated")
    if mark_age_ns > max_age_ns or collection_age_ns > max_age_ns:
        raise ValueError("public mark is stale against the account snapshot")

    return {
        "status": "temporally_aligned_unqualified",
        "session_id": capture.session_id,
        "snapshot_id": account_state.snapshot_id,
        "account_id": account.account_id,
        "instrument_id": capture.instrument_id,
        "capture_count": account_state.capture_count,
        "capture_sha256": account_state.capture_sha256,
        "account_observed_ns": account_state.captured_ns,
        "market_venue_time_ms": sample.venue_mark_time_ms,
        "market_mark_completed_ns": sample.mark_end_ns,
        "historical_rate_completed_ns": sample.funding_end_ns,
        "mark_age_ns": mark_age_ns,
        "collection_age_ns": collection_age_ns,
        "mark_price": sample.mark_price,
        "index_price": sample.index_price,
        "mark_response_sha256": sample.mark_sha256,
        "historical_funding_response_sha256": sample.funding_sha256,
        "historical_funding_rate": sample.historical_funding_rate,
        "historical_funding_time_ms": sample.historical_funding_time_ms,
        "funding_payment_verified": False,
        "account_source_authenticated": False,
        "clock_domain_independently_verified": False,
        "equity_reconciled": False,
        "margin_qualified": False,
        "order_state_complete": False,
        "risk_ready": False,
        "economic_posting_allowed": False,
    }
