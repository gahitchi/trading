"""Credential-free native Nautilus fill/accounting parity experiment.

Only an explicitly artificial offline quote fixture may enter. This is a
diagnostic executable scenario, NOT a strategy, broker client, live/paper
trade entry point, price-impact model, or qualified market-data replay.
Nautilus is the only matching/order engine; independent exact Decimal
arithmetic validates its observed fills and native account after settlement.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import (
    AccountType,
    Currency,
    LiquiditySide,
    Money,
    OmsType,
    OrderSide,
    Price,
    Quantity,
    QuoteTick,
    TimeInForce,
    Venue,
)
from nautilus_trader.trading import Strategy, StrategyConfig

from lab.markets.accounting import linear_transition
from lab.markets.domain import MAX_EVENT_TIMESTAMP_NS, Side, decimal, positive
from lab.markets.nautilus_instruments import LinearContractDefinition, to_nautilus_perpetual


class NativeParityError(ValueError):
    """An input, simulated execution, or independent accounting mismatch."""


@dataclass(frozen=True, slots=True)
class NativeFillParity:
    instrument: str
    entry_side: Side
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    gross_pnl: Decimal
    charged_fees: Decimal
    net_pnl: Decimal
    ending_balance: Decimal
    expected_ending_balance: Decimal
    fills: int
    source: str = "synthetic_offline_quotes"
    execution_cost_parity_verified: bool = True
    broker_execution_enabled: bool = False
    qualified_for_live_trading: bool = False


class _RoundTripConfig(StrategyConfig):
    def __init__(self, *, instrument_id, quantity: Quantity, entry_side: OrderSide) -> None:
        super().__init__()
        self.instrument_id = instrument_id
        self.quantity = quantity
        self.entry_side = entry_side


class _RoundTripProbe(Strategy):
    """Fixed two-order test; not reusable as a trading signal or production strategy."""

    def __init__(self, config: _RoundTripConfig) -> None:
        super().__init__(config)
        self.instrument_id = config.instrument_id
        self.quantity = config.quantity
        self.entry_side = config.entry_side
        self.quotes_seen = 0
        self.fills = []
        self.failures = []
        self.order_ids = []

    def on_start(self) -> None:
        self.subscribe_quotes(self.instrument_id)

    def on_quote(self, tick: QuoteTick) -> None:
        self.quotes_seen += 1
        if self.quotes_seen == 2:
            side = self.entry_side
        elif self.quotes_seen == 3:
            # A cover requires an actual native short, not an assumed fill.
            if self.entry_side == OrderSide.BUY:
                if not self.portfolio.is_net_long(self.instrument_id):
                    self.failures.append("entry not long before scheduled exit")
                    return
                side = OrderSide.SELL
            else:
                if not self.portfolio.is_net_short(self.instrument_id):
                    self.failures.append("entry not short before scheduled cover")
                    return
                side = OrderSide.BUY
        else:
            return
        order = self.order_factory.market(self.instrument_id, side, self.quantity, time_in_force=TimeInForce.GTC)
        self.order_ids.append(str(order.client_order_id))
        self.submit_order(order)

    def on_order_filled(self, event) -> None:
        self.fills.append(event)

    def on_order_rejected(self, event) -> None:
        self.failures.append("native order rejected")

    def on_order_denied(self, event) -> None:
        self.failures.append("native order denied")


def _quote_identity(q: QuoteTick) -> tuple[object, ...]:
    return (
        str(q.instrument_id),
        q.ts_event,
        q.ts_init,
        str(q.bid_price),
        str(q.ask_price),
        str(q.bid_size),
        str(q.ask_size),
    )


def _validate_quotes(spec: LinearContractDefinition, quotes: tuple[QuoteTick, ...], qty: Decimal) -> None:
    if spec.metadata_source != "offline_fixture":
        raise NativeParityError("native fill parity only accepts offline_fixture contracts")
    if len(quotes) != 4:
        raise NativeParityError("controlled round trip requires exactly four artificial quotes")
    spec.instrument.check_quantity(qty)
    native = to_nautilus_perpetual(spec)
    previous = -1
    for q in quotes:
        if type(q) is not QuoteTick or q.instrument_id != native.id:
            raise NativeParityError("quote has wrong native type or instrument")
        if (
            type(q.ts_event) is not int
            or type(q.ts_init) is not int
            or not 0 <= q.ts_event <= q.ts_init <= MAX_EVENT_TIMESTAMP_NS
            or q.ts_init <= previous
        ):
            raise NativeParityError("quote timestamps are noncausal or non-increasing")
        previous = q.ts_init
        bid, ask = Decimal(str(q.bid_price)), Decimal(str(q.ask_price))
        spec.instrument.check_price(bid)
        spec.instrument.check_price(ask)
        if bid >= ask:
            raise NativeParityError("artificial bid/ask must be positive and uncrossed")
        if Decimal(str(q.bid_size)) < qty or Decimal(str(q.ask_size)) < qty:
            raise NativeParityError("top-of-book size insufficient for fully controlled fill")
    if spec.observed_ns > quotes[0].ts_init:
        raise NativeParityError("contract metadata observed after the first test quote")
    # Reject external quote streams disguised as the artificial deterministic fixture.
    if tuple(_quote_identity(q) for q in quotes) != tuple(_quote_identity(q) for q in synthetic_quotes(spec)):
        raise NativeParityError("native parity requires the exact fixed synthetic quote fixture")


def synthetic_quotes(spec: LinearContractDefinition) -> tuple[QuoteTick, ...]:
    """Four explicit synthetic BBOs. Never represent these as captured market depth."""
    c = spec.instrument
    native = to_nautilus_perpetual(spec)
    if c.tick_size != Decimal("0.1") or c.quantity_step != Decimal("0.001"):
        raise NativeParityError("test fixture is qualified only for its exact artificial price/size grid")
    result = []
    for i, (bid, ask) in enumerate(
        (("100.0", "100.2"), ("100.0", "100.2"), ("101.0", "101.2"), ("101.0", "101.2")),
        start=1,
    ):
        stamp = i * 1_000_000_000
        result.append(
            QuoteTick(
                native.id,
                Price.from_str(bid),
                Price.from_str(ask),
                Quantity.from_str("10.000"),
                Quantity.from_str("10.000"),
                stamp,
                stamp,
            )
        )
    return tuple(result)


def run_native_fill_parity(
    spec: LinearContractDefinition,
    quotes: tuple[QuoteTick, ...],
    *,
    quantity: Decimal = Decimal("1.000"),
    collateral: Decimal = Decimal("1000"),
    side: Side = Side.BUY,
) -> NativeFillParity:
    """Verify a synthetic native BUY/SELL against the independent Decimal oracle.

    No fills are fabricated, returned success requires two observed TAKER fills,
    exact controlled bid/ask execution, fees charged by Nautilus' venue, and
    final native margin-account balance equality. No broker adapters exist.
    """
    if type(quantity) is not Decimal or type(collateral) is not Decimal:
        raise TypeError("quantity and collateral must be exact Decimal values")
    if not isinstance(side, Side):
        raise TypeError("entry side must be a typed Side")
    positive(decimal(quantity), "quantity")
    positive(decimal(collateral), "collateral")
    _validate_quotes(spec, quotes, quantity)
    c = spec.instrument
    native = to_nautilus_perpetual(spec)
    currency = Currency.from_str(c.quote_currency)
    venue = Venue(c.venue)
    rate = spec.taker_fee_bps / Decimal("10000")
    # The oracle must not use Nautilus Money.from_decimal to derive its expected values.
    # Validate collateral precision before allowing the engine to round starting equity.
    quantum = Decimal("1").scaleb(-currency.precision)
    if collateral != collateral.quantize(quantum):
        raise NativeParityError("starting collateral exceeds quote currency precision")
    if rate <= 0:
        raise NativeParityError("fee parity requires a strictly positive taker fee")
    entry_side = OrderSide.BUY if side == Side.BUY else OrderSide.SELL
    exit_side = OrderSide.SELL if side == Side.BUY else OrderSide.BUY
    probe = _RoundTripProbe(
        _RoundTripConfig(instrument_id=native.id, quantity=Quantity.from_str(str(quantity)), entry_side=entry_side)
    )
    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggerConfig(stdout_level=LogLevel.ERROR)))
    try:
        engine.add_venue(
            venue=venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money.from_decimal(collateral, currency)],
            base_currency=currency,
            default_leverage=Decimal("1"),
            fee_model=MakerTakerFeeModel(
                maker_rate=spec.maker_fee_bps / Decimal("10000"),
                taker_rate=rate,
            ),
        )
        engine.add_instrument(native)
        engine.add_strategy(probe)
        engine.add_data(list(quotes))
        engine.run()
        if probe.failures or probe.quotes_seen != len(quotes) or len(probe.order_ids) != 2:
            raise NativeParityError("native strategy did not complete its fixed order schedule")
        if len(probe.fills) != 2:
            raise NativeParityError("native engine did not produce exactly two observed fills")
        entry, exit_ = probe.fills
        if (
            entry.order_side != entry_side
            or exit_.order_side != exit_side
            or entry.liquidity_side != LiquiditySide.TAKER
            or exit_.liquidity_side != LiquiditySide.TAKER
            or len({str(entry.trade_id), str(exit_.trade_id)}) != 2
            or [str(entry.client_order_id), str(exit_.client_order_id)] != probe.order_ids
        ):
            raise NativeParityError("native order sides, attribution, or taker liquidity do not match")
        q1, q2 = Decimal(str(entry.last_qty)), Decimal(str(exit_.last_qty))
        px1, px2 = Decimal(str(entry.last_px)), Decimal(str(exit_.last_px))
        if (
            q1 != quantity
            or q2 != quantity
            or px1 != Decimal(str(quotes[1].ask_price if side == Side.BUY else quotes[1].bid_price))
            or px2 != Decimal(str(quotes[2].bid_price if side == Side.BUY else quotes[2].ask_price))
        ):
            raise NativeParityError("native fill violates expected synthetic quote price/quantity")
        if not quotes[1].ts_init <= entry.ts_event <= quotes[2].ts_init:
            raise NativeParityError("entry fill occurred before the decision or after the exit decision")
        if not quotes[2].ts_init <= exit_.ts_event <= quotes[3].ts_init:
            raise NativeParityError("exit fill occurred before its decision or after test termination")
        if not probe.portfolio.is_net_flat(native.id):
            raise NativeParityError("native round trip did not end flat")

        # Independent price/quantity accounting and Decimal currency quantization.
        # Neither expected fees nor expected balance come from Nautilus Money.
        signed_quantity = quantity * side.sign
        opened = linear_transition(Decimal("0"), Decimal("0"), signed_quantity, px1, c.multiplier)
        closed = linear_transition(opened.position, opened.entry_price, -signed_quantity, px2, c.multiplier)
        if closed.position != 0:
            raise NativeParityError("independent position oracle did not end flat")
        expected_fees = Decimal("0")
        for price in (px1, px2):
            expected_fees += (price * quantity * c.multiplier * rate).quantize(quantum, rounding=ROUND_HALF_UP)
        charges = []
        for fill in probe.fills:
            if fill.commission.currency != currency:
                raise NativeParityError("native fee currency is not quote collateral")
            charges.append(fill.commission.as_decimal())
        charged = sum(charges, Decimal("0"))
        if charged != expected_fees or charged <= 0:
            raise NativeParityError("native commission differs from the independent BPS cost schedule")
        account = engine.cache.account_for_venue(venue)
        balance = account.balance_total(currency) if account is not None else None
        if balance is None:
            raise NativeParityError("missing native account or collateral balance")
        ending = balance.as_decimal()
        expected_ending = (collateral + closed.realized_pnl - expected_fees).quantize(quantum, rounding=ROUND_HALF_UP)
        if ending != expected_ending:
            raise NativeParityError("native margin account disagrees with exact Decimal PnL and charged fees")
        if len(engine.generate_fills_report()) != 2:
            raise NativeParityError("native cached fill report disagrees with fill callbacks")
        return NativeFillParity(
            instrument=str(native.id),
            entry_side=side,
            entry_price=px1,
            exit_price=px2,
            quantity=quantity,
            gross_pnl=closed.realized_pnl,
            charged_fees=charged,
            net_pnl=closed.realized_pnl - charged,
            ending_balance=ending,
            expected_ending_balance=expected_ending,
            fills=len(probe.fills),
        )
    finally:
        engine.dispose()
