"""Pure multi-instrument risk preflight for a future shared Nautilus risk boundary.

This is NOT an executor or a second trading-agent pipeline. It audits potential
pending-order outcomes for one quote-collateral, LINEAR derivatives account.
No FX conversions, cross-venue netting, options, inverse contracts or margin
tiers are guessed. A missing contract parameter fails closed.

Every independent pending order can fill 0%, 50% or 100% of its requested
quantity. This samples 3**N partial-fill states and takes the worst case.
It is a bounded conservative screening oracle, NOT a mathematical proof for
every possible partial fill, intrafill path, or exchange liquidation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import product

from lab.markets.accounting import linear_notional, linear_transition, linear_unrealized
from lab.markets.domain import (
    AssetClass,
    ContractKind,
    Instrument,
    OrderIntent,
    Side,
    decimal,
    positive,
)

_STEPS = (Decimal("0"), Decimal("0.5"), Decimal("1"))
_BPS = Decimal("10000")
_NS_LIMIT = 2**63 - 1


@dataclass(frozen=True, slots=True)
class OpenPosition:
    instrument: Instrument
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    mark_ns: int

    def __post_init__(self) -> None:
        if self.instrument.asset_class != AssetClass.CRYPTO or self.instrument.kind != ContractKind.LINEAR_DERIVATIVE:
            raise ValueError("risk preflight supports quote-collateral crypto linear derivatives only")
        if self.instrument.multiplier <= 0:
            raise ValueError("contract multiplier must be positive")
        if not isinstance(self.quantity, Decimal) or not isinstance(self.entry_price, Decimal):
            raise TypeError("position quantity and entry must be exact Decimal")
        decimal(self.quantity)
        if self.quantity:
            positive(decimal(self.entry_price), "position entry")
        elif self.entry_price != 0:
            raise ValueError("flat position must have zero entry")
        if not isinstance(self.mark_price, Decimal):
            raise TypeError("mark must be exact Decimal")
        positive(decimal(self.mark_price), "mark")
        if type(self.mark_ns) is not int or not 0 <= self.mark_ns <= _NS_LIMIT:
            raise ValueError("mark timestamp invalid")


@dataclass(frozen=True, slots=True)
class RiskCeilings:
    max_gross: Decimal = Decimal("1.0")
    max_instrument_fraction: Decimal = Decimal("0.5")
    max_margin_fraction: Decimal = Decimal("0.65")
    max_drawdown: Decimal = Decimal("0.1")
    max_daily_loss: Decimal = Decimal("0.04")
    max_loss_currency: Decimal = Decimal("40")
    price_gap_bps: Decimal = Decimal("300")
    slippage_bps: Decimal = Decimal("10")
    fee_bps: Decimal = Decimal("10")
    funding_buffer_bps: Decimal = Decimal("20")
    max_mark_age_ns: int = 120_000_000_000
    max_pending: int = 6

    def __post_init__(self) -> None:
        for name in (
            "max_gross",
            "max_instrument_fraction",
            "max_margin_fraction",
            "max_drawdown",
            "max_daily_loss",
            "max_loss_currency",
            "price_gap_bps",
            "slippage_bps",
            "fee_bps",
            "funding_buffer_bps",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be an exact Decimal")
            decimal(value)
        if not 0 < self.max_gross <= 3:
            raise ValueError("max gross must be in (0, 3]")
        for name in ("max_instrument_fraction", "max_margin_fraction", "max_drawdown", "max_daily_loss"):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if self.max_loss_currency <= 0:
            raise ValueError("max monetary loss must be positive")
        for name in ("price_gap_bps", "slippage_bps", "fee_bps", "funding_buffer_bps"):
            if not 0 <= getattr(self, name) <= 5000:
                raise ValueError(f"{name} must be in [0, 5000] bps")
        if type(self.max_pending) is not int or not 0 <= self.max_pending <= 8:
            raise ValueError("max pending orders must be <= 8")
        if type(self.max_mark_age_ns) is not int or self.max_mark_age_ns <= 0:
            raise ValueError("invalid mark max age")


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    allowed: bool
    breaches: tuple[str, ...]
    tested_scenarios: int
    worst_equity: Decimal
    worst_margin: Decimal
    worst_gross: Decimal

    def require_safe(self) -> None:
        if not self.allowed:
            raise ValueError("pending exposure preflight refused: " + ", ".join(self.breaches))


def _reduction(position: Decimal, delta: Decimal) -> bool:
    return position != 0 and position * delta < 0 and abs(delta) <= abs(position)


def preflight_pending(
    positions: tuple[OpenPosition, ...],
    pending: tuple[OrderIntent, ...],
    *,
    collateral_currency: str,
    equity: Decimal,
    peak_equity: Decimal,
    day_open_equity: Decimal,
    asof_ns: int,
    ceilings: RiskCeilings | None = None,
) -> RiskVerdict:
    """Fail closed if ANY simulated fill subset breaches shared collateral limits.

    Equity is the actual account mark-to-market equity measured at each input
    mark, not a wallet balance or free margin. Peak/day-open must be operator
    or accounting evidence, not adjusted down by the strategy.
    """
    cap = ceilings or RiskCeilings()
    for name, value in (("equity", equity), ("peak_equity", peak_equity), ("day_open_equity", day_open_equity)):
        if not isinstance(value, Decimal):
            raise TypeError(f"{name} must be Decimal")
        positive(decimal(value), name)
    if equity > peak_equity:
        raise ValueError("peak equity cannot be below current equity")
    if type(asof_ns) is not int or not 0 <= asof_ns <= _NS_LIMIT:
        raise ValueError("invalid as-of event timestamp")
    if not isinstance(collateral_currency, str) or not collateral_currency.isascii():
        raise ValueError("invalid collateral currency")
    if len(pending) > cap.max_pending:
        raise ValueError("too many simultaneous pending orders; no unsafe approximation")
    instrument_by_id = {}
    for state in positions:
        iid = state.instrument.instrument_id
        if iid in instrument_by_id:
            raise ValueError("duplicate instrument, net the positions before risk evaluation")
        if state.instrument.quote_currency != collateral_currency:
            raise ValueError("unconverted cross-currency collateral is unsupported")
        if state.mark_ns > asof_ns or asof_ns - state.mark_ns > cap.max_mark_age_ns:
            raise ValueError("missing or stale independent valuation mark")
        instrument_by_id[iid] = state
    seen_orders = set()
    for intent in pending:
        if intent.client_order_id in seen_orders:
            raise ValueError("duplicate order identity")
        seen_orders.add(intent.client_order_id)
        if intent.instrument_id not in instrument_by_id:
            raise ValueError("unknown instrument, mark or margin schedule")
        state = instrument_by_id[intent.instrument_id]
        state.instrument.check_quantity(intent.quantity)
        if intent.created_ns > asof_ns or intent.created_ns < state.mark_ns - cap.max_mark_age_ns:
            raise ValueError("future or unqualified order timestamp")
    breaches = set()
    worst_equity, worst_margin, worst_gross = equity, Decimal("0"), Decimal("0")
    evaluated = 0
    # Every position is evaluated even when the caller supplies no new orders.
    for fractions in product(_STEPS, repeat=len(pending)):
        evaluated += 1
        holdings = {
            iid: (state.quantity, state.entry_price) for iid, state in instrument_by_id.items()
        }
        projected_equity = equity
        for order, fraction in zip(pending, fractions, strict=True):
            if not fraction:
                continue
            state = instrument_by_id[order.instrument_id]
            q, entry = holdings[order.instrument_id]
            delta = Decimal(order.side.sign) * order.quantity * fraction
            # A reduce-only order cannot open or reverse; venue enforces this
            # independently at fill time. Illegal sub-fills cannot execute.
            if order.reduce_only and not _reduction(q, delta):
                continue
            px = state.mark_price
            signed_slip = cap.slippage_bps / _BPS * (1 if order.side == Side.BUY else -1)
            fill_price = px * (1 + signed_slip)
            transition = linear_transition(q, entry, delta, fill_price, state.instrument.multiplier)
            old_marked = linear_unrealized(q, entry, px, state.instrument.multiplier)
            new_marked = linear_unrealized(
                transition.position, transition.entry_price, px, state.instrument.multiplier
            )
            fee = abs(delta) * fill_price * state.instrument.multiplier * cap.fee_bps / _BPS
            projected_equity += transition.realized_pnl + new_marked - old_marked - fee
            holdings[order.instrument_id] = (transition.position, transition.entry_price)
        stressed_equity = projected_equity
        gross = Decimal("0")
        margin = Decimal("0")
        instrument_notionals = []
        for iid, state in instrument_by_id.items():
            qty, _entry = holdings[iid]
            shock = cap.price_gap_bps / _BPS
            adverse_mark = state.mark_price * (1 - shock if qty > 0 else 1 + shock)
            stressed_equity += (adverse_mark - state.mark_price) * qty * state.instrument.multiplier
            notional = linear_notional(qty, state.mark_price, state.instrument.multiplier)
            adverse_notional = linear_notional(qty, adverse_mark, state.instrument.multiplier)
            gross += max(notional, adverse_notional)
            margin += max(notional, adverse_notional) * state.instrument.initial_margin
            instrument_notionals.append(max(notional, adverse_notional))
        # Future unknown funding/fee risk is a cash buffer, NOT a fabricated
        # funding settlement. Its sign is always adverse to the account.
        stressed_equity -= gross * cap.funding_buffer_bps / _BPS
        worst_equity = min(worst_equity, stressed_equity)
        worst_margin = max(worst_margin, margin)
        worst_gross = max(worst_gross, gross)
        if stressed_equity <= 0:
            breaches.add("nonpositive_stressed_equity")
            continue
        if gross > cap.max_gross * stressed_equity:
            breaches.add("gross_leverage")
        if any(n > cap.max_instrument_fraction * stressed_equity for n in instrument_notionals):
            breaches.add("instrument_concentration")
        if margin > cap.max_margin_fraction * stressed_equity:
            breaches.add("initial_margin")
        if peak_equity - stressed_equity >= cap.max_drawdown * peak_equity:
            breaches.add("peak_drawdown")
        if day_open_equity - stressed_equity >= cap.max_daily_loss * day_open_equity:
            breaches.add("daily_loss")
        if equity - stressed_equity >= cap.max_loss_currency:
            breaches.add("monetary_loss")
    return RiskVerdict(
        not breaches,
        tuple(sorted(breaches)),
        evaluated,
        worst_equity,
        worst_margin,
        worst_gross,
    )
