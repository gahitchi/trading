"""Trading cost models: commissions, slippage and order latency."""

from __future__ import annotations

from nautilus_trader.execution import DefaultFillModel, FeeModel, StaticLatencyModel
from nautilus_trader.model import Money

from lab.config import CostSettings
from lab.universe import USD


def ibkr_fixed_commission(shares: float, price: float, *, per_share: float = 0.005,
                          minimum: float = 1.0, max_pct: float = 0.01) -> float:
    """IBKR Pro Fixed: $0.005/share, at least $1.00, at most 1% of trade value."""
    return min(max(shares * per_share, minimum), max_pct * shares * price)


class IbkrFixedFeeModel(FeeModel):
    """Charges ibkr_fixed_commission() per fill.

    Note: IBKR applies the $1 minimum per order. A large order filled in several
    pieces is charged the minimum per piece here, which slightly overstates costs.
    """

    def __init__(self, per_share: float, minimum: float, max_pct: float):
        self.per_share = per_share
        self.minimum = minimum
        self.max_pct = max_pct

    def get_commission(self, order, fill_quantity, fill_px, instrument):
        fee = ibkr_fixed_commission(float(fill_quantity), float(fill_px), per_share=self.per_share,
                                    minimum=self.minimum, max_pct=self.max_pct)
        return Money(round(fee, 2), USD)


def fee_model(c: CostSettings) -> IbkrFixedFeeModel:
    return IbkrFixedFeeModel(c.commission_per_share, c.commission_min, c.commission_max_pct)


def fill_model(c: CostSettings) -> DefaultFillModel:
    # prob_slippage=1: every market fill is one tick worse than the print.
    return DefaultFillModel(prob_fill_on_limit=1.0, prob_slippage=1.0 if c.slippage else 0.0, random_seed=42)


def latency_model() -> StaticLatencyModel:
    # One second between decision and arrival at the venue: an order decided at the
    # 16:00 close meets the next market event, which is the next session's open.
    return StaticLatencyModel(base_latency_nanos=1_000_000_000)
