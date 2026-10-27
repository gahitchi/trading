use trading_core_types::{FixedPrice, OperatorSnapshot, TradingMode};
use trading_wallet_manager::Strategy;

const MAX_SIGNAL_AGE_NS: u64 = 50_000_000;
const LIQ_LOOKBACK_NS: u64 = 1_500_000_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StrategyError {
    InvalidSignal,
    StaleOrFutureSignal,
    InvalidOperator,
    InvalidTrap,
    TimeReversal,
}

#[derive(Debug, Clone, Copy)]
pub struct EntrySnapshot {
    /// Timestamp of the *oldest* signal used in this merged observation.
    pub observed_ns: u64,
    pub feed_live_and_synchronized: bool,
    pub symbol_id: u16,
    pub microprice: FixedPrice,
    pub ofi_z: f64,
    pub bid_imbalance: f64,
    pub bid_vacuum: f64,
    pub signed_sell_toxicity: f64,
    pub isr_down: f64,
    /// This is the provisional Part 2 activity proxy, NOT Hawkes spectral radius.
    pub hawkes_activity_proxy: f64,
    pub last_long_liquidation_ns: Option<u64>,
    pub leader_isr_down: f64,
    pub leader_return_50ms_bps: f64,
    pub laggard_residual_bps: f64,
}
impl EntrySnapshot {
    fn validate(self, now_ns: u64) -> Result<(), StrategyError> {
        if !self.feed_live_and_synchronized
            || self.observed_ns > now_ns
            || now_ns - self.observed_ns > MAX_SIGNAL_AGE_NS
        {
            return Err(StrategyError::StaleOrFutureSignal);
        }
        let values = [
            self.ofi_z,
            self.bid_imbalance,
            self.bid_vacuum,
            self.signed_sell_toxicity,
            self.isr_down,
            self.hawkes_activity_proxy,
            self.leader_isr_down,
            self.leader_return_50ms_bps,
            self.laggard_residual_bps,
        ];
        if !values.iter().all(|x| x.is_finite())
            || self.microprice.0 <= 0
            || !(-1.0..=1.0).contains(&self.bid_imbalance)
            || self.bid_vacuum < 0.0
            || !(-1.0..=1.0).contains(&self.signed_sell_toxicity)
            || self.isr_down < 0.0
            || self.leader_isr_down < 0.0
            || !(0.0..=1.5).contains(&self.hawkes_activity_proxy)
        {
            return Err(StrategyError::InvalidSignal);
        }
        if self.last_long_liquidation_ns.is_some_and(|ts| ts > now_ns) {
            return Err(StrategyError::InvalidSignal);
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EntryCandidate {
    pub strategy: Strategy,
    pub symbol_id: u16,
    pub decision_ts_ns: u64,
    /// True means an *entry proposal* only. Never an executable order.
    pub short_only: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ManualTrap {
    pub arm_id: u64,
    pub symbol_id: u16,
    pub threshold_price: FixedPrice,
    pub expires_at_ns: u64,
}

/// Explicit one-shot manual short trap. Never derive an arm token from the
/// packed operator heartbeat: it has no authentication or freshness proof.
#[derive(Default)]
pub struct EntryEngine {
    last_now_ns: Option<u64>,
    arm: Option<ManualTrap>,
}
impl EntryEngine {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn disarm(&mut self) {
        self.arm = None;
    }
    pub fn reset_on_feed_gap(&mut self) {
        self.arm = None;
        self.last_now_ns = None;
    }
    pub fn armed(&self) -> Option<ManualTrap> {
        self.arm
    }

    pub fn arm_trap(
        &mut self,
        token: ManualTrap,
        now_ns: u64,
        operator: OperatorSnapshot,
    ) -> Result<(), StrategyError> {
        operator
            .validate()
            .map_err(|_| StrategyError::InvalidOperator)?;
        if operator.mode != TradingMode::ManualArmedTrap
            || operator.strategy_enable_mask & 0b1000 == 0
            || token.arm_id == 0
            || token.symbol_id >= 9
            || token.threshold_price.0 <= 0
            || token.expires_at_ns <= now_ns
            || token.expires_at_ns - now_ns > 300_000_000_000
            || operator.active_symbol_mask & (1 << token.symbol_id) == 0
            || self.last_now_ns.is_some_and(|prev| now_ns < prev)
        {
            return Err(StrategyError::InvalidTrap);
        }
        self.arm = Some(token);
        self.last_now_ns = Some(now_ns);
        Ok(())
    }

    pub fn evaluate(
        &mut self,
        now_ns: u64,
        operator: OperatorSnapshot,
        snapshot: EntrySnapshot,
    ) -> Result<Option<EntryCandidate>, StrategyError> {
        operator
            .validate()
            .map_err(|_| StrategyError::InvalidOperator)?;
        if self.last_now_ns.is_some_and(|prev| now_ns < prev) {
            return Err(StrategyError::TimeReversal);
        }
        snapshot.validate(now_ns)?;
        self.last_now_ns = Some(now_ns);

        if snapshot.symbol_id >= 9
            || operator.active_symbol_mask & (1 << snapshot.symbol_id) == 0
            || operator.mode == TradingMode::HaltAll
        {
            self.arm = None;
            return Ok(None);
        }
        if let Some(arm) = self.arm {
            if now_ns >= arm.expires_at_ns
                || operator.mode != TradingMode::ManualArmedTrap
                || operator.strategy_enable_mask & 0b1000 == 0
            {
                self.arm = None;
            }
        }
        // ManualArmedTrap is intentionally distinct from the normal short
        // modes. It cannot inadvertently run A/B/C automated triggers.
        if operator.mode == TradingMode::ManualArmedTrap {
            let fired = self.arm.is_some_and(|arm| {
                arm.symbol_id == snapshot.symbol_id
                    && snapshot.microprice <= arm.threshold_price
                    && snapshot.ofi_z < -1.8
                    && snapshot.bid_imbalance < -0.40
            });
            if fired {
                self.arm = None; // one-shot, no repeated trigger after a tick
                return Ok(Some(EntryCandidate {
                    strategy: Strategy::D,
                    symbol_id: snapshot.symbol_id,
                    decision_ts_ns: now_ns,
                    short_only: true,
                }));
            }
            return Ok(None);
        }
        if !matches!(
            operator.mode,
            TradingMode::ShortOnly | TradingMode::ShortBiased | TradingMode::BiDirectional
        ) {
            self.arm = None;
            return Ok(None);
        }
        let eta = f64::from(operator.aggressiveness_pct) / 100.0;
        let m = operator.strategy_enable_mask;
        // Priority B > C > A when several signals arrive on the same tick.
        // This does not allocate capital or infer that either is profitable.
        let strategy = if m & 0b0010 != 0
            && snapshot.isr_down > 4.5 / eta
            && snapshot.hawkes_activity_proxy > 0.8
            && snapshot
                .last_long_liquidation_ns
                .is_some_and(|ts| now_ns - ts <= LIQ_LOOKBACK_NS)
            && snapshot.ofi_z < -2.0 / eta
        {
            Some(Strategy::B)
        } else if m & 0b0100 != 0
            && snapshot.leader_isr_down > 4.0
            && snapshot.leader_return_50ms_bps < -12.0
            && snapshot.laggard_residual_bps < -10.0 / eta
            && snapshot.ofi_z < -1.0
        {
            Some(Strategy::C)
        } else if m & 0b0001 != 0
            && snapshot.ofi_z < -2.8 / eta
            && snapshot.bid_imbalance < -0.60
            && snapshot.bid_vacuum < 0.35
            && snapshot.signed_sell_toxicity > 0.35
        {
            Some(Strategy::A)
        } else {
            None
        };
        Ok(strategy.map(|s| EntryCandidate {
            strategy: s,
            symbol_id: snapshot.symbol_id,
            decision_ts_ns: now_ns,
            short_only: true,
        }))
    }
}
