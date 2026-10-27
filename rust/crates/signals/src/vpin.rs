use crate::SignalError;
use trading_core_types::FixedUsd;

pub const VPIN_BUCKETS: usize = 50;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct VpinSnapshot {
    /// Absolute imbalance averaged across exactly 50 completed buckets.
    pub toxicity: f64,
    /// Positive means sell-initiated flow dominates completed buckets.
    pub signed_sell_toxicity: f64,
    pub completed_buckets: u64,
}

/// Fixed-notional, aggressor-labelled volume clock. It is an observable
/// trade-sign toxicity proxy, *not* a trade-classification-estimated VPIN
/// or evidence of informed order flow.
///
/// Each incoming trade completes at most one partial bucket, at most 49
/// separate full buckets and at most one fixed-size ring replacement. A huge
/// single trade therefore never causes an unbounded loop.
pub struct VpinToxicityEngine {
    bucket_size_raw: i128,
    partial_signed: i128,
    partial_used: i128,
    signed: [i128; VPIN_BUCKETS],
    rolling_signed: i128,
    rolling_absolute: i128,
    next: usize,
    filled: usize,
    total: u64,
}
impl VpinToxicityEngine {
    pub fn new(bucket_size: FixedUsd) -> Result<Self, SignalError> {
        if bucket_size.0 <= 0 { return Err(SignalError::InvalidConfiguration); }
        Ok(Self {
            bucket_size_raw: i128::from(bucket_size.0),
            partial_signed: 0,
            partial_used: 0,
            signed: [0; VPIN_BUCKETS],
            rolling_signed: 0,
            rolling_absolute: 0,
            next: 0,
            filled: 0,
            total: 0,
        })
    }
    pub fn reset(&mut self) {
        self.partial_signed = 0;
        self.partial_used = 0;
        self.signed = [0; VPIN_BUCKETS];
        self.rolling_signed = 0;
        self.rolling_absolute = 0;
        self.next = 0;
        self.filled = 0;
        self.total = 0;
    }
    fn push_completed(&mut self, signed: i128) {
        let old = self.signed[self.next];
        self.rolling_signed += signed - old;
        self.rolling_absolute += signed.abs() - old.abs();
        self.signed[self.next] = signed;
        self.next = (self.next + 1) % VPIN_BUCKETS;
        self.filled = (self.filled + 1).min(VPIN_BUCKETS);
        self.total = self.total.saturating_add(1);
    }
    /// Constant-time bulk replacement for >=50 identical complete buckets.
    fn fill_full_ring(&mut self, signed_full: i128, number: i128) {
        self.signed.fill(signed_full);
        self.rolling_signed = signed_full * VPIN_BUCKETS as i128;
        self.rolling_absolute = signed_full.abs() * VPIN_BUCKETS as i128;
        self.next = (self.next + (number % VPIN_BUCKETS as i128) as usize) % VPIN_BUCKETS;
        self.filled = VPIN_BUCKETS;
        self.total = self.total.saturating_add(u64::try_from(number).unwrap_or(u64::MAX));
    }
    pub fn observe(&mut self, notional: FixedUsd, is_aggressive_sell: bool)
        -> Result<Option<VpinSnapshot>, SignalError>
    {
        if notional.0 <= 0 { return Err(SignalError::InvalidQuantity); }
        let direction = if is_aggressive_sell { 1_i128 } else { -1_i128 };
        let size = self.bucket_size_raw;
        let remaining_first = size - self.partial_used;
        let mut remaining = i128::from(notional.0);
        if remaining >= remaining_first {
            self.partial_signed += direction * remaining_first;
            self.push_completed(self.partial_signed);
            self.partial_used = 0;
            self.partial_signed = 0;
            remaining -= remaining_first;
        } else {
            self.partial_used += remaining;
            self.partial_signed += direction * remaining;
            return Ok(self.snapshot());
        }
        let full = remaining / size;
        if full >= VPIN_BUCKETS as i128 {
            self.fill_full_ring(direction * size, full);
        } else {
            for _ in 0..full {
                self.push_completed(direction * size);
            }
        }
        remaining %= size;
        self.partial_used = remaining;
        self.partial_signed = direction * remaining;
        Ok(self.snapshot())
    }
    pub fn snapshot(&self) -> Option<VpinSnapshot> {
        if self.filled < VPIN_BUCKETS { return None; }
        let norm = VPIN_BUCKETS as f64 * self.bucket_size_raw as f64;
        Some(VpinSnapshot {
            toxicity: self.rolling_absolute as f64 / norm,
            signed_sell_toxicity: self.rolling_signed as f64 / norm,
            completed_buckets: self.total,
        })
    }
    pub fn completed_buckets(&self) -> u64 { self.total }
}
