"""Single NautilusTrader read-only crypto replay runtime (offline fixtures only).

This is a diagnostic *actor* within the existing Nautilus BacktestEngine, not
a strategy, allocator, second simulator or OMS. It proves instrument, native
data engine subscription, causal trade/mark delivery and account/fee setup.

Explicitly does not submit orders. No real venue, credentials, market strategy
or proprietary connector can enter this module. External demo and live modes
remain rejected by lab.markets.adapters independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import DataActor, DataActorConfig, LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import AccountType, Currency, InstrumentId, Money, OmsType, Venue

from lab.markets.domain import decimal, positive
from lab.markets.nautilus_data import CausalNautilusTape
from lab.markets.nautilus_instruments import LinearContractDefinition, to_nautilus_perpetual

_MAX_EVENTS = 10_000


def _fingerprint(event) -> tuple[str, ...]:
    """Exact native event identity and economics; never reduce delivery proof to counts."""
    kind = type(event).__name__
    common = (str(event.instrument_id), str(event.ts_event), str(event.ts_init))
    if kind == "TradeTick":
        return ("trade", *common, str(event.trade_id), str(event.price), str(event.size))
    if kind == "MarkPriceUpdate":
        return ("mark", *common, str(event.value))
    if kind == "QuoteTick":
        return (
            "quote",
            *common,
            str(event.bid_price),
            str(event.ask_price),
            str(event.bid_size),
            str(event.ask_size),
        )
    raise TypeError("unexpected event type in native replay")


class _ReadOnlyTapeObserver(DataActor):
    """Native actor used only to verify true exchange/clock event delivery."""

    def __init__(self, instrument_id: InstrumentId) -> None:
        super().__init__(DataActorConfig())
        self._instrument_id = instrument_id
        self.trades = 0
        self.marks = 0
        self.quotes = 0
        self.received_ns: list[int] = []
        self.clock_samples_ns: list[int] = []
        self.delivered_fingerprints: list[tuple[str, ...]] = []

    def on_start(self) -> None:
        self.subscribe_trades(self._instrument_id)
        self.subscribe_mark_prices(self._instrument_id)
        self.subscribe_quotes(self._instrument_id)

    def _record(self, event) -> None:
        self.clock_samples_ns.append(self.clock.timestamp_ns())
        self.received_ns.append(event.ts_init)
        self.delivered_fingerprints.append(_fingerprint(event))

    def on_trade(self, tick) -> None:
        self.trades += 1
        self._record(tick)

    def on_mark_price(self, mark) -> None:
        self.marks += 1
        self._record(mark)

    def on_quote(self, tick) -> None:
        self.quotes += 1
        self._record(tick)


@dataclass(frozen=True, slots=True)
class NativeReadOnlyReplay:
    native_instrument_id: str
    trade_observations: int
    mark_observations: int
    quote_observations: int
    first_received_ns: int
    last_received_ns: int
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    native_data_engine_used: bool = True
    event_integrity_verified: bool = False
    clock_receipt_verified: bool = False
    fees_charged_verified: bool = False
    broker_execution_enabled: bool = False
    strategies_qualified: bool = False


def replay_native_observations(
    spec: LinearContractDefinition,
    tape: CausalNautilusTape,
    *,
    collateral: Decimal = Decimal("1000"),
) -> NativeReadOnlyReplay:
    """Replay bounded offline observations using the *actual* Nautilus engine.

    Only a synthetic offline-fixture source is permitted. Exchange snapshots
    downloaded later must NOT be backdated into decision-time evidence.
    Maker/taker rates are set on Nautilus' venue fee model (never silently
    discarded), but no actual fee charge or execution PnL is claimed until
    instrument-specific order/fill parity has independently been proved.
    """
    if spec.metadata_source != "offline_fixture":
        raise ValueError("native pilot refuses unqualified external market data")
    if tape.instrument_id != spec.instrument.instrument_id:
        raise ValueError("native tape and contract identity mismatch")
    if tape.execution_qualified is not False:
        raise ValueError("pilot may not claim qualified execution data")
    if not 0 < len(tape.events) <= _MAX_EVENTS:
        raise ValueError("native pilot requires bounded, nonempty data")
    if spec.observed_ns > tape.events[0].ts_init:
        raise ValueError("contract metadata was received after the first replay observation")
    if not isinstance(collateral, Decimal):
        raise TypeError("collateral must be an exact Decimal")
    positive(decimal(collateral), "starting collateral")
    native = to_nautilus_perpetual(spec)
    observed = -1
    expected_trades, expected_marks, expected_quotes = 0, 0, 0
    for event in tape.events:
        if event.instrument_id != native.id:
            raise ValueError("native event belongs to another instrument")
        if event.ts_init < observed or event.ts_event > event.ts_init:
            raise ValueError("noncausal or out-of-order native receipt timestamps")
        observed = event.ts_init
        if type(event).__name__ == "TradeTick":
            expected_trades += 1
        elif type(event).__name__ == "MarkPriceUpdate":
            expected_marks += 1
        elif type(event).__name__ == "QuoteTick":
            expected_quotes += 1
        else:
            raise TypeError("unsupported native observation in read-only pilot")
    if observed != tape.last_received_ns:
        raise ValueError("tape terminal receipt timestamp disagrees with native events")

    c = spec.instrument
    quote = Currency.from_str(c.quote_currency)
    engine = BacktestEngine(
        config=BacktestEngineConfig(logging=LoggerConfig(stdout_level=LogLevel.ERROR)),
    )
    observer = _ReadOnlyTapeObserver(native.id)
    try:
        engine.add_venue(
            venue=Venue(c.venue),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money.from_str(f"{collateral} {c.quote_currency}")],
            base_currency=quote,
            default_leverage=Decimal("1"),
            fee_model=MakerTakerFeeModel(
                maker_rate=spec.maker_fee_bps / Decimal("10000"),
                taker_rate=spec.taker_fee_bps / Decimal("10000"),
            ),
        )
        engine.add_instrument(native)
        engine.add_actor(observer)
        engine.add_data(list(tape.events))
        engine.run()
        if (observer.trades, observer.marks, observer.quotes) != (
            expected_trades,
            expected_marks,
            expected_quotes,
        ):
            raise ValueError("Nautilus data engine did not deliver the full observed tape")
        if observer.delivered_fingerprints != [_fingerprint(event) for event in tape.events]:
            raise ValueError("Nautilus replay altered event contents or receipt-order delivery")
        if observer.clock_samples_ns != observer.received_ns:
            raise ValueError("Nautilus actor clock disagrees with observation receipt times")
        if not observer.received_ns:
            raise ValueError("Nautilus did not deliver the provided tape")
        return NativeReadOnlyReplay(
            native_instrument_id=str(native.id),
            trade_observations=observer.trades,
            mark_observations=observer.marks,
            quote_observations=observer.quotes,
            first_received_ns=observer.received_ns[0],
            last_received_ns=observer.received_ns[-1],
            maker_fee_rate=spec.maker_fee_bps / Decimal("10000"),
            taker_fee_rate=spec.taker_fee_bps / Decimal("10000"),
            event_integrity_verified=True,
            clock_receipt_verified=True,
        )
    finally:
        engine.dispose()
