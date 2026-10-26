"""Basic capacity invariants: missing observed volume never enables larger orders."""

from dataclasses import replace

import pytest

from lab.agents.execution import _liquidity_cap, _opening


@pytest.mark.parametrize(
    "observations",
    [
        None,
        [],
        [(1, 2000.0)],
        [(i, 2000.0) for i in range(4)],
        [(i, 2000.0) for i in range(4)] + [(5, 0.0)],
        [(i, 2000.0) for i in range(4)] + [(5, float("nan"))],
    ],
)
def test_configured_capacity_fails_closed_without_five_valid_volumes(observations):
    assert _liquidity_cap(observations, 0.05) == 0


def test_cap_requires_explicit_disabled_setting():
    observed = [(i, 2000.0) for i in range(5)]
    assert _liquidity_cap(observed, None) is None
    assert _liquidity_cap(observed, 0.05) == 100
    assert _liquidity_cap(observed, 0.05, decision_ts=4) == 100
    # An arriving no-volume bar is never authorized using prior bar's volume.
    assert _liquidity_cap(observed, 0.05, decision_ts=5) == 0
    assert _liquidity_cap(observed, 0.0) == 0
    assert _liquidity_cap(observed, float("nan")) == 0


def test_existing_position_reduction_is_not_subject_to_opening_volume_cap():
    assert _opening(10, 0) == 0
    assert _opening(-10, -5) == 0
    assert _opening(10, 15) == 5


@pytest.mark.harness
def test_harness_paper_node_receives_identical_capacity_configuration(goog_lab, goog, tmp_path, monkeypatch):
    """Spy on the actual IBKR-paper node constructor used by the broker harness."""
    from lab import paper
    from lab.signals.registry import SignalSpec
    from lab.sim.harness import LiveHarness

    observed = []
    original = paper.ExecutionAgentConfig

    def capture_execution_config(*args, **kwargs):
        observed.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(paper, "ExecutionAgentConfig", capture_execution_config)
    costs = replace(goog_lab.costs, max_participation=0.03, cost_window=12)
    cfg = replace(goog_lab, costs=costs)
    harness = LiveHarness(
        cfg,
        [SignalSpec("buy_and_hold")],
        {"GOOG": goog},
        tmp_path / "capacity-harness",
        first_day=550,
    )
    result = harness.session()
    assert result.error is None
    assert len(observed) == 1
    assert observed[0]["max_participation"] == pytest.approx(costs.max_participation)
    assert observed[0]["participation_window"] == costs.cost_window
