//! Deterministic, fixed-capacity microstructure research signals.
//!
//! Prices, quantities, and notionals stay fixed-point in the book, wallet and
//! order interfaces. Isolated signal registers use f64 for exponential decay,
//! logarithms and Kalman inference; they never produce executable orders.
//! Callers must supply ordered monotonic timestamps and synchronized books.
//! All update methods do zero heap allocation after construction.
//!
//! No individual statistic establishes informed trading, hidden liquidity,
//! market manipulation, or profitable predictive power.

mod absorption;
mod hawkes;
mod lead_lag;
mod microprice;
mod ofi;
mod vpin;

pub use absorption::{AbsorptionEngine, AbsorptionSnapshot};
pub use hawkes::{HawkesCascadeEngine, HawkesEvent, HawkesSnapshot};
pub use lead_lag::{KalmanLeadLagEngine, LeadLagSnapshot, PairSymbol};
pub use microprice::{MicroPriceEngine, MicroPriceSnapshot};
pub use ofi::{MlOfiEngine, MlOfiSnapshot};
pub use vpin::{VpinSnapshot, VpinToxicityEngine};

use trading_core_types::{FixedPrice, NumericError, SCALE};
use trading_orderbook::BookError;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SignalError {
    NonMonotonicTime,
    InvalidPrice,
    InvalidQuantity,
    InvalidConfiguration,
    NumericOverflow,
    InvalidBook,
    TruncatedBook,
    InvalidObservation,
}
impl From<NumericError> for SignalError {
    fn from(_: NumericError) -> Self {
        Self::NumericOverflow
    }
}
impl From<BookError> for SignalError {
    fn from(e: BookError) -> Self {
        match e {
            BookError::TruncatedDepth => Self::TruncatedBook,
            BookError::NumericOverflow => Self::NumericOverflow,
            _ => Self::InvalidBook,
        }
    }
}
pub(crate) fn elapsed_s(previous: Option<u64>, current: u64) -> Result<f64, SignalError> {
    if previous.is_some_and(|old| current < old) {
        return Err(SignalError::NonMonotonicTime);
    }
    Ok(previous.map_or(0.0, |old| current.saturating_sub(old) as f64 * 1e-9))
}
pub(crate) fn qty_float(value: i64) -> Result<f64, SignalError> {
    if value < 0 {
        return Err(SignalError::InvalidQuantity);
    }
    Ok(value as f64 / SCALE as f64)
}
pub(crate) fn positive_price(price: FixedPrice) -> Result<f64, SignalError> {
    if price.0 <= 0 {
        return Err(SignalError::InvalidPrice);
    }
    Ok(price.0 as f64 / SCALE as f64)
}
pub(crate) fn fixed_price(value: f64) -> Result<FixedPrice, SignalError> {
    if !value.is_finite() || value <= 0.0 || value * SCALE as f64 > i64::MAX as f64 {
        return Err(SignalError::NumericOverflow);
    }
    let raw = (value * SCALE as f64).round();
    if raw < 1.0 || raw >= 9_223_372_036_854_775_808.0 {
        return Err(SignalError::NumericOverflow);
    }
    Ok(FixedPrice(raw as i64))
}
pub(crate) fn finite_positive(v: f64) -> Result<f64, SignalError> {
    if v.is_finite() && v > 0.0 {
        Ok(v)
    } else {
        Err(SignalError::InvalidConfiguration)
    }
}
pub(crate) fn decay(dt_s: f64, time_constant_s: f64) -> f64 {
    (-dt_s / time_constant_s).exp()
}
pub(crate) fn init_weights<const N: usize>() -> [f64; N] {
    std::array::from_fn(|k| (-0.35 * k as f64).exp())
}
