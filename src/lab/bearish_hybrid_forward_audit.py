"""Causal, offline-only audit of first-received public USD-M minute evidence.

The last closed bar is never assumed available at its exchange candle close.
Market inputs are visible no earlier than the next UTC minute boundary after
the original local HTTP receipt. Neither local receipt clocks nor SHA-256 prove
independent exchange source origin. No qualification, orders or P&L.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from lab.bearish_hybrid import MINUTE_NS
from lab.bearish_hybrid_forward_price import PriceCapture, load_forward_prices
from lab.bearish_hybrid_public_capture import (
    FeatureCapture,
    _validate_symbol,
    first_seen_funding,
    first_seen_oi,
    load_public_features,
)


@dataclass(frozen=True)
class MinuteEvidence:
    """One DISTINCT closed-price minute first received before an as-of decision.

    The OI and funding fields are diagnostics only and must never be used as
    funded trading authorization. A valid OI delta is not bearish by itself.
    """

    bar_end_ns: int
    decision_ns: int
    price_received_ns: int
    price_close: float
    price_source: str
    oi_receipts_asof: int
    funding_receipts_asof: int
    oi_change_pct: float | None
    funding_events_known: int
    missing_minutes_before: int
    qualified: bool = False
    actual_notional: int = 0


@dataclass(frozen=True)
class ForwardEvidenceAudit:
    symbol: str
    price_receipts: int
    distinct_price_minutes: int
    duplicate_price_receipts: int
    missing_internal_minutes: int
    oi_receipts: int
    funding_receipts: int
    rows: tuple[MinuteEvidence, ...]
    funding_verified: bool = False
    market_eligibility_verified: bool = False
    price_history_complete: bool = False
    source_independently_authenticated: bool = False
    qualified: bool = False
    actual_notional: int = 0


def _asof_after_receipt(price: PriceCapture) -> int:
    """First UTC minute boundary not earlier than price receipt completion."""
    ns = price.received_ns
    if type(ns) is not int or ns <= 0:
        raise ValueError("invalid first-received price time")
    decision_ns = ((ns + MINUTE_NS - 1) // MINUTE_NS) * MINUTE_NS
    if decision_ns < price.bar.end_ns or decision_ns < ns:
        raise ValueError("price was not available by decision")
    return decision_ns


def _count_received(captures: tuple[FeatureCapture, ...], decision_ns: int) -> int:
    return sum(c.received_ns <= decision_ns for c in captures)


def audit_forward_evidence(root: Path, symbol: str) -> ForwardEvidenceAudit:
    """Revalidate immutable receipts offline and join only as-of observations.

    Repeated identical price bar snapshots are counted as duplicate HTTP
    receipts, not as additional minute bars. Missing minutes are counted only
    BETWEEN distinct bars actually observed, never assumed from a capture
    schedule that was not independently recorded. Later receipts never alter
    the as-of inputs for an earlier row.
    """
    _validate_symbol(symbol)
    prices = load_forward_prices(root, symbol)
    oi = load_public_features(root, symbol, "open-interest")
    funding = load_public_features(root, symbol, "settled-funding")

    rows: list[MinuteEvidence] = []
    previous_bar_end = -1
    duplicate_receipts = 0
    missing = 0
    for capture in prices:
        end_ns = capture.bar.end_ns
        if end_ns == previous_bar_end:
            duplicate_receipts += 1
            continue
        if end_ns < previous_bar_end:
            raise ValueError("out-of-order forward minute")
        gap = 0 if previous_bar_end == -1 else (end_ns - previous_bar_end) // MINUTE_NS - 1
        if gap < 0:
            raise ValueError("overlapping forward minute")
        missing += gap
        previous_bar_end = end_ns

        decision_ns = _asof_after_receipt(capture)
        observed_oi = first_seen_oi(oi, decision_ns=decision_ns)
        observed_funding = first_seen_funding(funding, decision_ns=decision_ns)
        rows.append(
            MinuteEvidence(
                bar_end_ns=end_ns,
                decision_ns=decision_ns,
                price_received_ns=capture.received_ns,
                price_close=capture.bar.close,
                price_source=capture.source,
                oi_receipts_asof=_count_received(oi, decision_ns),
                funding_receipts_asof=_count_received(funding, decision_ns),
                oi_change_pct=observed_oi.change_pct(decision_ns, 60) if observed_oi else None,
                funding_events_known=len(observed_funding),
                missing_minutes_before=gap,
            )
        )
    return ForwardEvidenceAudit(
        symbol=symbol,
        price_receipts=len(prices),
        distinct_price_minutes=len(rows),
        duplicate_price_receipts=duplicate_receipts,
        missing_internal_minutes=missing,
        oi_receipts=len(oi),
        funding_receipts=len(funding),
        rows=tuple(rows),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline forward-market evidence audit; never places trades")
    parser.add_argument("--root", type=Path, default=Path.home() / "trading-forward-evidence")
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--last", type=int, default=12, help="Show up to 120 latest distinct minutes")
    args = parser.parse_args(argv)
    if type(args.last) is not int or not 1 <= args.last <= 120:
        parser.error("--last must be between 1 and 120")
    audit = audit_forward_evidence(args.root, args.symbol)
    print(
        f"symbol={audit.symbol} price_receipts={audit.price_receipts} "
        f"distinct_minutes={audit.distinct_price_minutes} "
        f"duplicates={audit.duplicate_price_receipts} "
        f"internal_missing_minutes={audit.missing_internal_minutes} "
        f"OI_receipts={audit.oi_receipts} funding_receipts={audit.funding_receipts}"
    )
    for row in audit.rows[-args.last :]:
        print(
            f"bar_end_ns={row.bar_end_ns} decision_ns={row.decision_ns} "
            f"price_received_ns={row.price_received_ns} "
            f"close={row.price_close} OI_asof={row.oi_receipts_asof} "
            f"OI_delta_pct={row.oi_change_pct} "
            f"funding_asof={row.funding_receipts_asof} "
            f"known_settlements={row.funding_events_known} "
            f"missing_before={row.missing_minutes_before}"
        )
    print("qualified=False actual_notional=0 source_authenticated=False funding_verified=False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
