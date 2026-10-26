"""Adversarial regressions for the full software audit after Claude's 1852ad1."""

import threading
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from lab import backtest, data, experiments
from lab.agents.execution import ExecutionAgent
from lab.agents.risk import RiskAgent, RiskAgentConfig
from lab.agents.schedule import NY
from lab.config import ConfigError, Period, load_config
from lab.halt import read_halt, set_halt
from lab.notify import Notifier
from lab.research.approval import deployment_context
from lab.research.orchestrator import Hypothesis, Orchestrator
from lab.safety import read_kill_switch
from lab.signals.base import Signal
from lab.signals.registry import SignalSpec
from tests.test_execution_recovery_unknown import _Agent
from tests.test_review_execution import _agent


def test_age_is_not_terminal_broker_evidence():
    a = _Agent()
    a.pending["O-123"]["ts"] = int(pd.Timestamp("2010-01-04", tz=NY).value)
    a._now = lambda: datetime(2026, 10, 23, 18, tzinfo=NY)
    ExecutionAgent._recover_pending(a)
    assert "O-123" in a.pending
    assert a.halts


def test_capital_remains_part_of_the_evaluated_artifact(goog_lab):
    cfg = replace(goog_lab, capital=100_000, paper=replace(goog_lab.paper, capital=100))
    assert deployment_context(cfg.snapshot()) != deployment_context(cfg.snapshot(), live=True)


def test_capital_change_can_eliminate_every_trade(goog_lab):
    frame = data.gbm(30, seed=19, price=1000)
    periods = Period("train", frame.index[1].date(), frame.index[-1].date())
    large = experiments.run_spec(goog_lab, SignalSpec("buy_and_hold"), ["GOOG"], {"GOOG": frame}, periods)
    small = experiments.run_spec(
        replace(goog_lab, capital=100), SignalSpec("buy_and_hold"), ["GOOG"], {"GOOG": frame}, periods
    )
    assert large.fills and not small.fills


def test_gross_headroom_is_not_short_or_net_headroom():
    agent, sent = _agent({"A": -5, "B": 0}, {"A": 0, "B": -0.5}, max_gross=1.8)
    # The approved target has short gross 0.5. If A's cover fails and B fills,
    # short gross is 1.0 and net -1.0, beyond aggressive's 0.75 / -0.75 limits.
    ExecutionAgent.rebalance(agent)
    assert sent == [("A", 5, False)]


def test_null_uses_the_same_effective_configuration_as_the_observation(goog_lab, monkeypatch):
    cfg = replace(goog_lab, capital=2500)
    seen = []
    monkeypatch.setattr(
        experiments, "run_spec", lambda c, *a, **kw: seen.append(c.capital) or SimpleNamespace(metrics={"sharpe": 0})
    )
    frame = data.gbm(60, seed=1, start="2007-01-03")
    experiments.null_test(
        cfg, SignalSpec("sma_trend"), ["GOOG"], cfg.period("train"), 1, {"GOOG": frame}, workers=1, observed_edge=0
    )
    assert seen == [2500, 2500]


def test_walk_forward_selection_does_not_shift_when_baseline_is_listed(goog_lab, monkeypatch):
    chosen = SignalSpec("sma_trend")
    eq = pd.Series([100.0, 101.0, 100.0, 102.0], index=pd.bdate_range("2009-01-02", periods=4))
    selected = []
    monkeypatch.setattr(backtest, "run", lambda *a, **kw: SimpleNamespace(equity=eq, metrics={}, fills=[]))
    monkeypatch.setattr(experiments, "run_spec", lambda *a, **kw: SimpleNamespace(equity=eq, metrics={}, fills=[]))
    monkeypatch.setattr(
        experiments,
        "active_matrix",
        lambda *a: pd.DataFrame({"sma_trend(fast=20,slow=100)": [0.01, -0.01, 0.02, 0.01]}),
    )

    def compare(cfg, specs, *args, **kwargs):
        selected.extend(specs)
        return SimpleNamespace(rows=[{"total_return": 0}, {"total_return": 0.02}]), [
            SimpleNamespace(equity=eq),
            SimpleNamespace(equity=eq * [1, 1.01, 1.0, 1.02]),
        ]

    monkeypatch.setattr(experiments, "compare", compare)
    experiments.walk_forward(
        goog_lab,
        [experiments.BASELINE, chosen],
        ["GOOG"],
        pd.Timestamp("2006-01-01").date(),
        pd.Timestamp("2007-12-31").date(),
        {},
        is_years=1,
        oos_years=1,
    )
    assert selected == [chosen]


@pytest.mark.parametrize("reader", [read_halt, read_kill_switch])
def test_invalid_utf8_controls_fail_closed(reader, tmp_path):
    p = tmp_path / "control"
    p.write_bytes(b"\xff")
    assert reader(p) is not None


def test_unhashable_halt_kind_fails_closed(tmp_path):
    p = tmp_path / "HALT"
    p.write_text('{"kind": []}')
    assert read_halt(p)["kind"] == "invalid_control_file"


def test_an_earlier_halt_cannot_hide_a_drawdown_breaker(tmp_path):
    p = tmp_path / "HALT"
    set_halt(p, "cash_mismatch", "first")
    set_halt(p, "circuit_breaker", "second")
    a = SimpleNamespace(config=SimpleNamespace(halt_path=str(p)))
    assert not RiskAgent._cleared(a, "circuit_breaker")


def test_notification_flush_waits_for_the_in_flight_request(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def post(self, text):
        entered.set()
        release.wait(2)
        return True

    monkeypatch.setattr(Notifier, "_post", post)
    n = Notifier(token="test", chat_id="test")
    try:
        n.send("local test; no network")
        assert entered.wait(1)
        assert n.flush(timeout=0.03) is False
    finally:
        release.set()
    assert n.flush(timeout=1) is True


def test_missing_timestamp_is_not_valid_market_data():
    frame = data.gbm(5, seed=1)
    frame.index = pd.DatetimeIndex([pd.NaT, *frame.index[1:]])
    with pytest.raises(data.DataError, match="timestamp"):
        data.validate(frame, "X")


def test_retry_cannot_rewrite_a_hypothesis_after_validation_error(goog_lab, monkeypatch):
    h = Hypothesis(goog_lab.research_dir, "audit")
    h.write("train.json", {"passed": True})
    h.set_status("error", "test worker interrupted after exposure to data")
    o = Orchestrator(goog_lab, None)
    monkeypatch.setattr(o, "_implement", lambda *a: False)
    monkeypatch.setattr(o, "notebook", lambda *a: None)
    with pytest.raises(ValueError, match="validation"):
        o.retry(h.id)


class Nonfinite(Signal):
    name = "audit_nonfinite"

    def target(self, w):
        return float("nan")


def test_nonfinite_signal_outputs_count_as_validation_errors(goog_lab):
    frame = data.gbm(10, seed=3)
    r = backtest.run(goog_lab, [backtest.Sleeve(Nonfinite(), "GOOG", 1)], {"GOOG": frame}, frame.index[1].date(), None)
    assert r.signal_errors > 0 and not r.fills


def test_risk_gate_never_approves_nan():
    r = RiskAgent(RiskAgentConfig(max_weight=1, max_gross=1, max_drawdown=0.2))
    out = r._approve({"A": float("nan")}, 1)
    assert np.isfinite(list(out.weights.values())).all() and out.weights["A"] == 0


@pytest.mark.parametrize(
    "old,new",
    [
        ("cash_buffer = 0.01", "cash_buffer = -1.0"),
        ("commission_min = 1.0", "commission_min = -1.0"),
        ("capital = 100000.0", "capital = nan"),
    ],
)
def test_invalid_monetary_settings_are_refused(goog_lab, tmp_path, old, new):
    p = tmp_path / "lab.toml"
    source = (goog_lab.root / "lab.toml").read_text()
    assert old in source
    p.write_text(source.replace(old, new))
    with pytest.raises(ConfigError):
        load_config(p)


def test_duplicate_sleeves_cannot_silently_overwrite_allocation(goog_lab):
    frame = data.gbm(10, seed=1)
    sig = SignalSpec("buy_and_hold")
    with pytest.raises(ValueError, match="duplicate sleeve"):
        backtest.run(
            goog_lab,
            [backtest.Sleeve(sig.make(), "GOOG", 0.5), backtest.Sleeve(sig.make(), "GOOG", 0.5)],
            {"GOOG": frame},
            frame.index[0].date(),
            None,
        )


def test_duplicate_paper_specs_are_rejected_before_node_construction(goog_lab, monkeypatch):
    from lab import paper

    monkeypatch.setattr(
        paper, "_base_node", lambda *a: pytest.fail("constructed a broker node before checking identity")
    )
    with pytest.raises(ValueError, match="duplicate"):
        paper.build_node(goog_lab, [SignalSpec("sma_trend"), SignalSpec("sma_trend", {"fast": 10})], ["GOOG"])


@pytest.mark.parametrize("method", ["moments", "sharpe_ci"])
def test_nonfinite_returns_cannot_disappear_from_statistics(method):
    from lab import stats

    with pytest.raises(ValueError, match="finite"):
        getattr(stats, method)(np.array([0.01, 0.02, float("inf"), -0.01]))


@pytest.mark.parametrize("matrix", [np.empty((0, 2)), np.full((40, 2), float("nan"))])
def test_invalid_pbo_input_cannot_report_a_probability(matrix):
    from lab import stats

    with pytest.raises(ValueError):
        stats.pbo(matrix)


def test_duplicate_candidate_labels_cannot_shift_selection(goog_lab, monkeypatch):
    result = SimpleNamespace(label="same")
    monkeypatch.setattr(experiments, "compare", lambda *a: (None, [result, result, result]))
    with pytest.raises(ValueError, match="unique"):
        experiments.active_matrix(goog_lab, [], ["GOOG"], goog_lab.period("train"), {})


def test_custom_validation_records_the_risk_actually_used(goog_lab, monkeypatch):
    import json

    from lab.research import worker

    hdir = goog_lab.research_dir / "hypotheses" / "audit_binding"
    hdir.mkdir(parents=True)
    (hdir / "signal.py").write_text("# validator inputs are mocked; no generated code executes")
    monkeypatch.setattr(worker, "load_config", lambda _: goog_lab)
    monkeypatch.setattr(worker, "available_tickers", lambda _: ["GOOG"])
    monkeypatch.setattr(worker.data, "load_many", lambda *a: {})
    observed_bounds = []

    def approved_compare(*args, **kwargs):
        observed_bounds.append(kwargs.get("bound_risk"))
        return SimpleNamespace(rows=[{}, {"sharpe_edge": 1, "dsr": 1, "signal_errors": 0, "fills": 1}]), []

    monkeypatch.setattr(experiments, "compare", approved_compare)
    monkeypatch.setattr(experiments, "null_test", lambda *a, **kw: {"p_value": 0})
    from lab import factor_stats

    monkeypatch.setattr(factor_stats, "diagnose", lambda *a, **kw: {})  # Stage-0 diagnostics (F-R15) run the signal
    worker.main(["train", str(hdir), "unused"])
    record = json.loads((hdir / "train.json").read_text())
    # Custom's relaxed exploratory limits MUST NOT be mistaken for deployable risk.
    assert observed_bounds == [True]
    assert record["validation"]["deployment"]["risk"]["max_drawdown"] == goog_lab.risk.max_drawdown
    assert record["validation"]["deployment"]["risk"]["max_weight"] == goog_lab.risk.max_weight
    assert experiments.effective_risk(goog_lab, 1).max_weight != goog_lab.risk.max_weight


def test_retry_refuses_validation_attempt_even_without_results(goog_lab):
    h = Hypothesis(goog_lab.research_dir, "audit_attempt")
    h.write("validation_attempt.json", {"mode": "train"})
    h.set_status("error", "validator timed out")
    with pytest.raises(ValueError, match="validation"):
        Orchestrator(goog_lab, None).retry(h.id)


def test_shutdown_delivers_batched_fill_alerts():
    from lab.agents.alerts import AlertAgent, AlertAgentConfig

    sent = []
    a = AlertAgent(AlertAgentConfig(notifier=SimpleNamespace(send=sent.append)))
    a.pending_fills = [SimpleNamespace(side="BUY", qty=1, ticker="X", price=100, commission=1)]
    a.on_stop()
    assert len(sent) == 1 and "BUY 1 X" in sent[0]


def test_later_blocking_halt_cannot_hide_behind_reduce_only_halt():
    agent, sent = _agent({"A": 5}, {"A": 0})
    agent._halt = lambda: {"kind": "cash_mismatch", "history": [{"kind": "paper_guard"}]}
    ExecutionAgent.rebalance(agent)
    assert sent == []


def test_later_drawdown_breaker_still_flattens():
    agent, sent = _agent({"A": 5}, {"A": 0.5})
    agent._halt = lambda: {"kind": "cash_mismatch", "history": [{"kind": "circuit_breaker"}]}
    agent._liquidate = lambda: ExecutionAgent._liquidate(agent)
    ExecutionAgent.rebalance(agent)
    assert sent == [("A", -5, False)]
