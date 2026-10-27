//! Bounded, integer-only research capital management (Part 3).
//!
//! All wallet amounts, ratios, posterior statistics, and sizing calculations
//! use fixed-point i64/i128. No f64, network calls, or hot-path allocations.
//! This component does not submit orders, withdraw vault funds, or reconcile a
//! real broker. Broker-side losses can exceed modelled stop distances.
mod kelly;
mod sizing;
mod wallet;

pub use kelly::{BayesianKelly, KellyEstimate, Strategy};
pub use sizing::{
    ConvictionInputs, EntrySizingInputs, NoTradeReason, PositionSize, SizingDecision,
    compute_short_position_size,
};
pub use wallet::{WalletBuckets, WalletSnapshot};

use trading_core_types::SCALE;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WalletError {
    InvalidAmount,
    InvalidPercentage,
    InvalidObservation,
    InvalidBook,
    NonMonotonicSettlement,
    Insolvent,
    ArithmeticOverflow,
}
pub(crate) fn narrow(v: i128) -> Result<i64, WalletError> {
    i64::try_from(v).map_err(|_| WalletError::ArithmeticOverflow)
}
pub(crate) fn ratio(num: i128, den: i128) -> Result<i128, WalletError> {
    if den <= 0 {
        return Err(WalletError::InvalidObservation);
    }
    num.checked_mul(i128::from(SCALE))
        .ok_or(WalletError::ArithmeticOverflow)
        .map(|n| n / den)
}
pub(crate) fn mul_scaled(a: i128, b: i128) -> Result<i128, WalletError> {
    a.checked_mul(b)
        .ok_or(WalletError::ArithmeticOverflow)
        .map(|v| v / i128::from(SCALE))
}
