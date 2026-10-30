"""Carry paper trading: the holdout-tested decisions, filled at the live touch, funding accrued, the kill switch
obeyed (no network in tests)."""

import json

import pandas as pd
import pytest

from lab import carry_data as cd
from lab import carry_paper as cp
from lab import carry_study as cs
from lab.safety import set_kill_switch
from tests.test_carry import T0, market

RATES = {"AUSDT": 1e-4, "BUSDT": 3e-4, "CUSDT": 2e-4}


@pytest.fixture
def lab(tmp_path):
    root = tmp_path / "data" / "carry"
    for i, (sym, r) in enumerate(RATES.items()):
        market(root, sym, days=60, rate=r, seed=i)
    p = cp.PaperSettings(capital=3_000.0, leverage=2.0, carry=cs.CarrySettings(lookback_h=24, entry_apr=5.0, top=2))
    quotes = ({s: (99.9, 100.1) for s in RATES}, {s: (99.95, 100.05) for s in RATES})
    return root, tmp_path / "results", p, quotes, tmp_path / "results" / "KILL"


def run(lab, now, quotes=None):
    root, results, p, q, kill = lab
    return cp.step(root, results, p, now, kill_path=kill, quotes=quotes or q, refresh=False, log=lambda *_: None)


def ledger(lab):
    return [json.loads(x) for x in (lab[1] / "carry" / cp.LEDGER).read_text().splitlines()]


def test_enters_the_tested_targets_at_the_touch(lab):
    now = T0 + pd.Timedelta(days=40, minutes=1)
    st = run(lab, now)
    assert set(st["positions"]) == {"BUSDT", "CUSDT"}  # the two highest funding
    b = st["positions"]["BUSDT"]
    assert b["spot_entry"] == 100.1 and b["perp_entry"] == 99.95  # buy spot at the ask, sell perp at the bid
    assert b["qty"] * 100.1 == pytest.approx(3_000 / 2 / 1.5)  # spot plus half-margin perp fill the capital
    assert b["fees"] == pytest.approx(b["qty"] * (100.1 * 10 + 99.95 * 5) / 1e4)
    assert pd.Timestamp(st["decided"]) == T0 + pd.Timedelta(days=40)
    held, _ = cp.targets(lab[0], list(RATES), lab[2].carry, now)
    assert held == set(st["positions"])  # the decision is the backtest's
    again = run(lab, now + pd.Timedelta(minutes=5))  # a restart within the hour trades nothing
    assert again["positions"] == st["positions"]
    assert [e["event"] for e in ledger(lab)].count("entry") == 2


def test_funding_is_accrued_per_settlement_after_entry(lab):
    now = T0 + pd.Timedelta(days=40, minutes=1)
    run(lab, now)
    st = run(lab, now + pd.Timedelta(hours=16))  # two settlements later (8 h and 16 h)
    b = st["positions"]["BUSDT"]
    funding = [e for e in ledger(lab) if e["event"] == "funding" and e["symbol"] == "BUSDT"]
    assert len(funding) == 2 and b["funding"] == pytest.approx(sum(e["amount"] for e in funding))
    mark = cd.load(lab[0], "binance", "perp", "close", "BUSDT")[pd.Timestamp(funding[0]["at"])]
    assert funding[0]["amount"] == pytest.approx(b["qty"] * 3e-4 * mark)  # qty x rate x the perpetual's close


def test_kill_switch_halts_entries_and_flatten_exits(lab):
    kill = lab[4]
    set_kill_switch(kill, "halt", "test")
    st = run(lab, T0 + pd.Timedelta(days=40, minutes=1))
    assert st["positions"] == {} and st["kill"]["mode"] == "halt"
    kill.unlink()
    run(lab, T0 + pd.Timedelta(days=40, hours=8, minutes=1))
    set_kill_switch(kill, "flatten", "test")
    exit_quotes = ({s: (100.0, 100.2) for s in RATES}, {s: (99.9, 100.0) for s in RATES})
    st = run(lab, T0 + pd.Timedelta(days=40, hours=16, minutes=1), exit_quotes)
    assert st["positions"] == {}
    exits = [e for e in ledger(lab) if e["event"] == "exit"]
    assert {e["symbol"] for e in exits} == {"BUSDT", "CUSDT"}
    e = exits[0]
    assert e["spot"] == 100.0 and e["perp"] == 100.0  # sell spot at the bid, buy perp back at the ask
    assert st["realized"] == pytest.approx(sum(x["pnl"] for x in exits))
    assert st["equity"] == pytest.approx(3_000 + st["realized"])


def test_a_pair_without_a_live_quote_is_not_entered(lab):
    spot, perp = lab[3]
    st = run(lab, T0 + pd.Timedelta(days=40, minutes=1), ({k: v for k, v in spot.items() if k != "BUSDT"}, perp))
    assert set(st["positions"]) == {"CUSDT"}


def test_next_run_is_just_after_the_next_rebalance_hour():
    assert cp.next_run(pd.Timestamp("2026-10-30 21:31", tz="UTC"), 8) == pd.Timestamp("2026-10-31 00:01", tz="UTC")
    assert cp.next_run(pd.Timestamp("2026-10-31 00:00:30", tz="UTC"), 8) == pd.Timestamp("2026-10-31 08:01", tz="UTC")


def test_settings_are_the_holdout_tested_ones():
    c = cp.PaperSettings().carry
    assert (c.top, c.min_turnover_m, c.entry_apr, c.exit_apr, c.lookback_h, c.rebalance_h) == (
        10,
        20.0,
        10.0,
        0.0,
        72,
        8,
    )
    with pytest.raises(ValueError):
        cp.PaperSettings(carry=cs.CarrySettings())


def test_book_tickers_keep_two_sided_quotes():
    def get(url):
        if "fapi" in url:
            return [{"symbol": "AUSDT", "bidPrice": "1.0", "askPrice": "1.1"}]
        return [
            {"symbol": "AUSDT", "bidPrice": "0.9", "askPrice": "1.2"},
            {"symbol": "B", "bidPrice": "0", "askPrice": "1"},
        ]

    assert cp.book_tickers(get) == ({"AUSDT": (0.9, 1.2)}, {"AUSDT": (1.0, 1.1)})
