"""No-network, portable bearish-only signal acceptance tests."""

from dataclasses import replace

import pytest

from lab.bearish_ensemble import MINUTE_NS, BearishEnsemble, EnsembleSettings, MinuteBar, RoundTripCosts

START = 1_700_000_040_000_000_000  # UTC minute boundary
COSTS = RoundTripCosts(5, 5, 2, 1)


def bar(i, *, falling=True, volume=1_000, sell_share=0.4):
    # A long, quiet trend with one decisively bearish impulse at bar 200.
    price = 100 + (0.09 * i if not falling else -0.09 * i)
    if falling and i >= 200:
        price -= 0.2
    previous = 100 + (0.09 * (i - 1) if not falling else -0.09 * (i - 1))
    if falling and i > 200:
        previous -= 0.2
    return MinuteBar(
        START + i * MINUTE_NS,
        previous,
        max(previous, price) + 0.14,
        min(previous, price) - 0.14,
        price,
        volume,
        volume * (1 - sell_share),
    )


def ready(falling=True):
    engine = BearishEnsemble()
    for i in range(200):
        last = engine.on_closed_bar(bar(i, falling=falling), COSTS, flat=True, risk_ready=True)
    assert last.action == "FLAT"
    return engine


def test_short_candidate_requires_multiple_independent_bearish_gates():
    engine = ready()
    d = engine.on_closed_bar(bar(200, volume=3000, sell_share=0.72), COSTS, flat=True, risk_ready=True)
    assert d.action == "SHORT_CANDIDATE"
    assert d.setup in {"breakdown", "continuation"}
    assert len(d.gates_passed) == 5 and not d.vetoes
    assert d.estimated_cost_bps == 23
    assert d.target_bps >= d.estimated_cost_bps * 2


def test_uptrend_and_weak_participation_are_vetoed():
    up = ready(falling=False)
    assert up.on_closed_bar(
        bar(200, falling=False, volume=3000, sell_share=0.8), COSTS, flat=True, risk_ready=True
    ).action == "FLAT"
    down = ready()
    d = down.on_closed_bar(bar(200), COSTS, flat=True, risk_ready=True)
    assert "sell_participation" in d.vetoes


@pytest.mark.parametrize(
    "flat,risk_ready,reason", [(False, True, "position_open"), (True, False, "risk_not_ready")]
)
def test_external_risk_and_position_guards_are_required(flat, risk_ready, reason):
    engine = ready()
    d = engine.on_closed_bar(
        bar(200, volume=3000, sell_share=0.72), COSTS, flat=flat, risk_ready=risk_ready
    )
    assert d.action == "FLAT" and reason in d.vetoes


def test_costs_can_veto_an_otherwise_valid_short():
    engine = ready()
    d = engine.on_closed_bar(
        bar(200, volume=3000, sell_share=0.72),
        RoundTripCosts(20, 20, 10),
        flat=True,
        risk_ready=True,
    )
    assert "risk_reward_after_costs" in d.vetoes


def test_gap_discards_all_indicators_and_requires_new_warmup():
    engine = ready()
    gap = replace(bar(200, volume=3000, sell_share=0.72), end_ns=START + 300 * MINUTE_NS)
    d = engine.on_closed_bar(gap, COSTS, flat=True, risk_ready=True)
    assert d.vetoes == ("history_gap",)
    d = engine.on_closed_bar(
        replace(gap, end_ns=gap.end_ns + MINUTE_NS), COSTS, flat=True, risk_ready=True
    )
    assert d.vetoes == ("warmup",)


def test_malformed_unordered_or_duplicate_bars_fail_closed():
    engine = ready()
    with pytest.raises(ValueError):
        engine.on_closed_bar(bar(199), COSTS, flat=True, risk_ready=True)
    with pytest.raises(ValueError):
        engine.on_closed_bar(replace(bar(200), high=0), COSTS, flat=True, risk_ready=True)
    with pytest.raises(ValueError):
        engine.on_closed_bar(
            replace(bar(200), taker_buy_quote=2000), COSTS, flat=True, risk_ready=True
        )
    with pytest.raises(ValueError):
        engine.on_closed_bar(
            bar(200), RoundTripCosts(float("nan"), 0, 0), flat=True, risk_ready=True
        )


def test_prefix_invariance_no_lookahead_and_automatic_cooldown():
    first = ready()
    other = ready()
    signal = bar(200, volume=3000, sell_share=0.72)
    a = first.on_closed_bar(signal, COSTS, flat=True, risk_ready=True)
    b = other.on_closed_bar(signal, COSTS, flat=True, risk_ready=True)
    assert a == b and a.action == "SHORT_CANDIDATE"
    # Any future bars cannot change the signal already returned.
    first.on_closed_bar(
        bar(201, volume=3000, sell_share=0.72), COSTS, flat=True, risk_ready=True
    )
    assert a == b
    assert "cooldown" in first.on_closed_bar(
        bar(202, volume=3000, sell_share=0.72), COSTS, flat=True, risk_ready=True
    ).vetoes


def test_invalid_settings_rejected():
    with pytest.raises(ValueError):
        EnsembleSettings(fast_ema=60, slow_ema=60)
    with pytest.raises(ValueError):
        EnsembleSettings(sell_share=1.1)


def test_rejects_ambiguous_risk_signals_and_extreme_downside_extension():
    engine = ready()
    with pytest.raises(ValueError):
        engine.on_closed_bar(bar(200), COSTS, flat=1, risk_ready=True)
    d = engine.on_closed_bar(
        replace(bar(200, volume=3000, sell_share=0.72), close=79.9, low=79.7),
        COSTS,
        flat=True,
        risk_ready=True,
    )
    assert d.action == "FLAT" and "risk_reward_after_costs" in d.vetoes


def test_only_flat_or_short_candidate_is_ever_generated():
    engine = BearishEnsemble()
    actions = {
        engine.on_closed_bar(
            bar(i, volume=3000 if i in (200, 210) else 1000, sell_share=0.72 if i in (200, 210) else 0.4),
            COSTS,
            flat=True,
            risk_ready=True,
        ).action
        for i in range(240)
    }
    assert actions <= {"FLAT", "SHORT_CANDIDATE"}
