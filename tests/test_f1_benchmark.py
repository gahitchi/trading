"""Deterministic replay and explicit nonqualification of local F1 benchmark."""

import pytest

from lab.f1_benchmark import measure_f1_replay, run


def test_synthetic_replay_measurement_reconstructs_original_and_never_qualifies():
    one = measure_f1_replay(6)
    two = measure_f1_replay(6)
    assert one.event_count == two.event_count == 6
    assert one.economic_head == two.economic_head
    assert one.append_ms >= 0
    assert one.cold_replay_ms >= 0
    assert one.selected_limits_passed is None
    assert one.declared_machine_verified is False
    assert one.broker_source_authenticated is False
    assert one.f1_qualified is False


def test_operator_chosen_limit_is_not_assumed_or_bypassed(capsys):
    assert run(["--events", "4", "--max-append-ms", "0.000001", "--max-cold-replay-ms", "0.000001"]) == 2
    assert '"selected_limits_passed":false' in capsys.readouterr().out


@pytest.mark.parametrize(
    "count, limits",
    [
        (0, {}),
        (-1, {}),
        (10_001, {}),
        (True, {}),
        (4, {"max_append_ms": 1.0}),
        (4, {"max_append_ms": float("nan"), "max_cold_replay_ms": 1.0}),
        (4, {"max_append_ms": 1.0, "max_cold_replay_ms": float("inf")}),
    ],
)
def test_replay_benchmark_refuses_ambiguous_or_unbounded_work(count, limits):
    with pytest.raises(ValueError):
        measure_f1_replay(count, **limits)
