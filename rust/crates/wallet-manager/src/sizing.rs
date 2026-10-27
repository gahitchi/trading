use crate::{BayesianKelly, Strategy, WalletBuckets, WalletError, narrow};
use trading_core_types::{FixedUsd, OperatorSnapshot, SCALE, TradingMode};
use trading_orderbook::FlatOrderBook;

const SCALE128: i128 = SCALE as i128;
const BPS128: i128 = 10_000 * SCALE128;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ConvictionInputs {
    /// Absolute OFI Z-score * 1e8.
    pub ofi_abs_z_raw: i64,
    /// Downward intensity surge ratio * 1e8.
    pub isr_down_raw: i64,
    /// Positive sell-direction toxicity on [0,1] * 1e8.
    pub vpin_short_raw: i64,
}
impl ConvictionInputs {
    /// Formula weights 0.35 / 2.8, 0.35 / 4.5, 0.30 / 0.45.
    /// Clamped to [0.6, 1.85], all with integer arithmetic.
    pub fn multiplier_raw(self) -> Result<i64, WalletError> {
        if self.ofi_abs_z_raw < 0
            || self.isr_down_raw < 0
            || !(0..=SCALE).contains(&self.vpin_short_raw)
        {
            return Err(WalletError::InvalidObservation);
        }
        let z = i128::from(self.ofi_abs_z_raw);
        let h = i128::from(self.isr_down_raw);
        let v = i128::from(self.vpin_short_raw);
        let weighted =
            35 * z * 10 / (100 * 28) + 35 * h * 10 / (100 * 45) + 30 * v * 100 / (100 * 45);
        narrow(weighted.clamp(60_000_000, 185_000_000))
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EntrySizingInputs {
    /// Parkinson 1-minute volatility in basis points, scaled 1e8.
    pub sigma_1m_bps_raw: i64,
    /// Sum of open *and pending* gross order notionals, including all venues.
    pub gross_open_and_pending_notional: FixedUsd,
    pub minimum_order_notional: FixedUsd,
    /// Only synchronized, timestamp-valid, past-data-derived signals allowed.
    pub conviction: ConvictionInputs,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NoTradeReason {
    OperatorHalt,
    StrategyDisabled,
    WrongMode,
    SessionFloor,
    InsufficientSample,
    NoEdge,
    InsufficientBook,
    NoLeverageCapacity,
    BelowMinimumNotional,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PositionSize {
    pub final_notional: FixedUsd,
    pub kelly_notional_cap: FixedUsd,
    pub liquidity_notional_cap: FixedUsd,
    pub leverage_notional_cap: FixedUsd,
    pub modeled_stop_risk: FixedUsd,
    pub stop_distance_bps_raw: i64,
    pub conviction_multiplier_raw: i64,
    pub cppi_damper_ppm: u32,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SizingDecision {
    Permit(PositionSize),
    Reject(NoTradeReason),
}
fn multiply_divide(a: i128, b: i128, d: i128) -> Result<i128, WalletError> {
    if d <= 0 {
        return Err(WalletError::InvalidObservation);
    }
    Ok(a.checked_mul(b).ok_or(WalletError::ArithmeticOverflow)? / d)
}

/// **Short-only** entry sizing. Long market orders deliberately require a
/// symmetric separately qualified ask-side impact model. The static L2 book
/// slope used here is **not** an estimated Kyle lambda. The result is only
/// an entry *proposal*: it cannot authorize an order without the risk gate,
/// fresh venue book synchronizer, broker-side portfolio reconciliation, and
/// signed open/pending exposure reservations.
pub fn compute_short_position_size(
    wallet: &WalletBuckets,
    kelly: &BayesianKelly,
    strategy: Strategy,
    operator: OperatorSnapshot,
    book: &FlatOrderBook,
    inputs: EntrySizingInputs,
) -> Result<SizingDecision, WalletError> {
    operator
        .validate()
        .map_err(|_| WalletError::InvalidObservation)?;
    if inputs.sigma_1m_bps_raw < 0
        || inputs.gross_open_and_pending_notional.0 < 0
        || inputs.minimum_order_notional.0 < 0
    {
        return Err(WalletError::InvalidAmount);
    }
    let conviction = inputs.conviction.multiplier_raw()?;
    if operator.mode == TradingMode::HaltAll {
        return Ok(SizingDecision::Reject(NoTradeReason::OperatorHalt));
    }
    if !matches!(
        operator.mode,
        TradingMode::ShortOnly | TradingMode::ShortBiased | TradingMode::BiDirectional
    ) {
        return Ok(SizingDecision::Reject(NoTradeReason::WrongMode));
    }
    let mask = match strategy {
        Strategy::A => 1,
        Strategy::B => 2,
        Strategy::C => 4,
        Strategy::D => 8,
    };
    if operator.strategy_enable_mask & mask == 0 {
        return Ok(SizingDecision::Reject(NoTradeReason::StrategyDisabled));
    }
    if wallet.halt_required() {
        return Ok(SizingDecision::Reject(NoTradeReason::SessionFloor));
    }
    let damper = wallet.cppi_damper_ppm()?;
    if damper == 0 {
        return Ok(SizingDecision::Reject(NoTradeReason::SessionFloor));
    }
    let estimate = kelly.estimate(strategy)?;
    if !estimate.sample_ready {
        return Ok(SizingDecision::Reject(NoTradeReason::InsufficientSample));
    }
    if estimate.risk_fraction_raw == 0 {
        return Ok(SizingDecision::Reject(NoTradeReason::NoEdge));
    }
    if book.bids().len() < 10 || book.asks().is_empty() || !book.has_uncrossed_top() {
        return Ok(SizingDecision::Reject(NoTradeReason::InsufficientBook));
    }
    let lambda = book
        .bid_book_slope_10()
        .map_err(|_| WalletError::InvalidBook)?;
    let Some(lambda) = lambda else {
        return Ok(SizingDecision::Reject(NoTradeReason::InsufficientBook));
    };
    if lambda.0 < 0 {
        return Err(WalletError::InvalidBook);
    }
    let mut top5_raw: i128 = 0;
    for level in book.bids().iter().take(5) {
        if level.price.0 <= 0 || level.qty.0 <= 0 {
            return Err(WalletError::InvalidBook);
        }
        let notional =
            multiply_divide(i128::from(level.price.0), i128::from(level.qty.0), SCALE128)?;
        top5_raw = top5_raw
            .checked_add(notional)
            .ok_or(WalletError::ArithmeticOverflow)?;
    }
    // Quote notional is in the same currency as the wallet only for USD-quoted
    // instruments. FX and non-USD quotes require a separate conversion gate.
    let top5_consumption = top5_raw / 4; // <=25% displayed depth
    let impact_limit = multiply_divide(250_000_000, SCALE128, i128::from(lambda.0).max(1))?;
    let liq_cap = top5_consumption.min(impact_limit);
    let equity = i128::from(wallet.snapshot()?.active_equity.0);
    let stop_raw = (i128::from(inputs.sigma_1m_bps_raw) * 12 / 10).max(10 * SCALE128);
    let frac = i128::from(estimate.risk_fraction_raw);
    let risk0 = multiply_divide(equity, frac, SCALE128)?;
    let risk1 = multiply_divide(risk0, i128::from(damper), 1_000_000)?;
    let risk2 = multiply_divide(risk1, i128::from(conviction), SCALE128)?;
    let kelly_notional = multiply_divide(risk2, BPS128, stop_raw)?;
    let leverage_total = equity * i128::from(operator.max_leverage_tenths) / 10;
    let leverage_cap =
        (leverage_total - i128::from(inputs.gross_open_and_pending_notional.0)).max(0);
    if leverage_cap == 0 {
        return Ok(SizingDecision::Reject(NoTradeReason::NoLeverageCapacity));
    }
    let final_cap = kelly_notional.min(liq_cap).min(leverage_cap);
    if final_cap <= 0 || final_cap < i128::from(inputs.minimum_order_notional.0) {
        return Ok(SizingDecision::Reject(NoTradeReason::BelowMinimumNotional));
    }
    let modeled_risk = multiply_divide(final_cap, stop_raw, BPS128)?;
    Ok(SizingDecision::Permit(PositionSize {
        final_notional: FixedUsd(narrow(final_cap)?),
        kelly_notional_cap: FixedUsd(narrow(kelly_notional)?),
        liquidity_notional_cap: FixedUsd(narrow(liq_cap)?),
        leverage_notional_cap: FixedUsd(narrow(leverage_cap)?),
        modeled_stop_risk: FixedUsd(narrow(modeled_risk)?),
        stop_distance_bps_raw: narrow(stop_raw)?,
        conviction_multiplier_raw: conviction,
        cppi_damper_ppm: damper,
    }))
}
