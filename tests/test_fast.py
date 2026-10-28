"""The first fast strategy: same code in a backtest on recorded data and on the paper (sandbox) node."""

import random

import pytest
from nautilus_trader.model import (
    BookAction,
    BookOrder,
    CryptoPerpetual,
    Currency,
    InstrumentId,
    OrderBookDelta,
    OrderSide,
    Price,
    Quantity,
    Symbol,
)
from nautilus_trader.persistence import ParquetDataCatalog

from lab.fast import ImbalanceTaker, ImbalanceTakerConfig, backtest, build_paper_node
from lab.risk_manager import RiskManagedStrategy, TradingState
from lab.safety import set_kill_switch

IID = "BTCUSDT-PERP.BINANCE"
MS = 1_000_000
USDT = Currency.from_str("USDT")


def tilted_market(steps=3000, seed=7):
    """Every 100 ms the book is updated in place (adds, updates, deletes; no snapshots). Its size imbalance is
    the tilt that moves the mid exactly 1 s later; the spread is one 0.1 tick. Returns the deltas and, for
    each step, the mid and the tilt."""
    rng = random.Random(seed)
    iid = InstrumentId.from_str(IID)
    deltas, mids, tilts, pending, prev, seq, mid = [], [], [], [], {}, 0, 1000.0
    for i in range(steps):
        ts = i * 100 * MS
        tilt = rng.uniform(-1, 1)
        pending.append(tilt)
        if len(pending) > 10:
            mid = round(mid + pending.pop(0) * 0.2, 1)
        mids.append(mid)
        tilts.append(tilt)
        new = {}
        for k in range(10):
            new[(OrderSide.BUY, round(mid - k * 0.1, 1))] = 1 + max(tilt, 0) * 8
            new[(OrderSide.SELL, round(mid + 0.1 + k * 0.1, 1))] = 1 + max(-tilt, 0) * 8
        ops = [(BookAction.DELETE, key, q) for key, q in prev.items() if key not in new]
        ops += [(BookAction.UPDATE if key in prev else BookAction.ADD, key, q) for key, q in new.items()]
        for j, (action, (side, px), q) in enumerate(ops):
            seq += 1
            order = BookOrder(side, Price(px, 1), Quantity(q, 3), 0)
            deltas.append(OrderBookDelta(iid, action, order, 128 if j == len(ops) - 1 else 0, seq, ts, ts))
        prev = new
    return deltas, mids, tilts


def expected_round_trips(mids, tilts, threshold=0.6, hold_steps=10, size=0.01):
    """The strategy's trades by hand: enter at the touch when |imbalance| >= threshold, exit hold_steps later."""
    pnl, n, i = 0.0, 0, 0
    while i < len(mids) - hold_steps:
        t = tilts[i]
        if 8 * abs(t) / (2 + 8 * abs(t)) >= threshold:
            if t > 0:
                pnl += (mids[i + hold_steps] - (mids[i] + 0.1)) * size
            else:
                pnl += (mids[i] - (mids[i + hold_steps] + 0.1)) * size
            n += 1
            i += hold_steps + 1
        else:
            i += 1
    return n, pnl


@pytest.fixture(scope="module")
def market(tmp_path_factory):
    path = tmp_path_factory.mktemp("catalog")
    deltas, mids, tilts = tilted_market()
    inst = CryptoPerpetual(
        InstrumentId.from_str(IID), Symbol("BTCUSDT"), Currency.from_str("BTC"), USDT, USDT, False, 1, 3,
        Price.from_str("0.1"), Quantity.from_str("0.001"), 0, 0,
    )  # fmt: skip
    catalog = ParquetDataCatalog(str(path))
    catalog.write_instruments([inst])
    catalog.write_order_book_deltas(deltas)
    return str(path), mids, tilts


def cfg(**kw):
    return ImbalanceTakerConfig(**{"instrument_id": IID, "trade_size": 0.01, "hold_s": 1.0, **kw})


def test_backtest_trades_exactly_as_the_rules_say_on_the_recorded_book(market):
    path, mids, tilts = market
    n, pnl = expected_round_trips(mids, tilts)
    r = backtest(path, cfg(), taker_bps=0, maker_bps=0, latency_ms=0)
    assert r["stopped"] is None
    assert r["entries"] in (n, n + 1)  # one more may open in the last second and close at the end
    assert r["pnl"] == pytest.approx(pnl, abs=0.01)
    assert pnl > 0  # the planted edge beats one tick of spread...


def test_fees_and_latency_are_charged(market):
    path, *_ = market
    free = backtest(path, cfg(), taker_bps=0, maker_bps=0, latency_ms=0)
    taxed = backtest(path, cfg(), taker_bps=5, maker_bps=2, latency_ms=0)
    notional = 2 * free["entries"] * 0.01 * 1000  # about 1000 USDT a side
    assert taxed["commissions"] == pytest.approx(notional * 0.0005, rel=0.02)
    assert taxed["pnl"] < 0  # ...but not 5 bp a side
    slow = backtest(path, cfg(), taker_bps=0, maker_bps=0, latency_ms=50)
    assert slow["entries"] > 0 and slow["pnl"] != free["pnl"]


def test_loss_limit_flattens_and_stops(market):
    path, *_ = market
    r = backtest(path, cfg(), max_loss=0.5, taker_bps=5, maker_bps=2, latency_ms=0)
    assert r["stopped"].startswith("daily loss")
    assert r["pnl"] > -1.0  # stopped near the limit, not at the end of the run


def test_kill_switch_stops_new_orders(market, tmp_path):
    path, *_ = market
    kill = tmp_path / "KILL"
    set_kill_switch(kill, "halt", "operator test")
    r = backtest(path, cfg(), kill_file=str(kill), taker_bps=0, maker_bps=0, latency_ms=0)
    assert r["stopped"] == "kill switch (halt): operator test"
    assert r["entries"] == 0  # the risk manager reads the kill file before the first order
    assert r["risk_denials"].get("halted", 0) > 0


def test_position_limit_is_enforced_by_the_risk_manager(market):
    path, *_ = market
    r = backtest(path, cfg(trade_size=0.02), max_position=15.0, taker_bps=0, maker_bps=0, latency_ms=0)
    assert r["entries"] == 0 and r["risk_denials"].get("position_limit", 0) > 0  # 0.02 BTC is ~20 USDT


def test_bad_limits_are_refused(market):
    path, *_ = market
    with pytest.raises(ValueError):
        backtest(path, cfg(), max_loss=0)


@pytest.mark.parametrize("bad", [{"threshold": 0}, {"threshold": 1.5}, {"trade_size": 0}, {"hold_s": -1}])
def test_config_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        cfg(**bad)


def test_paper_node_uses_simulated_execution_only():
    node, strategy = build_paper_node(cfg())  # builds without connecting
    try:
        assert isinstance(strategy, ImbalanceTaker) and isinstance(strategy, RiskManagedStrategy)
        assert strategy.risk.state is TradingState.ACTIVE and strategy.risk.venues == ["BINANCE"]
        assert str(node.environment) == "LIVE"
    finally:
        node.dispose()
