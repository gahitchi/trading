"""Point-in-time US equity universe scouting (research only, no order authority).

A missing user watchlist is not permission to buy random securities. This module
constructs a bounded research shortlist from externally supplied, provenance-
labelled *as-known* issuer observations. Official SEC Form 4 open-market purchase
disclosures can add a descriptive lead, but are neither a trading signal nor
positive expected return. Listing, liquidity, stale-data and leak checks are
applied before ranking. No broker, trading, scraping or network access occurs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from lab.markets.domain import MAX_EVENT_TIMESTAMP_NS, decimal

_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,14}")
_ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")


class DiscoveryMode(StrEnum):
    EXPLICIT = "explicit_watchlist"
    SCOUT = "automatic_market_scout"


@dataclass(frozen=True, slots=True)
class EquityObservation:
    """Market facts recorded at or before decision time; not a predicted alpha."""

    ticker: str
    venue: str
    currency: str
    received_ns: int
    session_ns: int
    price: Decimal
    median_dollar_volume: Decimal
    spread_bps: Decimal
    market_cap: Decimal
    history_sessions: int
    market_source: str
    point_in_time_membership: bool = True

    def __post_init__(self) -> None:
        if not _SYMBOL.fullmatch(self.ticker) or self.venue not in {"XNAS", "XNYS", "ARCX", "BATS", "XASE"}:
            raise ValueError("invalid US equity ticker or venue")
        if self.currency != "USD":
            raise ValueError("US equity scouting currently supports only USD listings")
        _ns(self.received_ns)
        _ns(self.session_ns)
        if self.session_ns > self.received_ns:
            raise ValueError("market session cannot be observed before it occurred")
        for name in ("price", "median_dollar_volume", "market_cap"):
            value = getattr(self, name)
            if type(value) is not Decimal or decimal(value) <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        if type(self.spread_bps) is not Decimal or not Decimal("0") <= decimal(self.spread_bps):
            raise ValueError("spread must be a nonnegative finite Decimal")
        if type(self.history_sessions) is not int or self.history_sessions < 0:
            raise ValueError("history_sessions must be nonnegative")
        if not self.market_source.strip() or self.market_source == "synthetic_production":
            raise ValueError("source provenance is required")
        if type(self.point_in_time_membership) is not bool:
            raise TypeError("membership flag must be boolean")


@dataclass(frozen=True, slots=True)
class InsiderFiling:
    """SEC Form 4 filing *availability* and transaction time are distinct.

    Source-labelled objects are NOT cryptographic/source verification: this
    offline module can only validate fields supplied by a caller. Production
    ingestion must independently verify each official Form 4, issuer mapping,
    corrections, receipt time and raw-file fingerprint before using the record.
    Non-derivative open-market buys are informational only. P without A,
    automatic plans or missing times cannot qualify as discretionary buys.
    """

    ticker: str
    accession: str
    transaction_ns: int
    filed_ns: int
    received_ns: int
    shares: Decimal
    price: Decimal
    transaction_code: str
    acquired_disposed: str
    non_derivative: bool
    direct_ownership: bool
    plan_10b5_1: bool | None
    source: str = "sec_edgar_form4"

    def __post_init__(self) -> None:
        if not _SYMBOL.fullmatch(self.ticker) or not _ACCESSION.fullmatch(self.accession):
            raise ValueError("invalid ticker or SEC accession")
        for ts in (self.transaction_ns, self.filed_ns, self.received_ns):
            _ns(ts)
        if self.transaction_ns > self.filed_ns or self.filed_ns > self.received_ns:
            raise ValueError("filing cannot be available before transaction and filing")
        if type(self.shares) is not Decimal or decimal(self.shares) <= 0:
            raise ValueError("insider shares must be positive Decimal")
        if type(self.price) is not Decimal or decimal(self.price) <= 0:
            raise ValueError("transaction price must be positive Decimal")
        if self.source != "sec_edgar_form4":
            raise ValueError("insider transaction requires verified SEC primary filing")
        if self.plan_10b5_1 is not None and type(self.plan_10b5_1) is not bool:
            raise TypeError("plan flag must be bool or unknown")


@dataclass(frozen=True, slots=True)
class ScoutPolicy:
    min_history_sessions: int = 252
    min_market_cap: Decimal = Decimal("1000000000")
    min_median_dollar_volume: Decimal = Decimal("5000000")
    max_spread_bps: Decimal = Decimal("40")
    max_market_age_ns: int = 5 * 24 * 60 * 60 * 1_000_000_000
    insider_lookback_ns: int = 90 * 24 * 60 * 60 * 1_000_000_000
    max_candidates: int = 20

    def __post_init__(self) -> None:
        if not 1 <= self.max_candidates <= 100 or self.min_history_sessions < 1:
            raise ValueError("invalid universe bounds")
        if self.max_market_age_ns <= 0 or self.insider_lookback_ns <= 0:
            raise ValueError("invalid evidence windows")
        for field in ("min_market_cap", "min_median_dollar_volume", "max_spread_bps"):
            amount = getattr(self, field)
            if type(amount) is not Decimal or not decimal(amount).is_finite():
                raise ValueError(f"{field} must be a finite Decimal")
            if amount < 0 or (field == "min_median_dollar_volume" and amount == 0):
                raise ValueError(f"{field} must be nonnegative and volume must be positive")


@dataclass(frozen=True, slots=True)
class ScoutedStock:
    ticker: str
    venue: str
    priority: Decimal
    insider_purchase_count: int
    insider_purchased_notional: Decimal
    filing_accessions: tuple[str, ...]
    market_source: str
    observed_ns: int
    rationale: tuple[str, ...]
    eligible_for_orders: bool = False


@dataclass(frozen=True, slots=True)
class ScoutResult:
    mode: DiscoveryMode
    candidates: tuple[ScoutedStock, ...]
    skipped: tuple[tuple[str, str], ...]
    evaluated_universe: int
    decision_ns: int
    research_only: bool = True
    orders_authorized: bool = False


def _ns(value: int) -> None:
    if type(value) is not int or not 0 <= value <= MAX_EVENT_TIMESTAMP_NS:
        raise ValueError("expected a nonnegative integer UTC nanosecond timestamp")


def scout_equities(
    selected: tuple[str, ...],
    market: tuple[EquityObservation, ...],
    filings: tuple[InsiderFiling, ...],
    *,
    decision_ns: int,
    policy: ScoutPolicy = ScoutPolicy(),
) -> ScoutResult:
    """Build candidates, never order intents.

    With explicit symbols, never add others or silently replace an unavailable
    symbol. Without explicit symbols, scout all observed securities, subject to
    the point-in-time source and liquidity gates. The priority ranks *which
    securities to research first*, not a return forecast or expected alpha.
    """
    _ns(decision_ns)
    if any(not _SYMBOL.fullmatch(s) for s in selected) or len(selected) != len(set(selected)):
        raise ValueError("invalid or duplicated explicitly selected tickers")
    mode = DiscoveryMode.EXPLICIT if selected else DiscoveryMode.SCOUT
    targets = set(selected)
    by_ticker: dict[str, EquityObservation] = {}
    listing_venues: dict[str, set[str]] = {}
    for row in market:
        if type(row) is not EquityObservation:
            raise TypeError("expected typed market observations")
        if row.received_ns > decision_ns:
            # Future disclosures must not influence *either* eligibility or ranking.
            continue
        if mode == DiscoveryMode.EXPLICIT and row.ticker not in targets:
            continue
        listing_venues.setdefault(row.ticker, set()).add(row.venue)
        old = by_ticker.get(row.ticker)
        if old is None or (row.received_ns, row.session_ns, row.market_source) > (
            old.received_ns,
            old.session_ns,
            old.market_source,
        ):
            by_ticker[row.ticker] = row

    verified: dict[str, dict[str, InsiderFiling]] = {}
    for row in filings:
        if type(row) is not InsiderFiling:
            raise TypeError("expected typed Form 4 filings")
        if row.ticker not in by_ticker or not (
            decision_ns - policy.insider_lookback_ns <= row.received_ns <= decision_ns
            and decision_ns - policy.insider_lookback_ns <= row.transaction_ns <= decision_ns
        ):
            continue
        if (
            row.transaction_code != "P"
            or row.acquired_disposed != "A"
            or not row.non_derivative
            or not row.direct_ownership
            or row.plan_10b5_1 is not False
        ):
            continue
        by_accession = verified.setdefault(row.ticker, {})
        # A corrected filing with the same accession contributes only once,
        # and the first evidence seen at decision time is kept.
        old = by_accession.get(row.accession)
        if old is None or row.received_ns < old.received_ns:
            by_accession[row.accession] = row

    accepted: list[ScoutedStock] = []
    skipped: list[tuple[str, str]] = []
    for ticker in sorted(targets | by_ticker.keys()):
        row = by_ticker.get(ticker)
        if row is None:
            skipped.append((ticker, "missing point-in-time market evidence"))
            continue
        if not row.point_in_time_membership:
            reason = "not an active point-in-time listing"
        elif len(listing_venues.get(ticker, ())) > 1:
            reason = "ambiguous primary listing venue; qualified instrument required"
        elif (
            decision_ns - row.received_ns > policy.max_market_age_ns
            or decision_ns - row.session_ns > policy.max_market_age_ns
        ):
            reason = "stale market evidence"
        elif row.history_sessions < policy.min_history_sessions:
            reason = "insufficient historical observations"
        elif row.market_cap < policy.min_market_cap:
            reason = "market capitalization below screening floor"
        elif row.median_dollar_volume < policy.min_median_dollar_volume:
            reason = "insufficient observed dollar liquidity"
        elif row.spread_bps > policy.max_spread_bps:
            reason = "spread exceeds screening ceiling"
        else:
            reason = None
        if reason:
            skipped.append((ticker, reason))
            continue

        records = tuple(verified.get(ticker, {}).values())
        notional = sum((r.price * r.shares for r in records), Decimal("0"))
        # Transparent ranking for review workload only. This deliberately
        # avoids describing liquidity or insider transactions as alpha.
        # Decimal squashes a heavy-tailed dollar-volume scale without logs.
        liquidity = min(row.median_dollar_volume / policy.min_median_dollar_volume, Decimal("10"))
        spread_quality = (policy.max_spread_bps - row.spread_bps) / max(policy.max_spread_bps, Decimal("1"))
        priority = liquidity + spread_quality + min(Decimal(len(records)), Decimal("3"))
        rationale = (
            "passes as-known listing, age and liquidity screens",
            f"{len(records)} independently sourced discretionary SEC Form 4 open-market purchase filing(s)",
            "priority is research triage, not expected return or an executable recommendation",
        )
        accepted.append(
            ScoutedStock(
                ticker=ticker,
                venue=row.venue,
                priority=priority,
                insider_purchase_count=len(records),
                insider_purchased_notional=notional,
                filing_accessions=tuple(sorted(r.accession for r in records)),
                market_source=row.market_source,
                observed_ns=row.received_ns,
                rationale=rationale,
            )
        )
    accepted.sort(key=lambda x: (-x.priority, x.ticker))
    if len(accepted) > policy.max_candidates:
        skipped.extend((row.ticker, "bounded research shortlist limit") for row in accepted[policy.max_candidates :])
    return ScoutResult(
        mode=mode,
        candidates=tuple(accepted[: policy.max_candidates]),
        skipped=tuple(sorted(skipped)),
        evaluated_universe=len(by_ticker) if mode == DiscoveryMode.SCOUT else len(targets),
        decision_ns=decision_ns,
    )
