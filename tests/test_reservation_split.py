"""Exposure caps at decision marks, funding and margin under the gap stress (proposed by ChatGPT in G013).

Each case states what the previous all-stressed check did and what the split check does."""

from dataclasses import replace

import pytest

from lab.config import CostSettings, MarginSettings, RiskSettings
from lab.reservations import split_openings, stressed_openings

BALANCED = RiskSettings(0.25, 1.0, 0.18, max_short_gross=0.0, min_net=0.0, max_net=1.0)
DYNAMIC = RiskSettings(0.30, 1.35, 0.25, max_short_gross=0.30, min_net=-0.30, max_net=1.10)
AGGRESSIVE = RiskSettings(0.45, 1.8, 0.30, max_short_gross=0.75, min_net=-0.75, max_net=1.60)
M = MarginSettings()
SLIP, GAP = 0.01, 0.03


@pytest.fixture
def costs(goog_lab) -> CostSettings:
    return goog_lab.costs


def _book(weights: dict[str, float], equity=100_000.0, price=100.0):
    return {t: round(w * equity / price) for t, w in weights.items()}, dict.fromkeys(weights, price)


def test_a_full_long_book_is_no_longer_cut_by_the_gap_stress(costs):
    """Before: balanced could never be fully invested (a 3% adverse mark breaks a 1.0 gross cap on paper).
    Now the caps are judged at decision marks, so only entry slippage and fees are reserved."""
    target, prices = _book({"A": 0.245, "B": 0.245, "C": 0.245, "D": 0.245})  # 98% invested
    held = dict.fromkeys(target, 0)
    assert stressed_openings(held, target, prices, 100_000, BALANCED, M, costs, GAP)  # refused before
    assert split_openings(held, target, prices, 100_000, BALANCED, M, costs, SLIP, GAP) is None


def test_the_gap_stress_still_refuses_what_a_gap_could_turn_into_a_margin_call(costs):
    """1.72x gross at 50% initial margin needs 0.86 of equity; a 3% adverse move against every holding, its
    reserve and the 10% cushion take it over 1. The exposure caps pass; the funding stress refuses."""
    target, prices = _book({"A": 0.43, "B": 0.43, "C": 0.43, "D": -0.43})  # net 0.86, inside 1.6
    held = dict.fromkeys(target, 0)
    reason = split_openings(held, target, prices, 100_000, AGGRESSIVE, M, costs, SLIP, GAP)
    assert reason and "adverse move" in reason and "margin" in reason
    assert split_openings(held, target, prices, 100_000, AGGRESSIVE, M, costs, SLIP, 0.0) is None


def test_a_short_book_and_a_neutral_book_fit_their_profile(costs):
    short, prices = _book({"A": -0.14, "B": -0.14})
    assert split_openings(dict.fromkeys(short, 0), short, prices, 100_000, DYNAMIC, M, costs, SLIP, GAP) is None
    neutral, prices = _book({"A": 0.25, "B": -0.25})
    assert split_openings(dict.fromkeys(neutral, 0), neutral, prices, 100_000, DYNAMIC, M, costs, SLIP, GAP) is None


def test_an_unfilled_reduction_still_cannot_finance_new_exposure(costs):
    """ChatGPT's counterexample (G009/G010-F8), scaled to stay under the per-stock cap: covering A=-8 beside a
    new B=-8 breaks the 0.75 short book if only B fills. Every fill subset is still checked at decision marks."""
    reason = split_openings(
        {"A": -8, "B": 0}, {"A": 0, "B": -8}, {"A": 100, "B": 100}, 2000, AGGRESSIVE, M, costs, SLIP, GAP
    )
    assert reason and "short-book" in reason


def test_after_a_gap_pushed_a_holding_over_its_cap_no_new_exposure_goes_out(costs):
    """Post-gap: A drifted to 30% against a 25% cap. Adding B is refused while A breaches; the execution agent
    trims A first (reductions are never refused)."""
    held, prices = {"A": 300, "B": 0}, {"A": 100.0, "B": 100.0}
    reason = split_openings(held, {"B": 50}, prices, 100_000, BALANCED, M, costs, SLIP, GAP)
    assert reason and "per-stock" in reason
    assert split_openings({"A": 240, "B": 0}, {"B": 50}, prices, 100_000, BALANCED, M, costs, SLIP, GAP) is None


@pytest.mark.parametrize("fraction", [0.0, 0.25, 0.5, 0.75, 1.0])
def test_any_partial_fill_of_an_accepted_book_respects_the_caps_at_decision_marks(costs, fraction):
    held, prices = {"A": 100, "B": -50}, {"A": 100.0, "B": 100.0}
    target = {"A": 250, "B": -120}
    assert split_openings(held, target, prices, 100_000, DYNAMIC, M, costs, SLIP, GAP) is None
    equity = 100_000 - sum(abs(target[t] - held[t]) * prices[t] * (SLIP + costs.commission_max_pct) for t in held)
    for t in held:
        filled = {**held, t: held[t] + (target[t] - held[t]) * fraction}
        weights = {k: q * prices[k] / equity for k, q in filled.items()}
        assert max(map(abs, weights.values())) <= DYNAMIC.max_weight + 1e-9
        assert sum(map(abs, weights.values())) <= DYNAMIC.max_gross + 1e-9
        assert -sum(min(w, 0) for w in weights.values()) <= DYNAMIC.max_short_gross + 1e-9
        assert DYNAMIC.min_net - 1e-9 <= sum(weights.values()) <= DYNAMIC.max_net + 1e-9


def test_invalid_parameters_are_refused(costs):
    assert split_openings({}, {"A": 1}, {"A": 100}, 1000, BALANCED, M, costs, -0.1, GAP)
    assert split_openings({}, {"A": 1}, {}, 1000, BALANCED, M, costs, SLIP, GAP)
    assert replace(M, cushion=0.1).cushion == 0.1
