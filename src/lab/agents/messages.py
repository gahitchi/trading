"""
Messages the trading agents exchange over NautilusTrader's message bus.

    SignalAgent ──lab.signal──▶ AllocatorAgent ──lab.targets──▶ RiskAgent ──lab.approved──▶ ExecutionAgent
                                                                    ▲                            │
                                                                    └────────lab.equity──────────┤
    RecorderAgent listens to every topic                                   lab.fill / lab.risk ◀──┘
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

T_MARK = "lab.mark"
T_SIGNAL = "lab.signal"
T_TARGETS = "lab.targets"
T_APPROVED = "lab.approved"
T_EQUITY = "lab.equity"
T_FILL = "lab.fill"
T_RISK = "lab.risk"
ALL_TOPICS = (T_SIGNAL, T_TARGETS, T_APPROVED, T_EQUITY, T_FILL, T_RISK)


@dataclass(frozen=True)
class PriceMark:
    ticker: str
    price: float
    ts: int


@dataclass(frozen=True)
class SignalUpdate:
    sleeve: str  # which signal-on-ticker produced this
    ticker: str
    target: float  # desired exposure 0..1 for this sleeve
    price: float  # close of the bar the decision was made on
    ts: int  # bar timestamp (UNIX ns)
    active: bool = True  # False once the stock leaves the investable universe (delisting, index exit)


@dataclass(frozen=True)
class TargetWeights:
    weights: dict  # ticker -> fraction of portfolio equity
    ts: int


@dataclass(frozen=True)
class ApprovedTargets:
    weights: dict
    ts: int
    halted: bool = False
    notes: tuple = field(default_factory=tuple)
    reduce_only: bool = False  # margin call: positions may only shrink toward zero


@dataclass(frozen=True)
class EquitySnapshot:
    ts: int
    equity: float
    cash: float
    positions: dict  # ticker -> shares (negative = short)
    gross: float  # (long value + short value) / equity
    net: float = 0.0  # (long value - short value) / equity
    long_value: float = 0.0
    short_value: float = 0.0  # market value of shorted shares, >= 0
    financing: float = 0.0  # borrow fees and debit interest accrued so far (already deducted from equity)


@dataclass(frozen=True)
class FillReport:
    ts: int
    ticker: str
    side: str
    qty: float
    price: float
    commission: float
    order_id: str = ""
    decision_price: float | None = None
    decision_ts: int | None = None


@dataclass(frozen=True)
class RiskEvent:
    ts: int
    kind: str
    detail: str


def to_dict(msg) -> dict:
    d = asdict(msg)
    d["type"] = type(msg).__name__
    return d
