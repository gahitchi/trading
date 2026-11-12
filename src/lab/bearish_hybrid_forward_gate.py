"""Read-only inspection of forward-observed public features on a closed minute.

This intentionally cannot promote captured features to executable research:
without independently attested price availability, funding completeness or
historical market eligibility, all trade-enabling flags remain FALSE.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from lab.bearish_hybrid import Bar
from lab.bearish_hybrid_public_capture import FeatureCapture, _validate_symbol, first_seen_funding, first_seen_oi
from lab.bearish_hybrid_replay import HybridObservation


@dataclass(frozen=True)
class ForwardDiagnostic:
    """Public REST observation, never an authenticated trading signal."""

    observation: HybridObservation
    symbol: str
    oi_receipts: int
    funding_receipts: int
    qualified: bool = False
    actual_notional: float = 0.0


def inspect_public_features(
    bar: Bar,
    symbol: str,
    *,
    oi_captures: Sequence[FeatureCapture],
    funding_captures: Sequence[FeatureCapture],
) -> ForwardDiagnostic:
    """Select only receipts actually received by a completed minute end.

    A completed bar is not proof that its historical CSV was available at
    that minute. The returned observation is deliberately untrusted for any
    funded replay: market=None, funding_verified=False, source_complete=False.
    This is diagnostic inspection ONLY, not a trading-data adapter.
    """
    if not isinstance(bar, Bar):
        raise ValueError("expected one completed minute Bar")
    bar.check()
    _validate_symbol(symbol)
    if any(
        not isinstance(s, FeatureCapture) or (s.received_ns <= bar.end_ns and s.symbol != symbol)
        for s in (*oi_captures, *funding_captures)
    ):
        raise ValueError("mixed-symbol or invalid forward feature captures")
    oi = first_seen_oi(oi_captures, decision_ns=bar.end_ns)
    funding = first_seen_funding(funding_captures, decision_ns=bar.end_ns)
    return ForwardDiagnostic(
        observation=HybridObservation(bar, None, oi, funding, False, False),
        symbol=symbol,
        oi_receipts=sum(s.received_ns <= bar.end_ns for s in oi_captures),
        funding_receipts=sum(s.received_ns <= bar.end_ns for s in funding_captures),
    )
