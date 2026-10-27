use crate::{SignalError, decay, elapsed_s, fixed_price, init_weights, qty_float};
use trading_core_types::{FixedPrice, FixedQty};
use trading_orderbook::FlatOrderBook;

const LEVELS: usize = 5;
const TAU_DEPTH_S: f64 = 1_800.0;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MicroPriceSnapshot {
    pub mid: FixedPrice,
    pub microprice: FixedPrice,
    pub imbalance: f64,
    pub divergence_bps: f64,
    pub bid_depth_20bps: FixedQty,
    pub ask_depth_20bps: FixedQty,
    pub downward_vacuum_ratio: f64,
}

/// Five-level cross-weighted microprice plus an independent 20bp bid-depth
/// EWMA. Book depth is rejected if the bounded book omits levels in-band.
pub struct MicroPriceEngine {
    weights: [f64; LEVELS],
    previous_ns: Option<u64>,
    bid_depth_mean: f64,
}
impl Default for MicroPriceEngine {
    fn default() -> Self {
        Self {
            weights: init_weights(),
            previous_ns: None,
            bid_depth_mean: 0.0,
        }
    }
}
impl MicroPriceEngine {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn reset(&mut self) {
        *self = Self::default();
    }

    pub fn update(
        &mut self,
        ts_ns: u64,
        book: &FlatOrderBook,
    ) -> Result<Option<MicroPriceSnapshot>, SignalError> {
        if book.bids().len() < LEVELS || book.asks().len() < LEVELS || !book.has_uncrossed_top() {
            return Ok(None);
        }
        let dt = elapsed_s(self.previous_ns, ts_ns)?;
        let depth = book.depth_20bps()?.ok_or(SignalError::InvalidBook)?;
        let best_bid = book.best_bid().ok_or(SignalError::InvalidBook)?.price;
        let best_ask = book.best_ask().ok_or(SignalError::InvalidBook)?.price;
        if best_bid.0 <= 0 || best_ask.0 <= best_bid.0 {
            return Err(SignalError::InvalidBook);
        }
        let mut qb = 0.0;
        let mut qa = 0.0;
        for i in 0..LEVELS {
            qb += self.weights[i] * qty_float(book.bids()[i].qty.0)?;
            qa += self.weights[i] * qty_float(book.asks()[i].qty.0)?;
        }
        if qb + qa <= 0.0 {
            return Err(SignalError::InvalidQuantity);
        }
        let imbalance = (qb - qa) / (qb + qa);
        let mid = (best_bid.0 as f64 + best_ask.0 as f64) / 2.0 / 1e8;
        let micro = mid + imbalance * (best_ask.0 - best_bid.0) as f64 / 2.0 / 1e8;
        let depth_now = qty_float(depth.bid_qty.0)?;
        if depth_now <= 0.0 {
            return Err(SignalError::InvalidBook);
        }
        let mean = if self.previous_ns.is_none() {
            depth_now
        } else {
            let gamma = decay(dt, TAU_DEPTH_S);
            gamma * self.bid_depth_mean + (1.0 - gamma) * depth_now
        };
        if !mean.is_finite() || mean <= 0.0 {
            return Err(SignalError::NumericOverflow);
        }
        let s = MicroPriceSnapshot {
            mid: fixed_price(mid)?,
            microprice: fixed_price(micro)?,
            imbalance,
            divergence_bps: (micro / mid - 1.0) * 10_000.0,
            bid_depth_20bps: depth.bid_qty,
            ask_depth_20bps: depth.ask_qty,
            downward_vacuum_ratio: depth_now / mean,
        };
        self.bid_depth_mean = mean;
        self.previous_ns = Some(ts_ns);
        Ok(Some(s))
    }
}
