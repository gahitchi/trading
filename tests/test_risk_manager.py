"""The pre-trade risk manager: the decision on plain numbers, then the same limits through NautilusTrader."""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from nautilus_trader.model import (
    AccountType,
    BookAction,
    BookOrder,
    BookType,
    CryptoPerpetual,
    Currency,
    InstrumentId,
    Money,
    OmsType,
    OrderBookDelta,
    OrderSide,
    OrderStatus,
    Price,
    Quantity,
    Symbol,
    TimeInForce,
    Venue,
)
from nautilus_trader.trading import Strategy, StrategyConfig

from lab.risk_manager import (
    Exposure,
    Intent,
    RiskLimits,
    RiskManagedStrategy,
    RiskManager,
    TradingState,
    add_strategy,
    engine_config,
)
from lab.safety import set_kill_switch

IID = "BTCUSDT-PERP.BINANCE"
MS = 1_000_000
S = 1_000_000_000
DAY_NS = 86_400 * 10**9
USDT = Currency.from_str("USDT")


def limits(**kw):
    base = {
        "currency": "USDT",
        "max_daily_loss": 100.0,
        "max_position_notional": 1_000.0,
        "max_gross_notional": 1_500.0,
        "max_order_notional": 500.0,
        "max_live_orders": 3,
    }
    return RiskLimits(**{**base, **kw})


def manager(tmp_path=None, **kw):
    return RiskManager(limits(**kw), ["BINANCE"])


def started(m, equity=10_000.0, now=0):
    m.update(now, equity)
    return m


def buy(qty, iid=IID, **kw):
    return Intent(iid, "BUY", qty, **kw)


def sell(qty, iid=IID, **kw):
    return Intent(iid, "SELL", qty, **kw)


# ----------------------------------------------------------------------------- limits


@pytest.mark.parametrize(
    "bad",
    [
        {"max_daily_loss": 0},
        {"max_daily_loss": float("nan")},
        {"max_position_notional": -1},
        {"max_gross_notional": True},
        {"max_position_notional": 2_000.0},  # above the gross limit
        {"max_live_orders": 0},
        {"max_order_submit_rate": "100 per second"},
        {"currency": ""},
    ],
)
def test_limits_reject_nonsense(bad):
    with pytest.raises(ValueError):
        limits(**bad)


def test_engine_config_carries_the_native_limits():
    cfg = engine_config(limits(max_order_submit_rate="10/00:00:01"), [IID])
    assert not cfg.bypass
    assert cfg.max_order_submit_rate == "10/00:00:01"
    assert cfg.max_notional_per_order == {IID: "500.0"}
    assert engine_config(limits(), [IID], live=True).max_notional_per_order == {IID: "500.0"}


# ----------------------------------------------------------------------------- the decision


def test_position_limit_counts_the_position_and_every_live_order_on_that_side():
    m = started(manager())
    book = {IID: Exposure(net=0.5, live_buy=0.3, unit_notional=1_000.0, currency="USDT")}
    assert m.check(buy(0.2), book, live_orders=1, equity=10_000.0) is None  # 1.0 BTC = 1000 USDT: at the limit
    reason = m.check(buy(0.21), book, live_orders=1, equity=10_000.0)
    assert reason and "position_limit:" in reason
    # a sell that flips the position is checked on the short side: 0.5 - 1.6 = -1.1 BTC
    assert "position_limit:" in m.check(sell(1.6), book, live_orders=1, equity=10_000.0)


def test_gross_limit_sums_every_instrument():
    m = started(manager())
    eth = "ETHUSDT-PERP.BINANCE"
    book = {
        IID: Exposure(net=0.9, unit_notional=1_000.0, currency="USDT"),
        eth: Exposure(net=-0.2, live_sell=0.1, unit_notional=2_000.0, currency="USDT"),  # worst case 0.6 ETH
    }
    assert m.check(buy(0.0, eth), book, live_orders=0, equity=1e4) == "invalid: quantity must be positive"
    reason = m.check(buy(0.01), book, live_orders=0, equity=10_000.0)  # 910 + 600 = 1510 > 1500
    assert reason and reason.startswith("gross_limit:")
    book[eth].live_sell = 0.05  # 0.25 ETH worst case: 500; 910 + 500 = 1410
    assert m.check(buy(0.01), book, live_orders=0, equity=10_000.0) is None


def test_reducing_orders_pass_while_reducing_but_not_while_halted():
    m = started(manager())
    book = {IID: Exposure(net=0.5, live_sell=0.2, unit_notional=1_000.0, currency="USDT")}
    m.state, m.reason = TradingState.REDUCING, "test"
    assert m.check(sell(0.3), book, live_orders=99, equity=None) is None  # with 0.2 already selling: 0.5 in all
    assert m.check(sell(0.31), book, live_orders=0, equity=10_000.0).startswith("reducing_only:")
    assert m.check(buy(0.1), book, live_orders=0, equity=10_000.0).startswith("reducing_only:")
    assert m.check(buy(5.0, reduce_only=True), book, live_orders=0, equity=None) is None  # the engine enforces it
    m.state = TradingState.HALTED
    assert m.check(sell(0.1), book, live_orders=0, equity=10_000.0).startswith("halted:")


def test_fails_closed_when_it_cannot_value_the_order():
    m = started(manager())
    assert m.check(buy(0.1), {}, live_orders=0, equity=10_000.0) == f"no_price: no price for {IID}"
    ok = buy(0.1, limit_unit_notional=1_000.0, limit_currency="USDT")
    assert m.check(ok, {}, live_orders=0, equity=10_000.0) is None  # the order's own price, when the market has none
    usd = buy(0.1, limit_unit_notional=1_000.0, limit_currency="USD")
    assert "valued in USD" in m.check(usd, {}, live_orders=0, equity=10_000.0)
    assert m.check(ok, {}, live_orders=0, equity=None).startswith("no_equity:")
    other = {"ETHUSDT-PERP.BINANCE": Exposure(net=1.0)}  # a position that cannot be valued
    assert "cannot value" in m.check(ok, other, live_orders=0, equity=10_000.0)
    assert m.check(ok, {}, live_orders=3, equity=10_000.0) == "live_orders: 3 live orders, the cap is 3"
    fresh = manager()
    assert fresh.check(ok, {}, live_orders=0, equity=10_000.0) == "no_equity: no start-of-day equity yet"


# ----------------------------------------------------------------------------- daily loss and kill switch


def test_daily_loss_cap_latches_until_the_next_utc_day_and_survives_a_restart(tmp_path):
    state, audit = tmp_path / "risk.json", tmp_path / "audit.jsonl"
    m = RiskManager(limits(), ["BINANCE"], state_path=str(state), audit_path=str(audit))
    m.update(1 * MS, 10_000.0)
    assert m.state is TradingState.ACTIVE
    m.update(2 * MS, 9_950.0)
    assert m.state is TradingState.ACTIVE
    assert m.update(3 * MS, 9_900.0)  # a 100 loss reaches the cap
    assert m.state is TradingState.REDUCING and "daily loss 100.00 USDT" in m.reason

    again = RiskManager(limits(), ["BINANCE"], state_path=str(state))  # a restart, after the market came back
    again.update(4 * MS, 10_050.0)
    assert again.state is TradingState.REDUCING
    again.update(DAY_NS + 1, 10_050.0)  # the next UTC day starts from the equity seen then
    assert again.state is TradingState.ACTIVE
    assert json.loads(state.read_text()) == {"day": "1970-01-02", "start_equity": 10_050.0, "breach": None}
    kinds = [json.loads(line)["kind"] for line in audit.read_text().splitlines()]
    assert kinds == ["day_start", "state"]


def test_an_unreadable_state_file_halts(tmp_path):
    state = tmp_path / "risk.json"
    state.write_text('{"day": "1970-01-01"')
    m = RiskManager(limits(), ["BINANCE"], state_path=str(state))
    m.update(0, 10_000.0)
    assert m.state is TradingState.HALTED and "unreadable risk state" in m.reason
    assert state.read_text() == '{"day": "1970-01-01"'  # evidence kept for the operator


def test_a_stop_that_cannot_be_persisted_halts(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    m = RiskManager(limits(), ["BINANCE"], state_path=str(blocker / "risk.json"))
    m.update(0, 10_000.0)
    assert m.state is TradingState.HALTED and "cannot write risk state" in m.reason


def test_kill_switch_modes(tmp_path):
    kill = tmp_path / "KILL"
    m = RiskManager(limits(), ["BINANCE"], kill_file=str(kill))
    m.update(0, 10_000.0)
    assert m.state is TradingState.ACTIVE
    set_kill_switch(kill, "halt", "operator")
    m.update(S - 1, 10_000.0)
    assert m.state is TradingState.ACTIVE  # the file is read at most once a second
    m.update(1 * S, 10_000.0)
    assert m.state is TradingState.HALTED and "operator" in m.reason
    set_kill_switch(kill, "flatten", "operator")
    m.update(2 * S, 10_000.0)
    assert m.state is TradingState.REDUCING
    kill.write_text("[1, 2")
    m.update(3 * S, 10_000.0)
    assert m.state is TradingState.HALTED  # any kill file stops trading
    kill.unlink()
    m.update(4 * S, 10_000.0)
    assert m.state is TradingState.ACTIVE


# ----------------------------------------------------------------------------- through NautilusTrader


def falling_market(steps=600, start=1000.0, drop_per_step=0.0):
    """Every 100 ms the book moves to a new mid: 10 levels of 10 BTC each side, spread one 0.1 tick."""
    iid = InstrumentId.from_str(IID)
    deltas, prev, seq = [], {}, 0
    for i in range(steps):
        ts = i * 100 * MS
        mid = round(start - i * drop_per_step, 1)
        new = {}
        for k in range(10):
            new[(OrderSide.BUY, round(mid - k * 0.1, 1))] = 10.0
            new[(OrderSide.SELL, round(mid + 0.1 + k * 0.1, 1))] = 10.0
        ops = [(BookAction.DELETE, key, q) for key, q in prev.items() if key not in new]
        ops += [(BookAction.UPDATE if key in prev else BookAction.ADD, key, q) for key, q in new.items()]
        for j, (action, (side, px), q) in enumerate(ops):
            seq += 1
            order = BookOrder(side, Price(px, 1), Quantity(q, 3), 0)
            deltas.append(OrderBookDelta(iid, action, order, 128 if j == len(ops) - 1 else 0, seq, ts, ts))
        prev = new
    return deltas


def instrument():
    return CryptoPerpetual(
        InstrumentId.from_str(IID), Symbol("BTCUSDT"), Currency.from_str("BTC"), USDT, USDT, False, 1, 3,
        Price.from_str("0.1"), Quantity.from_str("0.001"), 0, 0,
    )  # fmt: skip


class Taker(RiskManagedStrategy):
    """Buys `size` at the ask on every book update while it has no live order: tries to grow without bound.
    Defines its own on_start without calling super(): the risk timer must still run."""

    def __init__(self, risk, size=0.01, passive=False, kill_at=None, kill_mode="halt"):
        super().__init__(StrategyConfig(), risk)
        self.size, self.passive, self.kill_at, self.kill_mode = size, passive, kill_at, kill_mode
        self.denied = []

    def on_start(self):
        self.iid = InstrumentId.from_str(IID)
        self.subscribe_book_deltas(self.iid, BookType.L2_MBP, managed=True)

    def on_risk_denied(self, order, reason):
        self.denied.append((self.clock.timestamp_ns(), reason))

    def on_book_deltas(self, deltas):
        if not deltas.flags & 128:
            return
        now = self.clock.timestamp_ns()
        if self.kill_at is not None and now >= self.kill_at and self.risk.kill_file:
            set_kill_switch(Path(self.risk.kill_file), self.kill_mode, "operator test")
            self.kill_at = None
        if any(not o.is_closed for o in self.cache.orders()):
            return
        book = self.cache.order_book(self.iid)
        inst = self.cache.instrument(self.iid)
        if self.passive:  # rests far below the market: never fills
            order = self.order_factory.limit(self.iid, OrderSide.BUY, inst.make_qty(self.size), Price(500.0, 1))
        else:
            order = self.order_factory.limit(
                self.iid, OrderSide.BUY, inst.make_qty(self.size), book.best_ask_price(), time_in_force=TimeInForce.IOC
            )
        self.submit_order(order)


def run(risk, strategy_factory, *, steps=600, drop_per_step=0.0, max_order_notional=None):
    from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.common import LoggerConfig, LogLevel
    from nautilus_trader.execution import MakerTakerFeeModel

    lim = risk.limits
    native = engine_config(lim, [IID])
    if max_order_notional is not None:
        from nautilus_trader.risk import RiskEngineConfig

        native = RiskEngineConfig(max_notional_per_order={IID: str(max_order_notional)})
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggerConfig(stdout_level=LogLevel.ERROR), risk_engine=native))
    engine.add_venue(
        Venue("BINANCE"), OmsType.NETTING, AccountType.MARGIN, [Money(10_000, USDT)], base_currency=USDT,
        default_leverage=Decimal(1), book_type=BookType.L2_MBP,
        fee_model=MakerTakerFeeModel(Decimal("0.0002"), Decimal("0.0005")),
    )  # fmt: skip
    engine.add_instrument(instrument())
    engine.add_data(falling_market(steps, drop_per_step=drop_per_step))
    strategy = strategy_factory(risk)
    add_strategy(engine, strategy, risk)
    engine.run()
    net = sum(float(p.signed_qty) for p in engine.cache.positions_open())
    orders = list(engine.cache.orders())
    return engine, strategy, net, orders


def test_every_strategy_must_trade_through_the_traders_risk_manager():
    risk = manager()

    class Plain(Strategy):
        pass

    with pytest.raises(TypeError):
        add_strategy(object(), Plain(StrategyConfig()), risk)
    with pytest.raises(ValueError):
        add_strategy(object(), Taker(manager()), risk)
    with pytest.raises(TypeError):
        Taker(object())


def test_position_limit_caps_the_position_in_a_backtest():
    risk = RiskManager(limits(max_position_notional=50.0, max_gross_notional=50.0), ["BINANCE"])
    engine, strategy, net, _ = run(risk, lambda r: Taker(r, size=0.01))
    try:
        assert net == pytest.approx(0.04)  # 0.05 BTC would be 50.0x at ~1000.1: just above the limit
        assert strategy.denied and all("position_limit:" in r for _, r in strategy.denied)
        assert risk.state is TradingState.ACTIVE
    finally:
        engine.dispose()
    control = RiskManager(limits(), ["BINANCE"])  # the same strategy under the default 1000 USDT limit
    engine, _, net, _ = run(control, lambda r: Taker(r, size=0.01))
    try:
        assert net > 0.5  # so it was the limit, not the strategy, that stopped it at 0.04
    finally:
        engine.dispose()


class Modifier(RiskManagedStrategy):
    """Rests one bid far below the market, then tries to enlarge it (denied) and to grow it a little (passes)."""

    def __init__(self, risk):
        super().__init__(StrategyConfig(), risk)
        self.results = []

    def on_start(self):
        self.iid = InstrumentId.from_str(IID)
        self.subscribe_book_deltas(self.iid, BookType.L2_MBP, managed=True)
        self.clock.set_timer("probe", timedelta(seconds=2), callback=self.probe)
        self.step = 0

    def probe(self, _event):
        self.step += 1
        inst = self.cache.instrument(self.iid)
        if self.step == 1:
            order = self.order_factory.limit(self.iid, OrderSide.BUY, inst.make_qty(0.01), Price(500.0, 1))
            self.results.append(self.submit_order(order))
            self.coid = order.client_order_id
        elif self.step == 2:
            self.results.append(self.modify_order(self.coid, quantity=inst.make_qty(1.0)))  # 1 BTC: 1000 > 50
            self.results.append(self.modify_order(self.coid, quantity=inst.make_qty(0.02)))
            self.results.append(self.modify_order(self.coid, price=Price(501.0, 1)))


def test_modify_is_checked_on_the_quantity_it_adds():
    risk = RiskManager(limits(max_position_notional=50.0, max_gross_notional=50.0), ["BINANCE"])
    engine, strategy, _, orders = run(risk, lambda r: Modifier(r), steps=60)
    try:
        assert strategy.results == [True, False, True, True]
        assert float(orders[0].quantity) == 0.02 and float(orders[0].price) == 501.0
    finally:
        engine.dispose()


def test_daily_loss_cap_flattens_and_stops_new_risk(tmp_path):
    audit = tmp_path / "audit.jsonl"
    risk = RiskManager(limits(max_daily_loss=20.0), ["BINANCE"], audit_path=str(audit))
    # buys up to ~1 BTC while the market falls 1 USDT per 100 ms: the 20 USDT cap is hit within seconds
    engine, strategy, net, orders = run(risk, lambda r: Taker(r, size=0.1), steps=300, drop_per_step=1.0)
    try:
        assert risk.state is TradingState.REDUCING and risk.reason.startswith("daily loss")
        assert net == 0.0
        exits = [o for o in orders if o.is_reduce_only and o.status == OrderStatus.FILLED]
        assert exits, "the position was exited by the risk manager's reduce-only orders"
        assert any(r.startswith("reducing_only:") for _, r in strategy.denied)
        breach_ns = next(json.loads(x)["ts_ns"] for x in audit.read_text().splitlines() if '"to": "REDUCING"' in x)
        late_entries = [
            o for o in orders if not o.is_reduce_only and o.status != OrderStatus.DENIED and o.ts_init > breach_ns
        ]
        assert late_entries == []
    finally:
        engine.dispose()


def test_kill_switch_halt_cancels_resting_orders_and_blocks_new_ones(tmp_path):
    kill = tmp_path / "KILL"
    risk = RiskManager(limits(), ["BINANCE"], kill_file=str(kill))
    engine, strategy, _, orders = run(risk, lambda r: Taker(r, size=0.01, passive=True, kill_at=10_000 * MS))
    try:
        assert risk.state is TradingState.HALTED
        resting = [o for o in orders if o.status != OrderStatus.DENIED]
        assert len(resting) == 1 and resting[0].status == OrderStatus.CANCELED
        assert resting[0].ts_last <= 12_000 * MS  # within the 1 s check after the kill file at 10 s
        assert strategy.denied and all(r.startswith("halted:") for _, r in strategy.denied)
    finally:
        engine.dispose()


def test_kill_switch_flatten_exits_the_position(tmp_path):
    kill = tmp_path / "KILL"
    risk = RiskManager(limits(max_position_notional=50.0, max_gross_notional=50.0), ["BINANCE"], kill_file=str(kill))
    engine, _, net, _ = run(risk, lambda r: Taker(r, size=0.01, kill_at=20_000 * MS, kill_mode="flatten"))
    try:
        assert risk.state is TradingState.REDUCING and net == 0.0
    finally:
        engine.dispose()


def test_nautilus_risk_engine_is_the_second_layer():
    risk = RiskManager(limits(), ["BINANCE"])
    engine, strategy, net, orders = run(risk, lambda r: Taker(r, size=0.01), steps=50, max_order_notional=5.0)
    try:
        assert net == 0.0 and orders and all(o.status == OrderStatus.DENIED for o in orders)
        assert not strategy.denied  # passed the first layer, refused by NautilusTrader's own max notional
    finally:
        engine.dispose()


def test_an_error_inside_the_check_denies_the_order():
    class Broken(Taker):
        def _exposures(self):
            raise RuntimeError("cache unavailable")

    risk = RiskManager(limits(), ["BINANCE"])
    engine, strategy, net, orders = run(risk, lambda r: Broken(r), steps=50)
    try:
        assert net == 0.0 and orders == []
        assert strategy.denied[0][1] == "error: RuntimeError: cache unavailable"
    finally:
        engine.dispose()
