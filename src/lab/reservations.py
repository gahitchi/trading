"""Worst-case marked exposure across every subset of independent order fills."""

import math

from lab import margin as mg


def check_openings(
    held, destinations, prices, equity, limits, margin, reserve_cost=0.0, *, price_bounds=None, equity_upside=0.0
):
    """Return a refusal reason, or None. Reductions may not finance these boxes.

    Bounds are exact for independent fills at the supplied marks; prices can
    subsequently gap. Execution rechecks fresh marks and keeps margin cushion.
    """
    if not math.isfinite(reserve_cost) or reserve_cost < 0:
        return "invalid execution-cost reservation"
    if not math.isfinite(equity) or equity <= reserve_cost:
        return "no equity after execution-cost reservation"
    if not math.isfinite(equity_upside) or equity_upside < 0:
        return "invalid equity-upside reservation"
    denominator = equity - reserve_cost
    upper_equity = equity + equity_upside
    lower, upper = {}, {}
    for t in set(held) | set(destinations):
        a, b = held.get(t, 0), destinations.get(t, held.get(t, 0))
        if not math.isfinite(a) or not math.isfinite(b):
            return f"{t}: nonfinite reservation quantity"
        p = prices.get(t)
        if not p or not math.isfinite(p) or p <= 0:
            return f"{t}: missing valid reservation mark"
        lo, hi = price_bounds[t] if price_bounds is not None else (p, p)
        if not all(math.isfinite(v) and v > 0 for v in (lo, hi)) or lo > hi:
            return f"{t}: invalid reservation price interval"
        corners = (a * lo, a * hi, b * lo, b * hi)
        lower[t], upper[t] = min(corners) / denominator, max(corners) / denominator
    worst = {t: max(abs(lower[t]), abs(upper[t])) for t in lower}
    if max(worst.values(), default=0) > limits.max_weight + 1e-9:
        return "possible fill exceeds per-stock limit"
    if sum(worst.values()) > limits.max_gross + 1e-9:
        return "possible fill exceeds gross limit"
    if -sum(min(v, 0) for v in lower.values()) > limits.max_short_gross + 1e-9:
        return "possible fill exceeds short-book limit"
    # Positive net floors and negative net ceilings are most restrictive at
    # HIGH equity; negative floors/positive ceilings at LOW equity. Cash/no-fill
    # and favorable marks must not disappear from this interval.
    min_net = sum(lower.values()) * (denominator / upper_equity if limits.min_net > 0 else 1)
    max_net = sum(upper.values()) * (denominator / upper_equity if limits.max_net < 0 else 1)
    if min_net < limits.min_net - 1e-9 or max_net > limits.max_net + 1e-9:
        return "possible fill exceeds net limit"
    requirement = sum(
        max(mg.initial_requirement({t: lower[t]}, margin), mg.initial_requirement({t: upper[t]}, margin)) for t in lower
    )
    if requirement * (1 + margin.cushion) > 1 + 1e-9:
        return "possible fill exceeds initial-margin budget"
    return None


def stressed_openings(held, destinations, prices, equity, limits, margin, costs, gap):
    """Conservative cost/price stress, across all subsets of fills including failed exits.

    Reserve the full configured fee cap on each order (covers fragmented fills),
    adverse entry slippage and adverse marks on all held inventory. Value exposure
    over independent lower/higher marks. This is a specified stress, never a broker guarantee.
    """
    if not math.isfinite(gap) or not 0 <= gap <= 0.5:
        return "invalid price-gap reservation"
    if any(t not in prices or not math.isfinite(prices[t]) or prices[t] <= 0 for t in set(held) | set(destinations)):
        return "missing valid reservation mark"
    costs_reserved = sum(abs(q) * prices[t] * gap for t, q in held.items())
    for t, target in destinations.items():
        qty = abs(target - held.get(t, 0))
        value = qty * prices[t]
        # A new fill can pay an adverse price AND subsequently mark adversely.
        costs_reserved += 2 * value * gap + value * (1 + gap) * costs.commission_max_pct
    return check_openings(
        held,
        destinations,
        prices,
        equity,
        limits,
        margin,
        reserve_cost=costs_reserved,
        price_bounds={t: (p * (1 - gap), p * (1 + gap)) for t, p in prices.items()},
        equity_upside=costs_reserved,
    )
