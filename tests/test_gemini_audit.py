"""Fixes for Gemini's quantitative research audit (docs/audits/2026-10-24-quantitative-research-upgrade-audit.md).
The statistics findings F-R01 to F-R04 are tested in test_stats.py."""

import shutil

import numpy as np
import pandas as pd
import pytest

from lab import data, metrics, provenance

# ----------------------------------------------------------------------------- F-R05


def test_several_marks_a_day_are_annualized_as_one_session():
    days = pd.bdate_range("2024-01-02", periods=300)
    eq = pd.Series(100_000 * np.exp(np.random.default_rng(1).normal(0.0004, 0.01, 300).cumsum()), index=days)
    noon = pd.Series(eq.to_numpy() * 0.999, index=days + pd.Timedelta(hours=12))
    one = metrics.summarize(eq)
    # Each session's last mark (noon here) stands for it; the extra marks do not double the "days".
    two = metrics.summarize(pd.concat([noon, eq]).sort_index())
    assert two["days"] == one["days"] and two["sharpe"] == pytest.approx(one["sharpe"])


def test_sharpe_uses_returns_in_excess_of_the_risk_free_rate():
    days = pd.bdate_range("2024-01-02", periods=252)
    # A book that only earns a 5% T-bill rate has no excess return: Sharpe ~0, not a large positive number.
    eq = pd.Series(100_000 * 1.05 ** (np.arange(252) / 252), index=days)
    eq = eq * (1 + np.random.default_rng(2).normal(0, 1e-4, 252))
    rf = pd.Series(0.05, index=days)
    assert metrics.summarize(eq)["sharpe"] > 5
    excess = metrics.summarize(eq, risk_free=rf)
    assert abs(excess["sharpe"]) < 1.5 and excess["risk_free"] == "series"


def test_risk_free_rates_are_point_in_time(goog_lab):
    data.save_risk_free(
        goog_lab, pd.DataFrame({"date": ["2024-01-02", "2024-01-03"], "rate": [5.0, "."]}), source="test"
    )
    s = data.risk_free(goog_lab)
    assert list(s.index) == [pd.Timestamp("2024-01-03")] and s.iloc[0] == pytest.approx(0.05)  # known next day
    with pytest.raises(data.DataError, match="annual percentages"):
        data.save_risk_free(goog_lab, pd.DataFrame({"date": ["2024-01-02"], "rate": [500]}), source="test")


def test_the_risk_free_file_is_part_of_the_trial_identity(goog_lab):
    from lab import experiments

    before = experiments.config_fingerprint(goog_lab)
    data.save_risk_free(goog_lab, pd.DataFrame({"date": ["2024-01-02"], "rate": [5.0]}), source="test")
    assert experiments.config_fingerprint(goog_lab) != before


# ----------------------------------------------------------------------------- F-R16


def test_a_cosmetic_app_edit_keeps_the_simulation_digest(tmp_path, monkeypatch):
    package = tmp_path / "lab"
    shutil.copytree(provenance.__file__.rsplit("/", 1)[0], package, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(provenance, "__file__", str(package / "provenance.py"))
    sim, run = provenance.simulation_digest(), provenance.runtime_digest()
    (package / "app.py").write_text((package / "app.py").read_text() + "\n# cosmetic\n")
    assert provenance.simulation_digest() == sim and provenance.runtime_digest() != run
    (package / "costs.py").write_text((package / "costs.py").read_text() + "\n# changes a result\n")
    assert provenance.simulation_digest() != sim
    (package / "brand_new_module.py").write_text("X = 1\n")  # unlisted modules count as simulation code
    changed = provenance.simulation_digest()
    (package / "brand_new_module.py").write_text("X = 2\n")
    assert provenance.simulation_digest() != changed


# ----------------------------------------------------------------------------- F-R02, F-R04 (workspace level)


def test_near_identical_trials_count_as_about_one(goog_lab):
    from lab import experiments
    from lab.signals.registry import SignalSpec

    per = goog_lab.period("train")
    comp, _ = experiments.compare(
        goog_lab, [SignalSpec("sma_trend", {"fast": 50}), SignalSpec("sma_trend", {"fast": 51})], ["GOOG"], per
    )
    assert len(list((goog_lab.results_dir / "trial_returns").glob("*.npz"))) == 2
    row = comp.rows[1]
    assert row["trials"] == 2 and 1 <= row["effective_trials"] < 1.3  # the two runs are almost the same bet
    experiments.compare(goog_lab, [SignalSpec("rsi_reversion")], ["GOOG"], per)
    # A different strategy adds to the count, but only partly: every active return carries the same
    # "minus buy-and-hold" leg, so even unrelated strategies' active returns are correlated.
    assert row["effective_trials"] + 0.1 < experiments.effective_trials(goog_lab) < 3


def test_the_panel_bootstrap_draws_blocks_of_varying_length():
    prices = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e6},
        index=pd.bdate_range("2015-01-02", periods=600),
    )
    counts = {
        data.block_bootstrap_panel({"X": prices}, seed, return_diagnostics=True)[1]["participating_blocks"]
        for seed in range(8)
    }
    assert len(counts) > 3  # fixed 20-session blocks always gave 30
    assert all(15 < c < 50 for c in counts)


# ----------------------------------------------------------------------------- F-R06 to F-R09: costs, financing


def _bars(n=300, price=100.0, volume=1e7, start="2021-01-04", spread=0.01):
    idx = pd.bdate_range(start, periods=n)
    close = price * np.exp(np.random.default_rng(3).normal(0, 0.01, n).cumsum())
    return pd.DataFrame(
        {
            "open": close,
            "high": close * (1 + spread),
            "low": close * (1 - spread),
            "close": close,
            "volume": volume,
        },
        index=idx,
    )


def _cfg(goog_lab, **costs):
    from dataclasses import replace

    return replace(goog_lab, costs=replace(goog_lab.costs, **costs), warmup_start=pd.Timestamp("2021-01-01").date())


def test_spread_costs_scale_with_value_not_with_the_price_level(goog_lab):
    """F-R06: one tick was 20 bps on a $5 stock and 0.2 bps on a $500 stock."""
    from lab import costs

    cfg = _cfg(goog_lab)
    cheap, dear = _bars(price=5.0), _bars(price=500.0)
    fee = costs.fee_model(
        cfg.costs,
        {"C": costs.MarketProfile.from_bars(cheap, cfg.costs), "D": costs.MarketProfile.from_bars(dear, cfg.costs)},
    )
    ts = int(pd.Timestamp("2021-06-01 10:00", tz="America/New_York").value)
    a = fee.costs("C", 10_000 / 5.0, 5.0, ts)
    b = fee.costs("D", 10_000 / 500.0, 500.0, ts)
    assert a["spread"] == pytest.approx(b["spread"], rel=0.05) and a["spread"] > 0


def test_impact_grows_with_the_square_root_of_size():
    from lab import costs
    from lab.config import CostSettings

    c = CostSettings(0.005, 1.0, 0.01, True, 0.01, 500.0, 0.02, 0.01)
    df = _bars(volume=1e6)
    fee = costs.fee_model(c, {"X": costs.MarketProfile.from_bars(df, c)})
    ts = int(pd.Timestamp("2021-12-01 10:00", tz="America/New_York").value)
    small, big = fee.costs("X", 1_000, 100.0, ts), fee.costs("X", 4_000, 100.0, ts)
    assert big["impact"] / 4_000 == pytest.approx(2 * small["impact"] / 1_000)  # per share, sqrt(4) = 2
    assert big["spread"] == pytest.approx(4 * small["spread"])


def test_corwin_schultz_reads_the_spread_from_highs_and_lows():
    from lab import costs

    calm = costs.corwin_schultz(np.full(50, 100.2), np.full(50, 99.8))
    assert np.nanmean(calm) > 0.002  # a 0.4% range every day implies a spread of a few tenths of a percent
    flat = costs.corwin_schultz(np.full(50, 100.0), np.full(50, 100.0))
    assert np.nanmax(flat) == 0


def test_orders_add_at_most_the_participation_cap(goog_lab):
    """F-R06: buying $100k of a stock that trades 2,000 shares a day takes many days, not one opening print."""
    from lab import backtest
    from lab.config import RiskSettings
    from lab.signals import BuyAndHold

    df = _bars(volume=2_000)
    cfg = _cfg(goog_lab)
    risk = RiskSettings(1.0, 1.0, 0.9999)
    r = backtest.run(cfg, backtest.equal_weight(BuyAndHold, ["X"]), {"X": df}, df.index[30].date(), None, risk=risk)
    buys = [f for f in r.fills if f.side == "BUY"]
    assert max(f.qty for f in buys) <= 100 and len(buys) > 5  # 5% of 2,000
    assert "liquidity_cap" in {e.kind for e in r.risk_events}


def test_commissions_use_the_real_share_count_before_a_split():
    """F-R07: $25,000 at a split-adjusted $0.50 is 50,000 adjusted shares but 1,250 real ones (40:1 split)."""
    from lab import costs
    from lab.config import CostSettings

    c = CostSettings(0.005, 1.0, 0.01, False, 0.01, 500.0, 0.02, 0.01)
    split_at = int(pd.Timestamp("2021-06-01", tz="America/New_York").value)
    before = int(pd.Timestamp("2021-01-04 10:00", tz="America/New_York").value)
    plain = costs.fee_model(c).costs("X", 50_000, 0.50, before)["commission"]
    real = costs.fee_model(c, splits={"X": [(split_at, 40.0)]}).costs("X", 50_000, 0.50, before)["commission"]
    assert plain == pytest.approx(250.0) and real == pytest.approx(6.25)


def test_simulated_prices_keep_four_decimals(goog_lab):
    """F-R07: a split-adjusted $0.5123 open is not rounded to $0.51."""
    from lab import backtest
    from lab.config import RiskSettings
    from lab.signals import BuyAndHold

    df = _bars(price=0.5)
    cfg = _cfg(goog_lab, slippage=False)
    r = backtest.run(
        cfg,
        backtest.equal_weight(BuyAndHold, ["X"]),
        {"X": df},
        df.index[30].date(),
        None,
        risk=RiskSettings(1.0, 1.0, 0.9999),
    )
    day = pd.Timestamp(r.fills[0].ts, tz="UTC").tz_convert("America/New_York").normalize().tz_localize(None)
    assert r.fills[0].price == pytest.approx(df.loc[day, "open"], abs=1e-4)


def test_free_cash_and_short_proceeds_earn_interest():
    """F-R08: credit on cash beyond the short proceeds, a rebate on the proceeds; zero unless configured."""
    from lab import margin
    from lab.config import MarginSettings

    m = MarginSettings(credit_apr=0.04, short_rebate_apr=0.03)
    assert margin.financing_credit(150_000, 50_000, 365, m) == pytest.approx(100_000 * 0.04 + 50_000 * 0.03)
    assert margin.financing_credit(150_000, 50_000, 365, MarginSettings()) == 0


def test_an_unlevered_book_in_cash_earns_credit_interest(goog_lab):
    from dataclasses import replace

    from lab import backtest
    from lab.config import RiskSettings
    from lab.signals import Signal

    class Cash(Signal):
        name = "test_cash"

        def target(self, w):
            return 0.0

    df = _bars()
    cfg = replace(_cfg(goog_lab), margin=replace(goog_lab.margin, credit_apr=0.05))
    r = backtest.run(
        cfg,
        backtest.equal_weight(Cash, ["X"]),
        {"X": df},
        df.index[30].date(),
        None,
        risk=RiskSettings(1.0, 1.0, 0.9999),
    )
    nights = (df.index[-1] - df.index[30]).days
    assert r.financing == pytest.approx(-100_000 * 0.05 * nights / 365, rel=0.02)
    assert r.equity.iloc[-1] > 100_000


def test_a_delisting_is_settled_at_its_recorded_value(goog_lab, tmp_path):
    """F-R09: the stock's prices end on its delisting date; the position settles at close x (1 + return)."""
    from lab import backtest
    from lab.config import RiskSettings
    from lab.signals import BuyAndHold

    alive, dying = _bars(), _bars().iloc[:200]
    cfg = _cfg(goog_lab, slippage=False)
    sleeves = backtest.equal_weight(BuyAndHold, ["A", "D"])
    risk = RiskSettings(1.0, 1.0, 0.9999)
    with pytest.raises(data.DataError, match="terminal/delisting"):
        backtest.run(cfg, sleeves, {"A": alive, "D": dying}, alive.index[30].date(), None, risk=risk)
    f = tmp_path / "delistings.csv"
    day = dying.index[-1].date()
    f.write_text(
        f"ticker,delist_date,delist_type,delist_return,settlement_price,source\nD,{day},merger,-0.5,,test vendor\n"
    )
    data.save_delistings(cfg, f)
    r = backtest.run(cfg, sleeves, {"A": alive, "D": dying}, alive.index[30].date(), None, risk=risk)
    sells = [x for x in r.fills if x.ticker == "D" and x.side == "SELL"]
    assert len(sells) == 1 and sells[0].price == pytest.approx(dying["close"].iloc[-1] * 0.5, abs=1e-3)
    held = sum(x.qty if x.side == "BUY" else -x.qty for x in r.fills if x.ticker == "D")
    assert held == 0 and r.metrics["delistings_settled"]["D"]["source"] == "test vendor"


def test_delisting_records_need_a_vendor_and_a_value(goog_lab, tmp_path):
    f = tmp_path / "d.csv"
    head = "ticker,delist_date,delist_type,delist_return,settlement_price,source\n"
    f.write_text(head + "X,2020-01-02,merger,,,vendor\n")
    with pytest.raises(data.DataError, match="needs delist_return"):
        data.save_delistings(goog_lab, f)
    f.write_text(head + "X,2020-01-02,merger,0.1,,\n")
    with pytest.raises(data.DataError, match="source"):
        data.save_delistings(goog_lab, f)
    f.write_text(head + "X,2020-01-02,bankruptcy,,,vendor\n")
    data.save_delistings(goog_lab, f)
    rec = data.delistings(goog_lab)["X"]
    assert rec["assumed"] and rec["delist_return"] == -0.30
    q = data.quality(goog_lab, ["X"])
    assert "ASSUMED -30%" in data.quality_warning(q)
