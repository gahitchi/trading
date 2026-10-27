use crate::{SignalError, decay, elapsed_s, finite_positive};
use trading_core_types::{FixedUsd, SCALE};

const BETA_FAST: f64 = 8.0;
const BETA_LIQ: f64 = 2.5;
const BETA_SLOW: f64 = 1.2;
const ALPHA_FAST: f64 = 3.2;
const ALPHA_LIQ: f64 = 6.5;
const ALPHA_CROSS: f64 = 2.8;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HawkesEvent {
    AggressiveSell,
    AggressiveBuy,
    ForcedLongLiquidation,
    ForcedShortLiquidation,
}
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct HawkesSnapshot {
    pub isr_down: f64,
    pub isr_up: f64,
    pub down_acceleration: f64,
    /// User-specified bounded activity-dependent proxy, not a spectral radius.
    pub effective_branching_proxy: f64,
    /// Fixed-parameter triangular kernel's true spectral radius: 2.6 (>1).
    pub fixed_kernel_spectral_radius: f64,
    pub mean_trade_notional_usd: f64,
}
#[derive(Default)]
struct Cascades {
    fast: f64,
    liquidation: f64,
    slow: f64,
}
impl Cascades {
    fn decay(&mut self, dt: f64) {
        self.fast *= decay(dt, 1.0 / BETA_FAST);
        self.liquidation *= decay(dt, 1.0 / BETA_LIQ);
        self.slow *= decay(dt, 1.0 / BETA_SLOW);
    }
    fn trade(&mut self, weight: f64) {
        self.fast += ALPHA_FAST * weight;
        self.slow += 0.25 * ALPHA_FAST * weight;
    }
    fn forced_liquidation(&mut self, weight: f64) {
        self.liquidation += ALPHA_LIQ * weight;
        self.fast += ALPHA_CROSS * weight;
    }
}
/// Two explicitly separated one-way cascade channels. The upward channel is
/// a symmetric proxy; it is NOT inferred from selling activity, and is not a
/// separately fitted Hawkes model. The given coefficients imply a
/// *supercritical* kernel (2.6); the 0.85 proxy threshold is NOT a statistical
/// proof of regime change.
pub struct HawkesCascadeEngine {
    baseline_intensity: f64,
    down: Cascades,
    up: Cascades,
    mean_notional: f64,
    previous_ns: Option<u64>,
}
impl HawkesCascadeEngine {
    pub fn new(baseline_intensity: f64) -> Result<Self, SignalError> {
        Ok(Self {
            baseline_intensity: finite_positive(baseline_intensity)?,
            down: Cascades::default(),
            up: Cascades::default(),
            mean_notional: 0.0,
            previous_ns: None,
        })
    }
    pub fn reset(&mut self) {
        self.down = Cascades::default();
        self.up = Cascades::default();
        self.previous_ns = None;
        self.mean_notional = 0.0;
    }
    pub fn advance(&mut self, ts_ns: u64) -> Result<HawkesSnapshot, SignalError> {
        let dt = elapsed_s(self.previous_ns, ts_ns)?;
        self.down.decay(dt);
        self.up.decay(dt);
        self.previous_ns = Some(ts_ns);
        Ok(self.snapshot())
    }
    pub fn observe(
        &mut self,
        ts_ns: u64,
        event: HawkesEvent,
        notional: FixedUsd,
    ) -> Result<HawkesSnapshot, SignalError> {
        if notional.0 <= 0 {
            return Err(SignalError::InvalidQuantity);
        }
        let now = notional.0 as f64 / SCALE as f64;
        let previous_mean = if self.mean_notional == 0.0 {
            now
        } else {
            self.mean_notional
        };
        let weight = (now / previous_mean).min(5.0);
        let dt = elapsed_s(self.previous_ns, ts_ns)?;
        let result = self.advance(ts_ns);
        result?;
        match event {
            HawkesEvent::AggressiveSell => self.down.trade(weight),
            HawkesEvent::AggressiveBuy => self.up.trade(weight),
            HawkesEvent::ForcedLongLiquidation => self.down.forced_liquidation(weight),
            HawkesEvent::ForcedShortLiquidation => self.up.forced_liquidation(weight),
        }
        let retention = decay(dt, 300.0);
        self.mean_notional = retention * previous_mean + (1.0 - retention) * now;
        Ok(self.snapshot())
    }
    pub fn snapshot(&self) -> HawkesSnapshot {
        let mu = self.baseline_intensity;
        let isr_down = 1.0 + (self.down.fast + self.down.liquidation) / mu;
        let isr_up = 1.0 + (self.up.fast + self.up.liquidation) / mu;
        let denominator = self.down.fast + self.down.liquidation + 1.0;
        let proxy = ((ALPHA_FAST / BETA_FAST) * self.down.fast / denominator
            + ((ALPHA_LIQ + ALPHA_CROSS) / BETA_LIQ) * self.down.liquidation / denominator)
            .clamp(0.0, 1.5);
        HawkesSnapshot {
            isr_down,
            isr_up,
            down_acceleration: isr_down - (1.0 + self.down.slow / mu),
            effective_branching_proxy: proxy,
            fixed_kernel_spectral_radius: (ALPHA_FAST / BETA_FAST).max(ALPHA_LIQ / BETA_LIQ),
            mean_trade_notional_usd: self.mean_notional,
        }
    }
}
