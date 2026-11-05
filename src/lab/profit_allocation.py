"""Pure, fixed-currency UTC daily-equity and partial-profit allocation kernel.

No orders, custody transfers or strategy signals. Values are decimal strings; the
marked-equity identity is verified after every event. Source IDs prevent a
settlement replay under a different event ID. Missing day closes fail closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_CURRENCY = re.compile(r"[A-Z][A-Z0-9]{2,7}\Z")
_KINDS = {"trade", "mark", "day_close", "adjustment", "external_flow", "reserve_transfer"}
_REASONS = {"fee", "funding", "financing", "fx", "correction"}
_ZERO = Decimal(0)


def _decimal(value, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, Decimal)):
        raise ValueError(f"{name} must be an exact decimal string or Decimal")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal {name}") from exc
    if not result.is_finite():
        raise ValueError(f"nonfinite {name}")
    return result


@dataclass(frozen=True)
class AllocationPolicy:
    currency: str
    initial_capital: str
    quantum: str
    reinvest_fraction: str
    max_deployable: str | None = None  # risk budget: unset is NOT unlimited order permission

    def __post_init__(self) -> None:
        if not _CURRENCY.fullmatch(self.currency):
            raise ValueError("invalid settlement currency")
        q = _decimal(self.quantum, "quantum")
        if q <= 0 or q.normalize().as_tuple().digits != (1,):
            raise ValueError("quantum must be a positive power of ten")
        init = _decimal(self.initial_capital, "initial capital")
        if init <= 0 or init != init.quantize(q):
            raise ValueError("initial capital must be positive and quantized")
        f = _decimal(self.reinvest_fraction, "reinvestment fraction")
        if not _ZERO <= f <= 1:
            raise ValueError("reinvestment fraction must be between 0 and 1")
        if self.max_deployable is not None:
            cap = _decimal(self.max_deployable, "deployable cap")
            if cap <= 0 or cap != cap.quantize(q):
                raise ValueError("deployable cap must be positive and quantized")


@dataclass(frozen=True)
class AllocationEvent:
    event_id: str
    source_id: str
    timestamp: datetime
    currency: str
    kind: str
    amount: str
    gross: str | None = None
    fees: str | None = None
    slippage: str | None = None
    funding: str | None = None
    released_mark: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class DayClose:
    day: date
    start_equity: Decimal
    end_equity: Decimal
    external_flows: Decimal
    net_pnl: Decimal
    realized_net_pnl: Decimal
    unrealized_change: Decimal
    reinvested_profit: Decimal
    retained_profit: Decimal
    deployable: Decimal
    reserve: Decimal
    open_unrealized: Decimal


class ProfitAllocationLedger:
    """Audited strategy research accounting, not an exchange wallet or order risk engine.

    mark and day_close supply the absolute open-position unrealized P&L.
    A trade closing a marked position must declare the exact unrealized P&L
    it releases. The closed trade's amount is net of its own fees,
    slippage and funding; separately booked adjustments must not duplicate it.
    No assumption is made that an in-venue reserve is legally segregated.
    """

    def __init__(self, policy: AllocationPolicy, start_day: date):
        if isinstance(start_day, datetime) or not isinstance(start_day, date):
            raise ValueError("start_day must be an explicit UTC calendar date")
        self.policy = policy
        self.start_day = start_day
        self._q = _decimal(policy.quantum, "quantum")
        self._f = _decimal(policy.reinvest_fraction, "reinvestment fraction")
        self._initial = _decimal(policy.initial_capital, "initial capital")
        self._deployable = self._initial
        self._reserve = _ZERO
        self._unrealized = _ZERO
        self._realized = _ZERO
        self._external = _ZERO
        self._reinvested = _ZERO
        self._retained = _ZERO
        self._seen_events: set[str] = set()
        self._seen_sources: set[str] = set()
        self._last_ts: datetime | None = None
        self._last_close_ts: datetime | None = None
        self._next_day = start_day
        self._last_close_equity = self._initial
        self._last_close_realized = _ZERO
        self._last_close_external = _ZERO
        self._last_close_unrealized = _ZERO
        self._last_close_reinvested = _ZERO
        self._last_close_retained = _ZERO
        self._days: list[DayClose] = []

    def _money(self, value: str | Decimal, name: str) -> Decimal:
        d = _decimal(value, name)
        try:
            if d != d.quantize(self._q):
                raise ValueError(f"{name} is not quantized to {self._q}")
        except InvalidOperation as exc:
            raise ValueError(f"invalid precision for {name}") from exc
        return d

    @property
    def equity(self) -> Decimal:
        return self._deployable + self._reserve + self._unrealized

    @property
    def deployable(self) -> Decimal:
        return self._deployable

    @property
    def reserve(self) -> Decimal:
        return self._reserve

    @property
    def daily(self) -> tuple[DayClose, ...]:
        return tuple(self._days)

    @property
    def risk_capacity(self) -> Decimal | None:
        """Unconfigured absolute cap means no executable risk authorization."""
        if self.policy.max_deployable is None:
            return None
        return max(_ZERO, min(self._deployable, self.equity, _decimal(self.policy.max_deployable, "deployable cap")))

    def apply(self, event: AllocationEvent) -> DayClose | None:
        if not isinstance(event, AllocationEvent):
            raise ValueError("allocation event has invalid type")
        if not _ID.fullmatch(event.event_id) or not _ID.fullmatch(event.source_id):
            raise ValueError("invalid event ID or stable economic source ID")
        if event.event_id in self._seen_events or event.source_id in self._seen_sources:
            raise ValueError("duplicate event or economic source ID")
        if (
            not isinstance(event.timestamp, datetime)
            or event.timestamp.tzinfo is None
            or event.timestamp.utcoffset() != timedelta(0)
        ):
            raise ValueError("event timestamp must be UTC aware")
        if self._last_ts is not None and event.timestamp < self._last_ts:
            raise ValueError("out-of-order allocation event")
        if event.timestamp < datetime.combine(self.start_day, time.min, UTC):
            raise ValueError("event precedes accounting start date")
        if event.currency != self.policy.currency:
            raise ValueError("currency mismatch; apply audited FX conversion before replay")
        if event.kind not in _KINDS:
            raise ValueError("unknown allocation event kind")
        closing_time = datetime.combine(self._next_day + timedelta(days=1), time.min, UTC)
        if event.timestamp > closing_time or (event.timestamp == closing_time and event.kind != "day_close"):
            raise ValueError("missing UTC day-close mark; cannot infer an unobserved day")
        if event.kind == "day_close" and event.timestamp != closing_time:
            raise ValueError("UTC day close must be at next-day 00:00:00Z")
        amount = self._money(event.amount, "amount")
        details = (event.gross, event.fees, event.slippage, event.funding, event.released_mark)
        if event.kind == "trade":
            if any(x is None for x in details) or event.reason is not None:
                raise ValueError("trade must specify gross, fees, slippage, funding and released mark")
            gross, fees, slip, funding, released = (
                self._money(x, name)
                for x, name in zip(details, ("gross", "fees", "slippage", "funding", "released mark"), strict=True)
            )
            if fees < 0 or slip < 0 or amount != gross - fees - slip + funding:
                raise ValueError("trade net does not reconcile to signed gross and nonnegative execution costs")
            if released and (
                not self._unrealized or released * self._unrealized < 0 or abs(released) > abs(self._unrealized)
            ):
                raise ValueError("released mark is not covered by the current unrealized P&L")
        elif any(x is not None for x in details):
            raise ValueError("only trade events may carry trade economics or released marks")
        if event.kind == "adjustment":
            if event.reason not in _REASONS:
                raise ValueError("adjustment requires an audited fee/funding/financing/FX/correction reason")
        elif event.reason is not None:
            raise ValueError("reason is only valid for adjustment events")
        if event.kind == "reserve_transfer" and (amount > self._reserve or -amount > self._deployable):
            raise ValueError("retained/deployable transfer exceeds available source bucket")
        if event.kind == "external_flow" and amount < 0 and -amount > self._deployable:
            raise ValueError("withdrawal cannot silently spend retained reserve")

        deployable, reserve = self._deployable, self._reserve
        unrealized, realized, external = self._unrealized, self._realized, self._external
        reinvested, retained = self._reinvested, self._retained
        if event.kind == "trade":
            unrealized -= released
            realized += amount
            if amount > 0:
                # Round DOWN the deployable increment; never mint extra
                # money by applying the fraction independently to buckets.
                credit = (amount * self._f).quantize(self._q, rounding=ROUND_DOWN)
                deployable += credit
                reserve += amount - credit
                reinvested += credit
                retained += amount - credit
            else:
                deployable += amount
        elif event.kind == "adjustment":
            deployable += amount
            realized += amount
        elif event.kind == "external_flow":
            deployable += amount
            external += amount
        elif event.kind == "reserve_transfer":
            reserve -= amount
            deployable += amount
        else:  # mark and day_close both set an *absolute* valuation snapshot
            unrealized = amount

        equity = deployable + reserve + unrealized
        if equity != self._initial + realized + external + unrealized:
            raise ValueError("economic ledger equity conservation failed")
        record = None
        if event.kind == "day_close":
            daily_external = external - self._last_close_external
            daily_realized = realized - self._last_close_realized
            mark_change = unrealized - self._last_close_unrealized
            pnl = equity - self._last_close_equity - daily_external
            if pnl != daily_realized + mark_change:
                raise ValueError("UTC daily P&L does not reconcile to actual economic flows")
            record = DayClose(
                self._next_day,
                self._last_close_equity,
                equity,
                daily_external,
                pnl,
                daily_realized,
                mark_change,
                reinvested - self._last_close_reinvested,
                retained - self._last_close_retained,
                deployable,
                reserve,
                unrealized,
            )

        # The event is fully validated and reconciled before state is published.
        self._deployable, self._reserve, self._unrealized = deployable, reserve, unrealized
        self._realized, self._external = realized, external
        self._reinvested, self._retained = reinvested, retained
        self._seen_events.add(event.event_id)
        self._seen_sources.add(event.source_id)
        self._last_ts = event.timestamp
        if record is not None:
            self._days.append(record)
            self._last_close_ts = event.timestamp
            self._next_day += timedelta(days=1)
            self._last_close_equity = equity
            self._last_close_realized = realized
            self._last_close_external = external
            self._last_close_unrealized = unrealized
            self._last_close_reinvested = reinvested
            self._last_close_retained = retained
        return record

    def report(self, through: date) -> dict:
        """Require all promised UTC days to have explicit valued end-of-day marks."""
        if isinstance(through, datetime) or not isinstance(through, date) or through < self.start_day:
            raise ValueError("invalid reporting end day")
        if self._next_day != through + timedelta(days=1) or self._last_ts != self._last_close_ts:
            raise ValueError("incomplete daily valuation coverage")
        wins = sum(row.net_pnl > 0 for row in self._days)
        losses = sum(row.net_pnl < 0 for row in self._days)
        profits = sum((row.net_pnl for row in self._days if row.net_pnl > 0), _ZERO)
        loss_abs = -sum((row.net_pnl for row in self._days if row.net_pnl < 0), _ZERO)
        peak = self._initial
        max_drawdown = _ZERO
        for row in self._days:
            peak = max(peak, row.end_equity)
            max_drawdown = max(max_drawdown, peak - row.end_equity)
        return {
            "currency": self.policy.currency,
            "days": len(self._days),
            "winning_days": wins,
            "losing_days": losses,
            "flat_days": len(self._days) - wins - losses,
            "gross_winning_days": str(profits),
            "gross_losing_days_abs": str(loss_abs),
            "net_pnl": str(profits - loss_abs),
            "profit_factor": str(profits / loss_abs) if loss_abs > 0 else None,
            "end_equity": str(self.equity),
            "deployable": str(self._deployable),
            "reserve": str(self._reserve),
            "max_drawdown_currency": str(max_drawdown),
            "risk_capacity": str(self.risk_capacity) if self.risk_capacity is not None else None,
        }
