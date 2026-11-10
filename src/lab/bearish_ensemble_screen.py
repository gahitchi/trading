"""Read-only historical signal census for the short-only bearish ensemble.

Pure in-memory adapter: no file or network I/O, no order simulation, no
profitability metrics, no protected-holdout access. A caller must use the
existing archived-data loader with its *before=boundary* guard before
passing an already protected research frame. Reject, rather than truncate,
any future observations at or beyond the research boundary.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from lab.bearish_ensemble import BearishEnsemble, EnsembleSettings, MinuteBar, RoundTripCosts

FIELDS = ("open", "high", "low", "close", "quote_volume", "taker_buy_quote", "oi_invalid")


@dataclass(frozen=True)
class SignalCensus:
    """Signal opportunities, not fills or independent trading outcomes."""

    bars: int
    valid_bars: int
    discarded_quality_bars: int
    candidate_times_ns: tuple[int, ...]
    candidate_setups: tuple[str, ...]
    candidate_stops_bps: tuple[float, ...]
    candidate_targets_bps: tuple[float, ...]
    veto_counts: dict[str, int]


def screen_research_frame(
    frame: pd.DataFrame,
    *,
    boundary: pd.Timestamp,
    costs: RoundTripCosts,
    settings: EnsembleSettings | None = None,
) -> SignalCensus:
    """Count candidates using only complete, trustworthy pre-holdout bars.

    Input index: consecutive UTC candle END timestamps. Missing timestamps
    are not filled; BearishEnsemble resets when a gap is encountered.
    A true 'oi_invalid' flag quarantines the entire candle and resets
    warmup even though the ensemble does not use OI as a feature.
    'flat' and 'risk_ready' are assumed solely for a signal census; this
    does not represent a portfolio or authorize paper/live orders.
    """
    boundary = pd.Timestamp(boundary)
    if boundary.tzinfo is None or boundary.utcoffset() != timedelta(0) or boundary != boundary.floor("D"):
        raise ValueError("boundary must be a fixed UTC midnight")
    if not isinstance(frame, pd.DataFrame) or not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("frame must have a UTC DatetimeIndex of completed minute-end bars")
    index = frame.index
    if str(index.tz) != "UTC" or index.hasnans or not index.is_monotonic_increasing or not index.is_unique:
        raise ValueError("timestamps must be ordered unique UTC minute ends")
    if len(index) and (index[-1] >= boundary or (index.asi8 <= 0).any() or (index.asi8 % 60_000_000_000).any()):
        raise ValueError("invalid bar timestamp or protected holdout observation")
    if not set(FIELDS).issubset(frame.columns) or len(set(frame.columns)) != len(frame.columns):
        raise ValueError("missing/duplicate candle, volume or provenance-quality fields")

    quality = frame["oi_invalid"]
    if quality.isna().any() or not pd.api.types.is_bool_dtype(quality.dtype):
        raise ValueError("oi_invalid must be an explicit boolean provenance-quality marker")
    cost_total = costs.total_bps  # validate even for an empty frame
    if not 0 <= cost_total < 10_000:
        raise ValueError("invalid total cost budget")

    engine = BearishEnsemble(settings)
    veto_counts: Counter[str] = Counter()
    candidates: list[int] = []
    setups: list[str] = []
    stops: list[float] = []
    targets: list[float] = []
    discarded = 0
    valid = 0
    for record in frame[list(FIELDS)].itertuples(index=True, name=None):
        ts, o, h, lo, c, v, buy, oi_bad = record
        if oi_bad:
            engine.reset()
            discarded += 1
            veto_counts["untrusted_oi"] += 1
            continue
        try:
            bar = MinuteBar(int(ts.value), float(o), float(h), float(lo), float(c), float(v), float(buy))
            bar.validate()
        except (TypeError, ValueError, OverflowError):
            engine.reset()
            discarded += 1
            veto_counts["invalid_candle"] += 1
            continue
        valid += 1
        decision = engine.on_closed_bar(bar, costs, flat=True, risk_ready=True)
        if decision.action == "SHORT_CANDIDATE":
            candidates.append(decision.signal_end_ns)
            setups.append(decision.setup or "unknown")
            stops.append(float(decision.stop_bps))
            targets.append(float(decision.target_bps))
        else:
            veto_counts.update(decision.vetoes)
    return SignalCensus(
        len(frame), valid, discarded, tuple(candidates), tuple(setups), tuple(stops), tuple(targets), dict(veto_counts)
    )
