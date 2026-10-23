"""Worst-case marked exposure across every subset of independent order fills."""

import math

from lab import margin as mg


def check_openings(held, destinations, prices, equity, limits, margin, reserve_cost=0.0):
    """Return a refusal reason, or None. Reductions may not finance these boxes.

    Bounds are exact for independent fills at the supplied marks; prices can
    subsequently gap. Execution rechecks fresh marks and keeps margin cushion.
    """
    if not math.isfinite(equity) or equity <= reserve_cost:
        return "no equity after execution-cost reservation"
    denominator = equity - reserve_cost
    lower, upper = {}, {}
    for t in set(held) | set(destinations):
        a, b = held.get(t, 0), destinations.get(t, held.get(t, 0))
        p = prices.get(t)
        if not p or not math.isfinite(p) or p <= 0:
            return f"{t}: missing valid reservation mark"
        lower[t], upper[t] = min(a, b) * p / denominator, max(a, b) * p / denominator
    worst = {t: max(abs(lower[t]), abs(upper[t])) for t in lower}
    if max(worst.values(), default=0) > limits.max_weight + 1e-9:
        return "possible fill exceeds per-stock limit"
    if sum(worst.values()) > limits.max_gross + 1e-9:
        return "possible fill exceeds gross limit"
    if -sum(min(v, 0) for v in lower.values()) > limits.max_short_gross + 1e-9:
        return "possible fill exceeds short-book limit"
    if sum(lower.values()) < limits.min_net - 1e-9 or sum(upper.values()) > limits.max_net + 1e-9:
        return "possible fill exceeds net limit"
    requirement = sum(
        max(mg.initial_requirement({t: lower[t]}, margin), mg.initial_requirement({t: upper[t]}, margin)) for t in lower
    )
    if requirement * (1 + margin.cushion) > 1 + 1e-9:
        return "possible fill exceeds initial-margin budget"
    return None
