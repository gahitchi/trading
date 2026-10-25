"""Pinned Nautilus controlled L2 partial-fill test, OFFLINE ONLY.

A four-frame artificial WS book is reconstructed with verified sequencing,
then converted to native OrderBookDepth. The real BacktestEngine, not a
parallel fill simulator, must produce partial fills with liquidity
consumption enabled. Every observed fill enters the independent Decimal
order/cashflow oracle and is reconciled against Nautilus' margin account.
No actual exchange market data, order permissions or account funding.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nautilus_trader.backtest import BacktestEngine
from nautilus_trader.common import LogLevel
from nautilus_trader.config import BacktestEngineConfig, LoggerConfig
from nautilus_trader.execution import MakerTakerFeeModel
from nautilus_trader.model import (
    AccountType,
    BookType,
    Currency,
    LiquiditySide,
    Money,
    OmsType,
    OrderSide,
    Price,
    Quantity,
    TimeInForce,
    Venue,
)
from nautilus_trader.trading import Strategy, StrategyConfig

from lab.markets.book_sequence import apply_recorded_book_frame
from lab.markets.domain import Side
from lab.markets.execution_audit import AuditOrder, RecordedFill, audit_observed_cashflows
from lab.markets.nautilus_data import to_nautilus_book_depth
from lab.markets.nautilus_instruments import LinearContractDefinition, to_nautilus_perpetual


class NativeL2Error(ValueError):
    """Native matching failed its fixed offline partial-fill accounting contract."""


class _PartialConfig(StrategyConfig):
    def __init__(self, *, instrument_id) -> None:
        super().__init__()
        self.instrument_id = instrument_id


class _PartialProbe(Strategy):
    """Fixed diagnostic order sequence, never a reusable trading strategy."""

    def __init__(self, config: _PartialConfig) -> None:
        super().__init__(config)
        self.instrument_id = config.instrument_id
        self.depth_seen = 0
        self.order_ids: list[str] = []
        self.fills = []
        self.failures: list[str] = []

    def on_start(self) -> None:
        self.subscribe_book_depth(self.instrument_id, BookType.L2_MBP)

    def on_book_depth(self, depth) -> None:
        self.depth_seen += 1
        if self.depth_seen == 2:
            order = self.order_factory.limit(
                self.instrument_id,
                OrderSide.BUY,
                quantity=Quantity.from_str("1.000"),
                price=Price.from_str("100.3"),
                time_in_force=TimeInForce.GTC,
            )
        elif self.depth_seen == 4:
            if not self.portfolio.is_net_long(self.instrument_id):
                self.failures.append("native book did not contain the full partial entry")
                return
            order = self.order_factory.market(
                self.instrument_id,
                OrderSide.SELL,
                quantity=Quantity.from_str("1.000"),
                time_in_force=TimeInForce.GTC,
            )
        else:
            return
        self.order_ids.append(str(order.client_order_id))
        self.submit_order(order)

    def on_order_filled(self, event) -> None:
        self.fills.append(event)

    def on_order_denied(self, event) -> None:
        self.failures.append("native order denied")

    def on_order_rejected(self, event) -> None:
        self.failures.append("native order rejected")


@dataclass(frozen=True, slots=True)
class NativeL2Parity:
    fill_count: int
    entry_parts: tuple[Decimal, ...]
    entry_prices: tuple[Decimal, ...]
    realized_pnl: Decimal
    charged_fees: Decimal
    ending_balance: Decimal
    native_account_parity_verified: bool = True
    source: str = "synthetic_offline_ws_l2"
    broker_execution_enabled: bool = False
    market_data_qualified: bool = False
    actual_queue_priority_verified: bool = False


def _frame(action: str, seq: int, prev: int, ts: int, bids, asks) -> dict:
    return {
        "arg": {"channel": "books", "instId": "BTC-USD-SWAP"},
        "action": action,
        "data": [
            {
                "ts": str(ts),
                "seqId": seq,
                "prevSeqId": prev,
                "checksum": 0,
                "bids": bids,
                "asks": asks,
            }
        ],
    }


def _controlled_depth(spec: LinearContractDefinition):
    if (
        spec.metadata_source != "offline_fixture"
        or spec.instrument.venue != "SIM"
        or spec.instrument.symbol != "BTC-USD-SWAP"
        or spec.instrument.tick_size != Decimal("0.1")
        or spec.instrument.quantity_step != Decimal("0.001")
        or spec.instrument.multiplier != 1
    ):
        raise NativeL2Error("only the pinned artificial linear L2 fixture is accepted")

    frames = (
        _frame("snapshot", 1, -1, 1000, [["100.0", "10.000", "0", "1"]], [["100.2", "10.000", "0", "1"]]),
        _frame("update", 2, 1, 2000, [], [["100.2", "0.400", "0", "1"]]),
        _frame("update", 3, 2, 3000, [], [["100.2", "0", "0", "0"], ["100.3", "0.600", "0", "1"]]),
        _frame(
            "update",
            4,
            3,
            4000,
            [["100.0", "0", "0", "0"], ["101.0", "10.000", "0", "1"]],
            [["100.3", "0", "0", "0"], ["101.2", "10.000", "0", "1"]],
        ),
    )
    state = None
    output = []
    for i, frame in enumerate(frames, start=1):
        state = apply_recorded_book_frame(state, frame, received_ns=i * 1_000_000_000)
        output.append(to_nautilus_book_depth(state, spec.instrument))
    return tuple(output)


def run_native_l2_partial_parity(
    spec: LinearContractDefinition,
    *,
    collateral: Decimal = Decimal("1000"),
) -> NativeL2Parity:
    """Prove native depth-driven partial fills and exact independent cashflows."""
    if type(collateral) is not Decimal or collateral != Decimal("1000"):
        raise NativeL2Error("fixed native L2 scenario requires exact 1000 quote collateral")
    depth = _controlled_depth(spec)
    native = to_nautilus_perpetual(spec)
    currency = Currency.from_str(spec.instrument.quote_currency)
    venue = Venue("SIM")
    probe = _PartialProbe(_PartialConfig(instrument_id=native.id))
    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggerConfig(stdout_level=LogLevel.ERROR)))
    try:
        engine.add_venue(
            venue=venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money.from_decimal(collateral, currency)],
            base_currency=currency,
            default_leverage=Decimal("1"),
            book_type=BookType.L2_MBP,
            liquidity_consumption=True,
            fee_model=MakerTakerFeeModel(
                maker_rate=spec.maker_fee_bps / Decimal("10000"),
                taker_rate=spec.taker_fee_bps / Decimal("10000"),
            ),
        )
        engine.add_instrument(native)
        engine.add_strategy(probe)
        engine.add_data(list(depth))
        engine.run()
        if probe.failures or probe.depth_seen != 4 or len(probe.order_ids) != 2 or len(probe.fills) != 3:
            observed_fills = tuple(
                (
                    str(fill.client_order_id),
                    str(fill.order_side),
                    str(fill.last_qty),
                    str(fill.last_px),
                    str(fill.liquidity_side),
                )
                for fill in probe.fills
            )
            raise NativeL2Error(
                "native L2 lifecycle mismatch: "
                f"depth={probe.depth_seen}, orders={len(probe.order_ids)}, "
                f"fills={observed_fills}, errors={probe.failures}"
            )
        expected_qty = (Decimal("0.400"), Decimal("0.600"), Decimal("1.000"))
        expected_px = (Decimal("100.2"), Decimal("100.3"), Decimal("101.0"))
        if (
            tuple(Decimal(str(fill.last_qty)) for fill in probe.fills) != expected_qty
            or tuple(Decimal(str(fill.last_px)) for fill in probe.fills) != expected_px
            or any(fill.liquidity_side != LiquiditySide.TAKER for fill in probe.fills)
            or [str(fill.client_order_id) for fill in probe.fills]
            != [probe.order_ids[0], probe.order_ids[0], probe.order_ids[1]]
        ):
            raise NativeL2Error("native L2 fill was not liquidity-limited at each price level")
        if not probe.portfolio.is_net_flat(native.id):
            raise NativeL2Error("native L2 round-trip did not flatten the position")
        receipts = tuple(
            RecordedFill(
                str(fill.client_order_id),
                str(fill.trade_id),
                Side.BUY if fill.order_side == OrderSide.BUY else Side.SELL,
                Decimal(str(fill.last_qty)),
                Decimal(str(fill.last_px)),
                fill.commission.as_decimal(),
                fill.ts_init,
            )
            for fill in probe.fills
        )
        audit = audit_observed_cashflows(
            spec.instrument,
            (
                AuditOrder(probe.order_ids[0], Side.BUY, Decimal("1")),
                AuditOrder(probe.order_ids[1], Side.SELL, Decimal("1")),
            ),
            receipts,
            collateral=collateral,
            taker_fee_bps=spec.taker_fee_bps,
            currency_precision=currency.precision,
        )
        account = engine.cache.account_for_venue(venue)
        balance = account.balance_total(currency) if account is not None else None
        if (
            balance is None
            or audit.final_position != 0
            or any(left != 0 for _, left in audit.remaining_orders)
            or audit.realized_pnl != Decimal("0.7400")
            or audit.charged_fees != Decimal("0.10")
            or audit.ending_balance != Decimal("1000.64")
            or audit.ending_balance != balance.as_decimal()
            or len(engine.generate_fills_report()) != 3
        ):
            raise NativeL2Error("native L2 account disagrees with exact partial-fill accounting")
        return NativeL2Parity(
            fill_count=3,
            entry_parts=expected_qty[:2],
            entry_prices=expected_px[:2],
            realized_pnl=audit.realized_pnl,
            charged_fees=audit.charged_fees,
            ending_balance=audit.ending_balance,
        )
    finally:
        engine.dispose()
