use crate::{WalletError, mul_scaled, narrow, ratio};
use trading_core_types::{FixedUsd, SCALE};

const PRIOR_ALPHA: i128 = 11;
const PRIOR_BETA: i128 = 9;
const PRIOR_PAYOFF_MASS: i128 = 10;
const RETAIN_BPS: i128 = 9_900;
const MIN_EFFECTIVE_SAMPLES: i128 = 30;
const MAX_OBSERVED_R: i128 = 100;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Strategy {
    A,
    B,
    C,
    D,
}
impl Strategy {
    const fn index(self) -> usize {
        match self {
            Self::A => 0,
            Self::B => 1,
            Self::C => 2,
            Self::D => 3,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct KellyEstimate {
    /// 1e8 scaled win probability; fixed Beta(11,9) prior.
    pub win_probability_raw: i64,
    /// 1e8 scaled average winning R / average losing R.
    pub payoff_ratio_raw: i64,
    /// 1e8 scaled quarter-Kelly risk fraction; zero if edge <= 0.
    pub risk_fraction_raw: i64,
    /// 1e8 scaled effective sample mass after exponential decay.
    pub effective_samples_raw: i64,
    /// Deployment gate: >= 30 effective outcomes; still requires OOS validation.
    pub sample_ready: bool,
}

#[derive(Debug, Clone, Copy, Default)]
struct Evidence {
    wins: i128,
    losses: i128,
    win_sum_r: i128,
    loss_sum_r: i128,
}
impl Evidence {
    fn observe(&mut self, positive: bool, r: i128) -> Result<(), WalletError> {
        if r <= 0 || r > MAX_OBSERVED_R * i128::from(SCALE) {
            return Err(WalletError::InvalidObservation);
        }
        let mut next = *self;
        next.wins = next.wins * RETAIN_BPS / 10_000;
        next.losses = next.losses * RETAIN_BPS / 10_000;
        next.win_sum_r = next.win_sum_r * RETAIN_BPS / 10_000;
        next.loss_sum_r = next.loss_sum_r * RETAIN_BPS / 10_000;
        if positive {
            next.wins += i128::from(SCALE);
            next.win_sum_r += r;
        } else {
            next.losses += i128::from(SCALE);
            next.loss_sum_r += r;
        }
        *self = next;
        Ok(())
    }
}

/// Fixed-prior Beta-Bernoulli shrinkage and exponentially weighted payoff-R
/// observations. This is *not* empirical-Bayes hyperparameter fitting, and
/// treating realized trades as IID would be a statistical error.
/// Keep separate per-strategy histories. Observations are reward-to-ex-ante-
/// risk multiples, never raw heterogeneous USD profit/loss magnitudes.
#[derive(Debug, Clone, Copy, Default)]
pub struct BayesianKelly {
    strategies: [Evidence; 4],
}
impl BayesianKelly {
    pub fn new() -> Self {
        Self::default()
    }

    /// Caller must first settle net P&L in the account's durable ledger.
    /// Observations >100R are rejected rather than silently clipping tails.
    pub fn observe(
        &mut self,
        strategy: Strategy,
        net_pnl: FixedUsd,
        ex_ante_risk: FixedUsd,
    ) -> Result<(), WalletError> {
        if ex_ante_risk.0 <= 0 || net_pnl.0 == 0 {
            return Err(WalletError::InvalidObservation);
        }
        let absolute = i128::from(net_pnl.0).abs();
        let r = ratio(absolute, i128::from(ex_ante_risk.0))?;
        self.strategies[strategy.index()].observe(net_pnl.0 > 0, r)
    }

    pub fn estimate(&self, strategy: Strategy) -> Result<KellyEstimate, WalletError> {
        let e = self.strategies[strategy.index()];
        let scale = i128::from(SCALE);
        let posterior_p = ratio(
            PRIOR_ALPHA * scale + e.wins,
            (PRIOR_ALPHA + PRIOR_BETA) * scale + e.wins + e.losses,
        )?;
        let mean_win = ratio(15 * scale + e.win_sum_r, PRIOR_PAYOFF_MASS * scale + e.wins)?;
        let mean_loss = ratio(
            10 * scale + e.loss_sum_r,
            PRIOR_PAYOFF_MASS * scale + e.losses,
        )?;
        let payoff_ratio = ratio(mean_win, mean_loss)?;
        let lose_p = scale - posterior_p;
        let loss_adjustment = ratio(lose_p, payoff_ratio)?;
        let full_kelly = posterior_p - loss_adjustment;
        // Important deviation from the raw supplied clamp: if the estimated
        // edge is non-positive, a forced 0.4% risk is NOT acceptable.
        let quarter_kelly = if full_kelly <= 0 {
            0
        } else {
            (full_kelly / 4).clamp(400_000, 3_500_000)
        };
        let samples = e.wins + e.losses;
        let _checked = mul_scaled(posterior_p, payoff_ratio)?;
        Ok(KellyEstimate {
            win_probability_raw: narrow(posterior_p)?,
            payoff_ratio_raw: narrow(payoff_ratio)?,
            risk_fraction_raw: narrow(quarter_kelly)?,
            effective_samples_raw: narrow(samples)?,
            sample_ready: samples >= MIN_EFFECTIVE_SAMPLES * scale,
        })
    }
}
