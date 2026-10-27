//! Wire-compatible live venue primitives. Exact scale 1e8; no float rounding.
use crate::{FixedPrice, FixedQty, FixedUsd, SCALE};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InstrumentError {
    ZeroIncrement, InvalidPrice, InvalidQuantity, UnderMinimum, OverMaximum,
    ArithmeticOverflow, NotAligned,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TickRounding { Down, Up }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InstrumentSpec {
    pub tick_size: i64,
    pub step_size: i64,
    /// Quote currency, not automatically USD if symbol is USDT-settled.
    pub min_notional: i64,
    pub min_price: i64,
    pub max_price: i64,
    pub min_qty: i64,
    pub max_qty: i64,
    /// MARKET_LOT_SIZE can differ from LIMIT LOT_SIZE.
    pub market_step_size: i64,
    pub market_min_qty: i64,
    pub market_max_qty: i64,
}
impl InstrumentSpec {
    pub fn validate(self) -> Result<(), InstrumentError> {
        if self.tick_size <= 0 || self.step_size <= 0
            || self.market_step_size <= 0 || self.min_notional < 0
            || self.min_price < 0 || self.min_qty < 0
            || self.market_min_qty < 0
            || (self.max_price != 0 && self.max_price < self.min_price)
            || (self.max_qty != 0 && self.max_qty < self.min_qty)
            || (self.market_max_qty != 0 && self.market_max_qty < self.market_min_qty)
        {
            return Err(InstrumentError::ZeroIncrement);
        }
        Ok(())
    }
    pub fn round_price_to_tick(self, raw: FixedPrice) -> Result<FixedPrice, InstrumentError> {
        self.round_price(raw, TickRounding::Down)
    }
    pub fn round_price(self, raw: FixedPrice, direction: TickRounding)
        -> Result<FixedPrice, InstrumentError>
    {
        self.validate()?;
        if raw.0 <= 0 { return Err(InstrumentError::InvalidPrice); }
        let step = i128::from(self.tick_size);
        let n = i128::from(raw.0);
        let adjusted = match direction {
            TickRounding::Down => n / step * step,
            TickRounding::Up => (n + step - 1) / step * step,
        };
        let result = i64::try_from(adjusted)
            .map_err(|_| InstrumentError::ArithmeticOverflow)?;
        self.check_price(FixedPrice(result))?;
        Ok(FixedPrice(result))
    }
    pub fn round_qty_to_step(self, raw: FixedQty) -> Result<FixedQty, InstrumentError> {
        self.round_qty(raw, false)
    }
    pub fn round_qty(self, raw: FixedQty, market: bool)
        -> Result<FixedQty, InstrumentError>
    {
        self.validate()?;
        if raw.0 <= 0 { return Err(InstrumentError::InvalidQuantity); }
        let step = if market { self.market_step_size } else { self.step_size };
        let rounded = raw.0 / step * step;
        self.check_qty(FixedQty(rounded), market)?;
        Ok(FixedQty(rounded))
    }
    pub fn check_price(self, p: FixedPrice) -> Result<(), InstrumentError> {
        self.validate()?;
        if p.0 <= 0 || p.0 % self.tick_size != 0 {
            return Err(InstrumentError::NotAligned);
        }
        if p.0 < self.min_price { return Err(InstrumentError::UnderMinimum); }
        if self.max_price != 0 && p.0 > self.max_price {
            return Err(InstrumentError::OverMaximum);
        }
        Ok(())
    }
    pub fn check_qty(self, q: FixedQty, market: bool) -> Result<(), InstrumentError> {
        self.validate()?;
        let (step, min, max) = if market {
            (self.market_step_size, self.market_min_qty, self.market_max_qty)
        } else { (self.step_size, self.min_qty, self.max_qty) };
        if q.0 <= 0 || q.0 % step != 0 { return Err(InstrumentError::NotAligned); }
        if q.0 < min { return Err(InstrumentError::UnderMinimum); }
        if max != 0 && q.0 > max { return Err(InstrumentError::OverMaximum); }
        Ok(())
    }
    pub fn check_notional(self, price: FixedPrice, qty: FixedQty)
        -> Result<FixedUsd, InstrumentError>
    {
        let raw = i128::from(price.0) * i128::from(qty.0) / i128::from(SCALE);
        if raw < i128::from(self.min_notional) { return Err(InstrumentError::UnderMinimum); }
        i64::try_from(raw).map(FixedUsd)
            .map_err(|_| InstrumentError::ArithmeticOverflow)
    }
}

#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExecutionStatus {
    New = 1, PartiallyFilled = 2, Filled = 3, Canceled = 4,
    Expired = 5, Rejected = 6, Unknown = 255,
}
impl TryFrom<u8> for ExecutionStatus {
    type Error = ();
    fn try_from(value: u8) -> Result<Self, Self::Error> {
        match value {
            1=>Ok(Self::New),2=>Ok(Self::PartiallyFilled),3=>Ok(Self::Filled),
            4=>Ok(Self::Canceled),5=>Ok(Self::Expired),6=>Ok(Self::Rejected),
            255=>Ok(Self::Unknown),_=>Err(()),
        }
    }
}
/// Real execution event layout for the Part 2 private stream. Side/status
/// are u8 wire discriminants. Fee and PnL retain original asset units:
/// conversion to account base currency is mandatory before wallet mutation.
/// A trade execution has last_filled_qty > 0; status updates can be zero.
#[repr(C, align(64))]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ExecutionReportEvent {
    pub exchange_ts_ns: u64,
    pub client_order_id: u64,
    pub fill_price: i64,
    pub last_filled_qty: i64,
    pub cumulative_qty: i64,
    pub realized_pnl: i64,
    pub commission: i64,
    pub symbol_id: u16,
    pub status: u8,
    pub side: u8,
    pub venue_id: u8,
    pub flags: u8,
    pub _padding: [u8; 2],
}
const _: [(); 64] = [(); core::mem::size_of::<ExecutionReportEvent>()];
const _: [(); 64] = [(); core::mem::align_of::<ExecutionReportEvent>()];

#[cfg(test)]
mod tests {
    use super::*;
    fn s() -> InstrumentSpec {
        InstrumentSpec {
            tick_size: 2_500_000, step_size: 1_000_000, min_notional: 500_000_000,
            min_price: 2_500_000, max_price: 10_000_000_000,
            min_qty: 1_000_000, max_qty: 100_000_000_000,
            market_step_size: 10_000_000, market_min_qty: 10_000_000,
            market_max_qty: 100_000_000_000,
        }
    }
    #[test]
    fn directional_tick_rounding_and_minimums() {
        assert_eq!(s().round_price(FixedPrice(10_000_001), TickRounding::Down).unwrap(),
            FixedPrice(10_000_000));
        assert_eq!(s().round_price(FixedPrice(10_000_001), TickRounding::Up).unwrap(),
            FixedPrice(12_500_000));
        assert!(s().round_qty_to_step(FixedQty(900_000)).is_err());
        assert_eq!(s().round_qty_to_step(FixedQty(11_500_000)).unwrap(),
            FixedQty(11_000_000));
        assert_eq!(s().round_qty(FixedQty(19_500_000), true).unwrap(),
            FixedQty(10_000_000));
    }
    #[test]
    fn notional_is_quote_denomination_and_i128_checked() {
        assert!(s().check_notional(FixedPrice(10_000_000), FixedQty(1_000_000)).is_err());
        assert_eq!(s().check_notional(FixedPrice(5_000_000_000), FixedQty(100_000_000))
            .unwrap(), FixedUsd(5_000_000_000));
    }
    #[test]
    fn event_has_one_cache_line_and_known_enum() {
        assert_eq!(core::mem::size_of::<ExecutionReportEvent>(), 64);
        assert!(ExecutionStatus::try_from(78).is_err());
        assert_eq!(ExecutionStatus::try_from(2), Ok(ExecutionStatus::PartiallyFilled));
    }
}
