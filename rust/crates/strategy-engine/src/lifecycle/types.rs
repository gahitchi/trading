use trading_core_types::{FixedPrice, FixedQty};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LifecycleError {
    InvalidInput, TimeReversal, InvalidTransition,
    NonMonotonicReport, UnknownExit, NumericOverflow, PendingExitAtRisk,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LifecyclePhase {
    InitialRisk, Breakeven, PartialExitPending, Runner,
    ExitPending, Closed, ReconciliationRequired,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExitReason {
    HardStop, Fakeout, PartialHarvest, RunnerStop,
    Climax, ContinuationFailed, Stagnation,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExitReportKind {
    Working, Canceled, Rejected, Filled, Unknown,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ExitProposal {
    pub action_id: u64,
    pub close_quantity: FixedQty,
    pub reason: ExitReason,
    pub reduce_only: bool,
}
#[derive(Debug, Clone, Copy)]
pub struct LifecycleSignals {
    pub ofi_z: f64,
    pub book_imbalance: f64,
    pub hawkes_acceleration: f64,
    /// Provisional activity proxy (not actual Hawkes branching spectral radius).
    pub hawkes_activity_proxy: f64,
    pub bid_vacuum_ratio: f64,
    pub absorption_ratio: f64,
}
#[derive(Debug, Clone, Copy)]
pub struct TradeTick {
    pub symbol_id: u16,
    pub timestamp_ns: u64,
    pub synchronized: bool,
    pub observed_ns: u64,
    /// Conservative executable ask estimate, not mid-quote.
    pub best_ask_to_cover: FixedPrice,
    pub microprice: FixedPrice,
    pub spread_bps: f64,
    pub sigma_10s_bps: f64,
    pub nearest_bid_wall: Option<FixedPrice>,
    pub signals: LifecycleSignals,
}
impl TradeTick {
    pub(super) fn checked(self, symbol_id: u16, last_ns: u64)
        -> Result<(), LifecycleError>
    {
        if self.timestamp_ns <= last_ns { return Err(LifecycleError::TimeReversal); }
        if !self.synchronized || self.symbol_id != symbol_id
            || self.observed_ns > self.timestamp_ns
            || self.timestamp_ns - self.observed_ns > 50_000_000
            || self.best_ask_to_cover.0 <= 0 || self.microprice.0 <= 0
        {
            return Err(LifecycleError::InvalidInput);
        }
        let sig = self.signals;
        let vals = [
            self.spread_bps, self.sigma_10s_bps, sig.ofi_z,
            sig.book_imbalance, sig.hawkes_acceleration,
            sig.hawkes_activity_proxy, sig.bid_vacuum_ratio,
            sig.absorption_ratio,
        ];
        if !vals.iter().all(|v| v.is_finite())
            || self.spread_bps < 0.0 || self.sigma_10s_bps < 0.0
            || !(-1.0..=1.0).contains(&sig.book_imbalance)
            || sig.bid_vacuum_ratio < 0.0
            || !(0.0..=1.5).contains(&sig.hawkes_activity_proxy)
            || sig.absorption_ratio < 0.0
            || self.nearest_bid_wall.is_some_and(|p| p.0 <= 0)
        {
            return Err(LifecycleError::InvalidInput);
        }
        Ok(())
    }
}
