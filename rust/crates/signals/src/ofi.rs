use crate::{decay, elapsed_s, init_weights, qty_float, SignalError};
use trading_orderbook::{FlatOrderBook, PriceLevel};

const LEVELS: usize = 10;
const TAU_OFI: f64 = 0.250;
const TAU_NORMALIZE: f64 = 30.0;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MlOfiSnapshot {
    pub ofi_base_units: f64,
    pub mean_base_units: f64,
    pub variance_base_units_squared: f64,
    pub z_score: f64,
}

/// Fixed 10-level, exponentially weighted Cont-style order flow register.
/// A change in rank does not identify the underlying individual order flow:
/// this is a **level-indexed proxy**, not order-add/cancel reconstruction.
pub struct MlOfiEngine {
    bid_old: [PriceLevel; LEVELS],
    ask_old: [PriceLevel; LEVELS],
    weights: [f64; LEVELS],
    previous_ns: Option<u64>,
    ofi: f64,
    mean: f64,
    variance: f64,
}
impl Default for MlOfiEngine {
    fn default() -> Self {
        Self {
            bid_old: [PriceLevel::default(); LEVELS],
            ask_old: [PriceLevel::default(); LEVELS],
            weights: init_weights(),
            previous_ns: None,
            ofi: 0.0,
            mean: 0.0,
            variance: 0.0,
        }
    }
}
impl MlOfiEngine {
    pub fn new() -> Self { Self::default() }
    pub fn reset(&mut self) { *self = Self::default(); }

    /// None on the first synchronized observation or insufficient level depth.
    /// The caller must reset the register if a book ever loses synchronization.
    pub fn update(&mut self, ts_ns: u64, book: &FlatOrderBook)
        -> Result<Option<MlOfiSnapshot>, SignalError>
    {
        if book.bids().len() < LEVELS || book.asks().len() < LEVELS ||
            !book.has_uncrossed_top() { return Ok(None); }
        let dt = elapsed_s(self.previous_ns, ts_ns)?;
        let current_bids: [PriceLevel; LEVELS] = book.bids()[..LEVELS].try_into()
            .map_err(|_| SignalError::InvalidBook)?;
        let current_asks: [PriceLevel; LEVELS] = book.asks()[..LEVELS].try_into()
            .map_err(|_| SignalError::InvalidBook)?;
        for x in current_bids.iter().chain(current_asks.iter()) {
            if x.price.0 <= 0 || x.qty.0 <= 0 { return Err(SignalError::InvalidBook); }
        }
        if self.previous_ns.is_none() {
            self.bid_old = current_bids;
            self.ask_old = current_asks;
            self.previous_ns = Some(ts_ns);
            return Ok(None);
        }

        let mut event_ofi = 0.0;
        for i in 0..LEVELS {
            let b = current_bids[i];
            let a = current_asks[i];
            let pb = self.bid_old[i];
            let pa = self.ask_old[i];
            let dq_bid = if b.price > pb.price { qty_float(b.qty.0)? }
                else if b.price == pb.price {
                    qty_float(b.qty.0)? - qty_float(pb.qty.0)?
                } else { -qty_float(pb.qty.0)? };
            let dq_ask = if a.price > pa.price { -qty_float(pa.qty.0)? }
                else if a.price == pa.price {
                    qty_float(a.qty.0)? - qty_float(pa.qty.0)?
                } else { qty_float(a.qty.0)? };
            event_ofi += self.weights[i] * (dq_bid - dq_ask);
        }
        let ofi = self.ofi * decay(dt, TAU_OFI) + event_ofi;
        let (mean, variance) = if self.variance == 0.0 && self.mean == 0.0 &&
            self.ofi == 0.0 {
            // The first actual event initializes EWMA without a false spike.
            (ofi, 0.0)
        } else {
            let gamma = decay(dt, TAU_NORMALIZE);
            let residual = ofi - self.mean;
            (self.mean + (1.0 - gamma) * residual,
                gamma * (self.variance + (1.0 - gamma) * residual * residual))
        };
        let z_score = (ofi - mean) / (variance + 1e-9).sqrt();
        if !ofi.is_finite() || !z_score.is_finite() {
            return Err(SignalError::NumericOverflow);
        }
        self.bid_old = current_bids;
        self.ask_old = current_asks;
        self.previous_ns = Some(ts_ns);
        self.ofi = ofi;
        self.mean = mean;
        self.variance = variance;
        Ok(Some(MlOfiSnapshot {
            ofi_base_units: ofi,
            mean_base_units: mean,
            variance_base_units_squared: variance,
            z_score,
        }))
    }
}
