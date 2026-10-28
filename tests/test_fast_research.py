"""Research on recorded data: a forward holdout research can't see, one holdout test per configuration, and
every run counted."""

import json

import pytest
from nautilus_trader.model import CryptoPerpetual, Currency, InstrumentId, OrderBookDelta, Price, Quantity, Symbol
from nautilus_trader.persistence import ParquetDataCatalog

from lab import fast_research as fr
from lab.cli import main as lab_main
from lab.fast import backtest
from tests.conftest import REPO
from tests.test_fast import IID, USDT, cfg, tilted_market

SPLIT = 2000  # steps recorded before the first research run; the rest arrive afterwards


def catalog_with(path, deltas):
    path.mkdir()
    catalog = ParquetDataCatalog(str(path))
    inst = CryptoPerpetual(
        InstrumentId.from_str(IID), Symbol("BTCUSDT"), Currency.from_str("BTC"), USDT, USDT, False, 1, 3,
        Price.from_str("0.1"), Quantity.from_str("0.001"), 0, 0,
    )  # fmt: skip
    catalog.write_instruments([inst])
    catalog.write_order_book_deltas(deltas)
    return str(path)


@pytest.fixture
def recording(tmp_path):
    """A catalog holding the first SPLIT steps; `more()` records the rest, as the recorder would later."""
    deltas, *_ = tilted_market(3000)
    cut = next(i for i, d in enumerate(deltas) if d.ts_init >= SPLIT * 100 * 1_000_000)
    path = catalog_with(tmp_path / "catalog", deltas[:cut])

    def more():
        ParquetDataCatalog(path).write_order_book_deltas(deltas[cut:])

    return path, tmp_path / "results", deltas[cut - 1].ts_init, more


def test_first_research_run_reserves_everything_recorded_later(recording):
    path, results, last_ts, more = recording
    b = fr.boundary(results, path, IID)
    assert b == last_ts + 1
    more()
    assert fr.boundary(results, path, IID) == b  # new data lands in the holdout, not in research
    start, end = fr.research_window(results, path, IID, None, None)
    assert end == b - 1
    r = backtest(path, cfg(), start=start, end=end, taker_bps=0, maker_bps=0, latency_ms=0)
    assert r["end_ns"] < b
    for window in ((b, None), (None, b), (b + 10, b + 20)):
        with pytest.raises(fr.HoldoutGuard):
            fr.research_window(results, path, IID, *window)


def test_a_configuration_meets_the_holdout_once(recording):
    path, results, _, more = recording
    more()
    b = fr.boundary(results, path, IID)
    params = {"threshold": 0.6}
    fr.check_holdout(results, IID, params, b)
    fr.record_holdout(results, IID, params, b, {"pnl": 1.0})
    with pytest.raises(fr.HoldoutGuard, match="--again"):
        fr.check_holdout(results, IID, params, b)
    fr.check_holdout(results, IID, params, b, again="fixed a fee bug")  # allowed, and recorded by the caller
    fr.check_holdout(results, IID, {"threshold": 0.7}, b)  # another configuration is another test


def test_release_moves_the_boundary_forward_with_a_reason(recording):
    path, results, _, more = recording
    old = fr.boundary(results, path, IID)
    with pytest.raises(fr.HoldoutGuard, match="nothing has been recorded"):
        fr.release(results, path, IID, "more data")
    more()
    with pytest.raises(ValueError, match="reason"):
        fr.release(results, path, IID, " ")
    was, new = fr.release(results, path, IID, "a week more data")
    assert was == old < new == fr.boundary(results, path, IID)
    history = json.loads((results / fr.PARTITION).read_text())["instruments"][IID]["history"]
    assert [h["reason"] for h in history] == ["first research run", "a week more data"]


def test_trials_count_distinct_configurations_on_research_data(tmp_path):
    def run(params, holdout=False):
        return fr.register_trial(tmp_path, "fast-backtest", IID, params, (0, 1), {}, holdout=holdout)

    assert run({"threshold": 0.6}) == 1
    assert run({"threshold": 0.6}) == 1  # a rerun is not a new trial
    assert run({"threshold": 0.7}) == 2
    assert run({"threshold": 0.8}, holdout=True) == 2
    assert fr.register_trial(tmp_path, "micro-study", IID, {"every_ms": 100}, (0, 1), {}) == 1


def test_backtest_reports_round_trips_net_of_costs(recording):
    path, *_ = recording
    r = backtest(path, cfg(), taker_bps=5, maker_bps=2, latency_ms=0)
    assert r["round_trips"] == r["entries"]
    assert r["mean_trip"] * r["round_trips"] == pytest.approx(r["pnl"], abs=1e-6)
    assert r["t_stat"] < 0  # 5 bp a side is more than the planted edge


def test_cli_research_then_one_holdout_test(recording, tmp_path, capsys):
    path, _, _, more = recording
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    run = ["--config", str(tmp_path / "lab.toml"), "fast", "backtest", "BTCUSDT", "--catalog", path]

    assert lab_main([*run, "--size", "0.01", "--hold", "1"]) == 0
    out = capsys.readouterr().out
    assert "research data; configurations tried on research data: 1" in out
    assert "round trips" in out
    more()
    assert lab_main([*run, "--holdout"]) == 0
    assert "HOLDOUT test" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="--again"):
        lab_main([*run, "--holdout"])
    assert lab_main([*run, "--holdout", "--again", "checking the report"]) == 0
    with pytest.raises(SystemExit, match="holdout"):
        lab_main([*run, "--start", "2000-01-01", "--end", "2100-01-01"])
    ledger = (tmp_path / "results" / fr.LEDGER).read_text().splitlines()
    assert [json.loads(e)["again"] for e in ledger] == ["", "checking the report"]


def test_micro_study_reads_research_data_only(recording, tmp_path, capsys):
    path, _, _, more = recording
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    study = ["--config", str(tmp_path / "lab.toml"), "micro-study", "BTCUSDT", "--catalog", path]

    assert lab_main(study) == 0
    first = capsys.readouterr().out
    more()
    assert lab_main(study) == 0
    assert capsys.readouterr().out == first  # the new data is holdout
    assert "study settings tried: 1" in first
    with pytest.raises(SystemExit, match="holdout"):
        lab_main([*study, "--end", "2100-01-01"])


def test_micro_study_keeps_the_leaders_holdout_unseen_too(recording, tmp_path, capsys):
    path, results, last_ts, _ = recording
    leader = InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")
    deltas, *_ = tilted_market(1500)  # the leader's recording stops earlier, so its holdout starts earlier
    ParquetDataCatalog(path).write_order_book_deltas(
        [OrderBookDelta(leader, d.action, d.order, d.flags, d.sequence, d.ts_event, d.ts_init) for d in deltas]
    )
    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    study = ["--config", str(tmp_path / "lab.toml"), "micro-study", "BTCUSDT", "--catalog", path]
    assert lab_main([*study, "--leader", "BTCUSDT", "--leader-venue", "bybit"]) == 0
    assert "lead_lag_bps" in capsys.readouterr().out
    assert fr.boundary(results, path, str(leader)) == deltas[-1].ts_init + 1
    trial = json.loads((results / fr.TRIALS).read_text().splitlines()[-1])
    assert trial["window"][1] == deltas[-1].ts_init < last_ts  # cut at the leader's holdout, not the symbol's
    assert trial["params"]["leader"] == str(leader)
    with pytest.raises(SystemExit, match="another symbol or venue"):
        lab_main([*study, "--leader", "BTCUSDT"])
