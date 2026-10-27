use crate::types::RiskError;

const NANOS_PER_SECOND: u128 = 1_000_000_000;
#[derive(Debug, Clone, Copy)]
pub struct TokenBucket {
    capacity: u32,
    refill_rate: u32,
    token_ns: u128,
    previous_ns: Option<u64>,
}
impl TokenBucket {
    pub fn new(capacity: u32, refill_per_second: u32) -> Result<Self, RiskError> {
        if capacity == 0 || refill_per_second == 0 {
            return Err(RiskError::InvalidConfiguration);
        }
        Ok(Self {
            capacity,
            refill_rate: refill_per_second,
            token_ns: u128::from(capacity) * NANOS_PER_SECOND,
            previous_ns: None,
        })
    }
    pub fn available(&self) -> u32 {
        (self.token_ns / NANOS_PER_SECOND) as u32
    }
    /// Integer nanosecond refill retains fractional tokens. On error no
    /// tokens are spent. Caller validates all other policy before debiting.
    pub fn take(&mut self, now_ns: u64) -> Result<bool, RiskError> {
        if self.previous_ns.is_some_and(|old| now_ns < old) {
            return Err(RiskError::ClockReversal);
        }
        if let Some(old) = self.previous_ns {
            let accrued = u128::from(now_ns - old)
                .checked_mul(u128::from(self.refill_rate))
                .ok_or(RiskError::NumericOverflow)?;
            self.token_ns = self
                .token_ns
                .saturating_add(accrued)
                .min(u128::from(self.capacity) * NANOS_PER_SECOND);
        }
        self.previous_ns = Some(now_ns);
        if self.token_ns < NANOS_PER_SECOND {
            return Ok(false);
        }
        self.token_ns -= NANOS_PER_SECOND;
        Ok(true)
    }
}
