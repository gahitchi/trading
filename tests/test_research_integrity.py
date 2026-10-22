"""Regressions for ChatGPT's G007 research-integrity findings F6-F10 (its reproductions, reversed)."""

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from lab import data, experiments
from lab.cli import _period, _with_target_vol, build_parser
from lab.config import Period, load_config
from lab.signals.registry import SignalSpec


def test_null_draws_never_depend_on_data_after_the_evaluated_period(monkeypatch, goog_lab):
    """F6: changing only rows after the period changed the training noise floor."""
    original = data.gbm(400, seed=7)
    revised = original.copy()
    revised.iloc[200:, :4] *= np.exp(np.arange(200) * 0.02)[:, None]
    period = Period("train", original.index[50].date(), original.index[199].date())
    seen = []

    def capture(cfg, spec, tickers, frames, period, label=None):
        seen.append(frames["GOOG"].copy())
        return SimpleNamespace(metrics={"sharpe": 0.0})

    monkeypatch.setattr(experiments, "run_spec", capture)
    monkeypatch.setattr("lab.config.load_config", lambda _p: goog_lab)
    for frame in (original, revised):
        experiments._null_worker(("lab.toml", SignalSpec("sma_trend"), ["GOOG"], {"GOOG": frame}, period, 7))
    assert seen[0].equals(seen[2])  # same seed, same past: identical resampled history
    assert seen[0].index[-1] <= pd.Timestamp(period.end)


def test_explicit_dates_cannot_route_around_the_holdout(goog_lab):
    """F7: --start/--end inside the holdout came back as 'custom' and passed the name-based guard."""
    parser = build_parser()
    h = goog_lab.period("holdout").start
    for argv in (
        ["backtest", "--start", str(h), "--end", "2013-01-31"],
        ["backtest", "--start", "2011-06-01"],  # open end runs into the holdout
        ["null-test", "sma_trend", "--start", "2011-12-01", "--end", str(h)],
    ):
        with pytest.raises(SystemExit, match="holdout is guarded"):
            _period(goog_lab, parser.parse_args(argv))
    with pytest.raises(experiments.HoldoutUsed):
        experiments.compare(goog_lab, [SignalSpec("sma_trend")], ["GOOG"], Period("custom", h, None))
    ok = _period(goog_lab, parser.parse_args(["backtest", "--start", "2006-01-01", "--end", "2007-12-31"]))
    assert ok.name == "custom"


def test_a_strategy_tried_under_another_risk_setting_is_another_trial(goog_lab):
    """F8: two risk configurations of one strategy were counted as one trial."""
    spec, active = SignalSpec("sma_trend"), pd.Series([0.01, -0.01, 0.02, 0.001])
    experiments.register_trial(goog_lab, "audit", spec, "first", active)
    experiments.register_trial(
        replace(goog_lab, risk=replace(goog_lab.risk, max_gross=0.5)), "audit", spec, "second", -active
    )
    assert len(experiments.trial_sharpes(goog_lab, "audit")) == 2
    experiments.register_trial(goog_lab, "audit", spec, "first again", active)  # identical: not a new trial
    assert len(experiments.trial_sharpes(goog_lab, "audit")) == 2


def test_cli_target_vol_cannot_exceed_the_selected_profile(tmp_path, goog_lab):
    """F9 (ChatGPT's own profile work): calm's 0.06 budget became 1.0 via --target-vol."""
    p = tmp_path / "calm.toml"
    p.write_text((goog_lab.root / "lab.toml").read_text().replace('profile = "custom"', 'profile = "calm"'))
    calm = load_config(p)
    assert calm.portfolio.target_vol == pytest.approx(0.06)
    with pytest.raises(SystemExit, match="volatility budget"):
        _with_target_vol(calm, SimpleNamespace(target_vol=1.0))
    with pytest.raises(SystemExit, match="volatility budget"):
        _with_target_vol(calm, SimpleNamespace(target_vol=0.0))  # 0 would switch the budget off
    assert _with_target_vol(calm, SimpleNamespace(target_vol=0.05)).portfolio.target_vol == 0.05
    with pytest.raises(SystemExit, match="finite"):
        _with_target_vol(goog_lab, SimpleNamespace(target_vol=float("inf")))


def test_invalid_bars_are_rejected_and_vendor_gaps_are_recorded(goog_lab):
    """F10: an infinite price passed validation; a bar missing its open vanished without a trace."""
    frame = data.gbm(5, seed=3)
    bad = frame.copy()
    bad.iloc[1, :4] = np.inf
    with pytest.raises(data.DataError, match="non-finite"):
        data.validate(bad, "X")
    gap = frame.copy()
    gap.iloc[2, 0] = np.nan
    with pytest.raises(data.DataError, match="incomplete"):
        data.validate(gap, "X")
    entry = data.save(goog_lab, "GAPPY", gap, source="test", adjusted=True)
    assert entry["dropped_incomplete"] == [str(gap.index[2].date())] and entry["rows"] == 4
    q = data.quality(goog_lab, ["GAPPY"])
    assert "removed at import" in data.quality_warning(q)


def test_holdout_window_from_config_is_still_usable_by_the_holdout_command(goog_lab):
    h = goog_lab.period("holdout")
    assert experiments.overlaps_holdout(goog_lab, h.start, None)
    assert not experiments.overlaps_holdout(goog_lab, date(2006, 1, 1), date(2006, 12, 31))


def test_a_profile_capped_by_operator_limits_says_so(tmp_path, goog_lab):
    """Integrating ChatGPT's profiles: aggressive on the default long-only hard limits trades long-only, and
    that must be reported, not silent."""
    from lab.risk_profiles import profile_bindings

    p = tmp_path / "aggr.toml"
    p.write_text((goog_lab.root / "lab.toml").read_text().replace('profile = "custom"', 'profile = "aggressive"'))
    notes = profile_bindings(load_config(p))
    assert any(n.startswith("max_short_gross") for n in notes) and any(n.startswith("max_gross") for n in notes)
    assert profile_bindings(goog_lab) == []  # custom: nothing to report
