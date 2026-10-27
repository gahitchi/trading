use crate::{SignalError, decay, elapsed_s, positive_price, qty_float};
use trading_core_types::{FixedPrice, FixedQty};

const WINDOW_S: f64 = 0.400;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct AbsorptionSnapshot {
    pub recent_sell_volume: f64,
    pub downward_displacement_bps: f64,
    pub absorption_ratio: f64,
}
/// Decayed sell-initiated volume divided by downward bid displacement.
/// Detection is a *candidate absorption proxy*: no L2-only observation can
/// prove an iceberg, distinguish replenishment from spoofing, or identify an
/// institutional participant. The reference volume must be calibrated from
/// past-only observations by the caller.
pub struct AbsorptionEngine {
    previous_ns: Option<u64>,
    reference_bid: f64,
    decayed_sell_volume: f64,
}
impl Default for AbsorptionEngine {
    fn default() -> Self {
        Self {
            previous_ns: None,
            reference_bid: 0.0,
            decayed_sell_volume: 0.0,
        }
    }
}
impl AbsorptionEngine {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn reset(&mut self) {
        *self = Self::default();
    }

    /// Called on an accepted book/trade event. Feed qty only for *aggressive
    /// sell* prints since the preceding call; pass zero for all other events.
    pub fn update(
        &mut self,
        ts_ns: u64,
        best_bid: FixedPrice,
        aggressive_sell_qty: FixedQty,
        normal_sell_volume_400ms: FixedQty,
    ) -> Result<AbsorptionSnapshot, SignalError> {
        let bid = positive_price(best_bid)?;
        let sold = qty_float(aggressive_sell_qty.0)?;
        let normal = qty_float(normal_sell_volume_400ms.0)?;
        if normal <= 0.0 {
            return Err(SignalError::InvalidConfiguration);
        }
        let dt = elapsed_s(self.previous_ns, ts_ns)?;
        let gamma = decay(dt, WINDOW_S);
        let reference = if self.previous_ns.is_none() {
            bid
        } else {
            gamma * self.reference_bid + (1.0 - gamma) * bid
        };
        let volume = self.decayed_sell_volume * gamma + sold;
        let displacement = ((reference - bid).max(0.0) / bid) * 10_000.0;
        let ratio = (volume / normal) / (displacement + 0.5);
        if !ratio.is_finite() {
            return Err(SignalError::NumericOverflow);
        }
        self.reference_bid = reference;
        self.decayed_sell_volume = volume;
        self.previous_ns = Some(ts_ns);
        Ok(AbsorptionSnapshot {
            recent_sell_volume: volume,
            downward_displacement_bps: displacement,
            absorption_ratio: ratio,
        })
    }
}
