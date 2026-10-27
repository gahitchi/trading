use crate::{SignalError, decay, finite_positive, positive_price};
use trading_core_types::FixedPrice;

const MAX_INTERVAL_NS: u64 = 50_000_000; // 50ms

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PairSymbol {
    Leader,
    Laggard,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct LeadLagSnapshot {
    pub beta: f64,
    pub posterior_variance: f64,
    pub leader_log_return: f64,
    pub laggard_log_return: f64,
    pub lag_signal_bps: f64,
    pub effective_observation_variance: f64,
    pub synchronization_density: f64,
}
#[derive(Debug, Clone, Copy)]
struct ReturnInterval {
    start_ns: u64,
    end_ns: u64,
    log_return: f64,
}
#[derive(Debug, Clone, Copy, Default)]
struct StreamState {
    last_ns: Option<u64>,
    last_px: f64,
    interval: Option<ReturnInterval>,
    sequence: u64,
}
impl StreamState {
    fn update(&mut self, ts_ns: u64, price: f64) -> Result<(), SignalError> {
        if self.last_ns.is_some_and(|old| ts_ns <= old) {
            return Err(SignalError::NonMonotonicTime);
        }
        self.interval = self.last_ns.and_then(|old| {
            if ts_ns - old > MAX_INTERVAL_NS {
                None
            } else {
                Some(ReturnInterval {
                    start_ns: old,
                    end_ns: ts_ns,
                    log_return: (price / self.last_px).ln(),
                })
            }
        });
        self.last_ns = Some(ts_ns);
        self.last_px = price;
        self.sequence = self.sequence.wrapping_add(1);
        Ok(())
    }
}

/// O(1) overlap-*gated* asynchronous Kalman beta estimate.
///
/// This deliberately does NOT claim to compute full Hayashi-Yoshida
/// covariance: an exact HY estimator must sum *all* overlapping asynchronous
/// return intervals, potentially more than one per event, which contradicts
/// the specified strict O(1) per-event requirement without bounded sampling.
/// We update only on a new laggard interval paired to one not-yet-consumed
/// overlapping 50ms leader interval. This avoids forward-filled lookahead.
pub struct KalmanLeadLagEngine {
    leader: StreamState,
    laggard: StreamState,
    last_global_ns: Option<u64>,
    matched_leader_sequence: u64,
    beta: f64,
    posterior_variance: f64,
    process_variance: f64,
    base_observation_variance: f64,
    synchronization_density: f64,
    last_laggard_ns: Option<u64>,
}
impl KalmanLeadLagEngine {
    pub fn new(
        initial_beta: f64,
        initial_variance: f64,
        process_variance: f64,
        base_observation_variance: f64,
    ) -> Result<Self, SignalError> {
        if !initial_beta.is_finite() {
            return Err(SignalError::InvalidConfiguration);
        }
        Ok(Self {
            leader: StreamState::default(),
            laggard: StreamState::default(),
            last_global_ns: None,
            matched_leader_sequence: 0,
            beta: initial_beta,
            posterior_variance: finite_positive(initial_variance)?,
            process_variance: finite_positive(process_variance)?,
            base_observation_variance: finite_positive(base_observation_variance)?,
            synchronization_density: 1.0,
            last_laggard_ns: None,
        })
    }
    pub fn reset(&mut self, initial_beta: f64, initial_variance: f64) -> Result<(), SignalError> {
        if !initial_beta.is_finite() {
            return Err(SignalError::InvalidConfiguration);
        }
        self.beta = initial_beta;
        self.posterior_variance = finite_positive(initial_variance)?;
        self.leader = StreamState::default();
        self.laggard = StreamState::default();
        self.last_global_ns = None;
        self.last_laggard_ns = None;
        self.matched_leader_sequence = 0;
        self.synchronization_density = 1.0;
        Ok(())
    }
    /// Input must be an event-time-ordered merge of both streams. A failed
    /// synchronization check produces None and must NOT yield a trading signal.
    pub fn observe(
        &mut self,
        symbol: PairSymbol,
        ts_ns: u64,
        price: FixedPrice,
    ) -> Result<Option<LeadLagSnapshot>, SignalError> {
        let price = positive_price(price)?;
        if self.last_global_ns.is_some_and(|old| ts_ns < old) {
            return Err(SignalError::NonMonotonicTime);
        }
        match symbol {
            PairSymbol::Leader => self.leader.update(ts_ns, price)?,
            PairSymbol::Laggard => self.laggard.update(ts_ns, price)?,
        };
        self.last_global_ns = Some(ts_ns);
        if symbol == PairSymbol::Leader {
            return Ok(None);
        }
        let dt = self
            .last_laggard_ns
            .map_or(0.0, |old| (ts_ns - old) as f64 * 1e-9);
        self.last_laggard_ns = Some(ts_ns);

        let overlaps = match (self.leader.interval, self.laggard.interval) {
            (Some(x), Some(y))
                if x.start_ns < y.end_ns
                    && y.start_ns < x.end_ns
                    && self.leader.sequence != self.matched_leader_sequence =>
            {
                Some((x, y))
            }
            _ => None,
        };
        let matched = overlaps.is_some();
        let alpha = decay(dt, 0.050);
        self.synchronization_density = (alpha * self.synchronization_density
            + (1.0 - alpha) * if matched { 1.0 } else { 0.0 })
        .clamp(0.05, 1.0);
        let Some((x, y)) = overlaps else {
            return Ok(None);
        };
        let r = self.base_observation_variance / self.synchronization_density;
        let predicted_variance = self.posterior_variance + self.process_variance;
        let innovation_var = x.log_return * x.log_return * predicted_variance + r;
        if !innovation_var.is_finite() || innovation_var <= 0.0 {
            return Err(SignalError::NumericOverflow);
        }
        let gain = predicted_variance * x.log_return / innovation_var;
        let innovation = y.log_return - self.beta * x.log_return;
        let next_beta = self.beta + gain * innovation;
        let residual = 1.0 - gain * x.log_return;
        // Joseph form retains non-negative variance in finite precision.
        let next_variance = residual * residual * predicted_variance + gain * gain * r;
        let lag_bps = (next_beta * x.log_return - y.log_return) * 10_000.0;
        if !next_beta.is_finite() || !next_variance.is_finite() || !lag_bps.is_finite() {
            return Err(SignalError::NumericOverflow);
        }
        self.beta = next_beta;
        self.posterior_variance = next_variance;
        self.matched_leader_sequence = self.leader.sequence;
        Ok(Some(LeadLagSnapshot {
            beta: self.beta,
            posterior_variance: self.posterior_variance,
            leader_log_return: x.log_return,
            laggard_log_return: y.log_return,
            lag_signal_bps: lag_bps,
            effective_observation_variance: r,
            synchronization_density: self.synchronization_density,
        }))
    }
}
