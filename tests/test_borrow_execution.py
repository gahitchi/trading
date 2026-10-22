"""Historical loan evidence controls real engine orders and charged costs."""

from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from lab import backtest, data, experiments
from lab.agents.execution import ExecutionAgentConfig
from lab.borrow import BorrowBook, BorrowQuote, BorrowUnavailable, HistoricalBorrowLedger
from lab.config import BorrowSettings, ConfigError, Period, load_config
from lab.signals.registry import SignalSpec
from tests.test_long_short import Constant, _positions, _risk


def _quote(day, apr=0.1, available=True):
    return BorrowQuote("GOOG", date.fromisoformat(day), available, apr, "test vendor")


def _flat():
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1e7},
        index=pd.bdate_range("2021-01-04", periods=12),
    )


def _cfg(cfg, rows, age=3):
    path = cfg.root / "borrow.csv"
    path.write_text("ticker,as_of,available,apr,source\n" + rows)
    return replace(cfg, risk=_risk(), borrow=BorrowSettings(path, age))


def _run(cfg, signal=None):
    frame = _flat()
    return backtest.run(
        cfg, [backtest.Sleeve(signal or Constant(x=-1.0), "GOOG", 1.0)], {"GOOG": frame}, frame.index[1].date(), None
    )


def test_daily_fees_do_not_backfill_future_rates_or_charge_pre_entry_weekend():
    book = BorrowBook([_quote("2021-01-08", 0.1), _quote("2021-01-11", 0.5)])
    ledger = HistoricalBorrowLedger(book)
    friday, monday, tuesday = map(date.fromisoformat, ["2021-01-08", "2021-01-11", "2021-01-12"])
    assert ledger.close(friday, {"GOOG": -10}, {"GOOG": 100}) == 0
    assert ledger.close(monday, {"GOOG": 0}, {"GOOG": 200}) == pytest.approx(1000 * 0.1 * 3 / 365)
    assert ledger.close(tuesday, {}, {}) == 0  # covered Monday; no Tuesday fees
    assert {e["as_of"] for e in ledger.evidence} == {"2021-01-08"}
    fresh = HistoricalBorrowLedger(book)
    fresh.close(friday, {}, {})
    assert fresh.close(monday, {"GOOG": -10}, {"GOOG": 100}) == 0  # Monday entry
    assert fresh.close(tuesday, {"GOOG": -10}, {"GOOG": 100}) == pytest.approx(1000 * 0.5 / 365)


def test_partial_fee_failure_does_not_commit_or_double_charge():
    ledger = HistoricalBorrowLedger(BorrowBook([_quote("2021-01-08")], max_age_days=0))
    ledger.close(date(2021, 1, 8), {"GOOG": -10}, {"GOOG": 100})
    for _ in range(2):
        with pytest.raises(BorrowUnavailable, match="stale"):
            ledger.close(date(2021, 1, 11), {}, {})
        assert ledger.day == date(2021, 1, 8) and ledger.evidence == []


def test_unavailable_inventory_blocks_additions_but_not_reductions_or_recorded_fees():
    book = BorrowBook([_quote("2021-01-08", available=False)])
    day = date(2021, 1, 8)
    assert book.restrict_target("GOOG", 10, -20, day)[0] == 0
    assert book.restrict_target("GOOG", -10, -20, day)[0] == -10
    for target in (-5, 0, 10):
        assert book.restrict_target("GOOG", -10, target, day) == (target, None)
    ledger = HistoricalBorrowLedger(book)
    ledger.close(day, {"GOOG": -10}, {"GOOG": 100})
    assert ledger.close(date(2021, 1, 9), {}, {}) == pytest.approx(1000 * 0.1 / 365)


@pytest.mark.parametrize(
    "rows",
    [
        "",
        "GOOG,2022-01-01,true,0.1,test vendor\n",
        "GOOG,2020-01-01,true,0.1,test vendor\n",
        "GOOG,2021-01-04,false,,test vendor\n",
    ],
)
def test_engine_never_opens_short_without_current_evidence(goog_lab, rows):
    result = _run(_cfg(goog_lab, rows))
    assert result.fills == [] and result.borrow_fees == 0
    assert any(e.kind == "borrow_unavailable" for e in result.risk_events)
    assert result.borrow_model == "historical_close_book" and result.borrow_fingerprint


def test_engine_charges_per_day_history_and_records_exact_provenance(goog_lab):
    cfg = _cfg(goog_lab, "GOOG,2021-01-04,true,0.365,test vendor\n", age=30)
    result = _run(cfg)
    assert min(_positions(result.fills)) < 0
    assert result.borrow_fees == pytest.approx(sum(e["fee"] for e in result.borrow_evidence))
    # Every day is charged on the preceding close's book; first fill is Jan 6.
    assert result.borrow_evidence[0]["day"] == "2021-01-06"
    assert result.borrow_evidence[-1]["day"] == str((result.equity.index[-1] - pd.Timedelta(days=1)).date())
    days = (result.equity.index[-1] - pd.Timestamp("2021-01-06")).days
    assert result.borrow_fees == pytest.approx(result.fills[0].qty * 100 * 0.365 * days / 365)
    assert all(e["apr"] == 0.365 and e["source"] == "test vendor" for e in result.borrow_evidence)
    assert result.borrow_fees > 100  # cannot silently fall back to assumed 0.5% APR
    assert result.ledger_vs_account == pytest.approx(0, abs=1e-6)


def test_engine_missing_held_fee_invalidates_run_instead_of_publishing_free_short(goog_lab):
    cfg = _cfg(goog_lab, "GOOG,2021-01-04,true,0.1,test vendor\n", age=3)
    with pytest.raises(BorrowUnavailable, match=r"backtest invalid.*stale"):
        _run(cfg)


def test_missing_configured_history_is_never_an_assumed_fee_fallback(goog_lab):
    cfg = replace(goog_lab, borrow=BorrowSettings(goog_lab.root / "absent.csv"))
    with pytest.raises(BorrowUnavailable, match="cannot load borrow history"):
        _run(cfg)


def test_engine_long_to_short_flip_without_borrow_still_closes_long(goog_lab):
    class Flip(Constant):
        def target(self, w):
            return 1.0 if w.close[-1] == 100 else -1.0

    frame = _flat()
    frame.loc[frame.index[5] :, ["open", "high", "low", "close"]] -= 10
    result = backtest.run(
        _cfg(goog_lab, ""),
        [backtest.Sleeve(Flip(), "GOOG", 1.0)],
        {"GOOG": frame},
        frame.index[1].date(),
        None,
    )
    positions = _positions(result.fills)
    assert max(positions) > 0 and min(positions) == 0 and positions[-1] == 0
    assert any(e.kind == "borrow_unavailable" for e in result.risk_events)


def test_history_cannot_be_misused_as_live_paper_order_authorization():
    with pytest.raises(ValueError, match="cannot authorize"):
        ExecutionAgentConfig(
            instruments={}, venue=None, min_trade_value=0, capital=1, live=True, borrow_book=BorrowBook([])
        )


def test_config_and_saved_report_bind_fee_evidence(goog_lab):
    import json

    path = goog_lab.root / "lab.toml"
    path.write_text(path.read_text() + '\n[borrow]\nhistory = "loans.csv"\nmax_age_days = 4\n')
    cfg = load_config(path)
    assert cfg.borrow.history == cfg.root / "loans.csv" and cfg.borrow.max_age_days == 4
    cfg.borrow.history.write_text("ticker,as_of,available,apr,source\n")
    frame = _flat()
    period = Period("test", frame.index[1].date(), frame.index[-1].date())
    comp, results = experiments.compare(  # synthetic 2021 bars sit inside the fixture's open-ended holdout
        cfg, [SignalSpec("buy_and_hold")], ["GOOG"], period, {"GOOG": frame}, register=False, holdout_ok=True
    )
    out = experiments.save_run(cfg, "borrow-test", comp, results)
    report = json.loads((out / "summary.json").read_text())
    assert report["borrow"][0]["fingerprint"] == results[0].borrow_fingerprint
    assert report["borrow"][0]["model"] == "historical_close_book"
    warning = data.quality_warning(data.quality(replace(cfg, risk=_risk()), ["GOOG"]))
    assert "historical borrow" in warning and "borrow fees ASSUMED" not in warning
    path.write_text(path.read_text().replace("max_age_days = 4", "max_age_days = -1"))
    with pytest.raises(ConfigError, match="max_age_days"):
        load_config(path)


def test_historical_run_rejects_stale_marks_in_held_short_book(goog_lab):
    cfg = _cfg(goog_lab, "GOOG,2021-01-04,true,0.1,test vendor\nAAPL,2021-01-04,true,0.1,test vendor\n", age=30)
    frame = _flat()
    missing = frame.drop(frame.index[8])
    with pytest.raises(BorrowUnavailable, match="missing/stale marks"):
        backtest.run(
            cfg,
            [backtest.Sleeve(Constant(), t, 0.5) for t in ("GOOG", "AAPL")],
            {"GOOG": missing, "AAPL": frame},
            frame.index[1].date(),
            None,
        )


def test_unavailable_short_cannot_leave_new_long_hedge_open(goog_lab):
    cfg = _cfg(goog_lab, "")
    cfg = replace(cfg, risk=replace(cfg.risk, min_net=0, max_net=0))
    frame = _flat()
    result = backtest.run(
        cfg,
        [backtest.Sleeve(Constant(x=1), "AAPL", 0.5), backtest.Sleeve(Constant(x=-1), "GOOG", 0.5)],
        {"GOOG": frame, "AAPL": frame},
        frame.index[1].date(),
        None,
    )
    assert result.fills == []
    assert any(e.kind == "borrow_unavailable" for e in result.risk_events)


def test_historical_short_null_fails_closed_until_joint_sampling_is_qualified(goog_lab):
    cfg = _cfg(goog_lab, "")
    with pytest.raises(data.DataError, match="jointly validated sampler"):
        experiments.null_test(cfg, SignalSpec("sma_trend"), ["GOOG"], cfg.period("train"), samples=2)
