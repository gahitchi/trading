use super::types::{
    ExitProposal, ExitReason, LifecycleError, LifecyclePhase, LifecycleSignals, TradeTick,
};
use trading_core_types::{FixedPrice, FixedQty};
use trading_wallet_manager::Strategy;

#[derive(Debug, Clone, Copy)]
pub(super) struct PendingExit {
    pub(super) proposal: ExitProposal,
    pub(super) cumulative: FixedQty,
    pub(super) sequence: u64,
}

/// Fixed weighted *heuristic*, not a solved HJB or Snell-envelope value.
pub fn continuation_proxy(signal: LifecycleSignals, stagnant_ns: u64) -> f64 {
    let v = 0.30 * ((-signal.ofi_z - 0.5) / 2.0).tanh()
        + 0.25 * (signal.hawkes_acceleration / 2.5).tanh()
        + 0.20 * ((signal.hawkes_activity_proxy - 0.65) / 0.25).tanh()
        + 0.15 * ((0.45 - signal.bid_vacuum_ratio) / 0.25).tanh()
        - if signal.absorption_ratio > 4.0 {
            0.50
        } else {
            0.0
        }
        - 0.15 * (stagnant_ns as f64 / 10_000_000_000.0);
    v.clamp(-1.0, 1.0)
}
/// Created ONLY after externally verified account fills and reconciliation.
/// Proposals do not update exposure; only confirmed cumulative fills do.
/// The higher-level risk gate must manage protective stop orders and reserve
/// open + pending exposure.
pub struct ShortTradeLifecycle {
    pub(super) symbol_id: u16,
    pub(super) last_ns: u64,
    pub(super) remaining_qty: FixedQty,
    pub(super) phase: LifecyclePhase,
    pub(super) pending: Option<PendingExit>,
    pub(super) partial_target_raw: i64,
    pub(super) partial_realized_raw: i64,
    pub(super) stop_price_raw: i64,
    entry_ns: u64,
    entry_price: FixedPrice,
    initial_qty: FixedQty,
    initial_stop_bps: f64,
    roundtrip_fee_bps: f64,
    min_microprice_raw: i64,
    last_favorable_ns: u64,
    next_action_id: u64,
}
impl ShortTradeLifecycle {
    pub fn from_confirmed_short(
        _strategy: Strategy,
        symbol_id: u16,
        entry_ns: u64,
        entry_price: FixedPrice,
        confirmed_qty: FixedQty,
        initial_stop_bps: f64,
        roundtrip_fee_bps: f64,
    ) -> Result<Self, LifecycleError> {
        if symbol_id >= 9
            || entry_price.0 <= 0
            || confirmed_qty.0 <= 0
            || !initial_stop_bps.is_finite()
            || initial_stop_bps <= 0.0
            || initial_stop_bps > 9_000.0
            || !roundtrip_fee_bps.is_finite()
            || roundtrip_fee_bps < 0.0
        {
            return Err(LifecycleError::InvalidInput);
        }
        let candidate = entry_price.0 as f64 * (1.0 + initial_stop_bps / 10_000.0);
        if !candidate.is_finite() || candidate >= i64::MAX as f64 {
            return Err(LifecycleError::NumericOverflow);
        }
        let stop_raw = candidate.ceil() as i64;
        if stop_raw <= entry_price.0 {
            return Err(LifecycleError::InvalidInput);
        }
        let half = confirmed_qty.0 / 2;
        Ok(Self {
            symbol_id,
            entry_ns,
            last_ns: entry_ns,
            entry_price,
            initial_qty: confirmed_qty,
            remaining_qty: confirmed_qty,
            initial_stop_bps,
            roundtrip_fee_bps,
            stop_price_raw: stop_raw,
            min_microprice_raw: entry_price.0,
            last_favorable_ns: entry_ns,
            phase: LifecyclePhase::InitialRisk,
            pending: None,
            next_action_id: 1,
            partial_target_raw: if half > 0 { half } else { confirmed_qty.0 },
            partial_realized_raw: 0,
        })
    }
    pub fn phase(&self) -> LifecyclePhase {
        self.phase
    }
    pub fn remaining(&self) -> FixedQty {
        self.remaining_qty
    }
    pub fn initial_quantity(&self) -> FixedQty {
        self.initial_qty
    }
    pub fn stop_price(&self) -> FixedPrice {
        FixedPrice(self.stop_price_raw)
    }
    pub fn pending_exit(&self) -> Option<ExitProposal> {
        self.pending.map(|p| p.proposal)
    }
    pub(super) fn issue_exit(
        &mut self,
        qty_raw: i64,
        reason: ExitReason,
        partial: bool,
    ) -> Result<ExitProposal, LifecycleError> {
        if qty_raw <= 0
            || qty_raw > self.remaining_qty.0
            || self.pending.is_some()
            || self.next_action_id == u64::MAX
        {
            return Err(LifecycleError::InvalidTransition);
        }
        let proposal = ExitProposal {
            action_id: self.next_action_id,
            close_quantity: FixedQty(qty_raw),
            reason,
            reduce_only: true,
        };
        self.next_action_id += 1;
        self.pending = Some(PendingExit {
            proposal,
            cumulative: FixedQty::ZERO,
            sequence: 0,
        });
        self.phase = if partial {
            LifecyclePhase::PartialExitPending
        } else {
            LifecyclePhase::ExitPending
        };
        Ok(proposal)
    }

    pub fn on_tick(&mut self, tick: TradeTick) -> Result<Option<ExitProposal>, LifecycleError> {
        tick.checked(self.symbol_id, self.last_ns)?;
        if matches!(
            self.phase,
            LifecyclePhase::Closed | LifecyclePhase::ReconciliationRequired
        ) {
            return Err(LifecycleError::InvalidTransition);
        }
        self.last_ns = tick.timestamp_ns;
        if tick.microprice.0 < self.min_microprice_raw {
            self.min_microprice_raw = tick.microprice.0;
            self.last_favorable_ns = tick.timestamp_ns;
        }
        let stagnant = tick.timestamp_ns - self.last_favorable_ns;
        let v = continuation_proxy(tick.signals, stagnant);
        // Conservative PnL estimate based on quoted *ask to buy back*.
        let pnl_bps = (self.entry_price.0 - tick.best_ask_to_cover.0) as f64
            / self.entry_price.0 as f64
            * 10_000.0;
        let stop_hit = tick.best_ask_to_cover.0 >= self.stop_price_raw;
        // Pending orders have UNKNOWN remaining execution until reconciled.
        // No second order may be generated automatically in this state.
        if self.pending.is_some() {
            if stop_hit {
                return Err(LifecycleError::PendingExitAtRisk);
            }
            return Ok(None);
        }
        if stop_hit {
            let reason = if self.phase == LifecyclePhase::Runner {
                ExitReason::RunnerStop
            } else {
                ExitReason::HardStop
            };
            return self
                .issue_exit(self.remaining_qty.0, reason, false)
                .map(Some);
        }
        if self.phase == LifecyclePhase::InitialRisk {
            if tick.timestamp_ns - self.entry_ns <= 1_500_000_000
                && tick.signals.book_imbalance > 0.30
                && tick.signals.ofi_z > 1.2
                && v < -0.25
            {
                return self
                    .issue_exit(self.remaining_qty.0, ExitReason::Fakeout, false)
                    .map(Some);
            }
            if pnl_bps > 0.0 && pnl_bps >= 2.2 * self.roundtrip_fee_bps {
                self.phase = LifecyclePhase::Breakeven;
                // A short's fee-covered exit stop is below the entry.
                // The stop is only a target: gaps/slippage can exceed it.
                let break_even =
                    self.entry_price.0 as f64 * (1.0 - self.roundtrip_fee_bps / 10_000.0);
                if break_even > 0.0 {
                    self.stop_price_raw = self.stop_price_raw.min(break_even.floor() as i64);
                }
            }
        }
        if self.phase == LifecyclePhase::Breakeven && pnl_bps > 0.0 {
            let deceleration = pnl_bps >= 1.5 * self.initial_stop_bps && v < 0.25;
            let wall = tick.nearest_bid_wall.is_some_and(|p| {
                tick.microprice.0 >= p.0
                    && (tick.microprice.0 - p.0) as f64 / p.0 as f64 * 10_000.0 <= 2.0
                    && v < 0.55
            });
            if deceleration || wall {
                let target = self.partial_target_raw - self.partial_realized_raw;
                return self
                    .issue_exit(
                        target.min(self.remaining_qty.0),
                        ExitReason::PartialHarvest,
                        true,
                    )
                    .map(Some);
            }
        } else if self.phase == LifecyclePhase::PartialExitPending {
            // Reissue only after terminal exchange confirmation of prior
            // partial order; never overshoot the 50% target.
            let target = self.partial_target_raw - self.partial_realized_raw;
            if target > 0 {
                return self
                    .issue_exit(
                        target.min(self.remaining_qty.0),
                        ExitReason::PartialHarvest,
                        true,
                    )
                    .map(Some);
            }
        } else if self.phase == LifecyclePhase::Runner {
            let trail_bps = 2.0 * tick.spread_bps + 0.85 * tick.sigma_10s_bps;
            let target = self.min_microprice_raw as f64 * (1.0 + trail_bps / 10_000.0);
            if !target.is_finite() || target >= i64::MAX as f64 {
                return Err(LifecycleError::NumericOverflow);
            }
            self.stop_price_raw = self.stop_price_raw.min(target.ceil() as i64);
            if tick.best_ask_to_cover.0 >= self.stop_price_raw {
                return self
                    .issue_exit(self.remaining_qty.0, ExitReason::RunnerStop, false)
                    .map(Some);
            }
            let reason = if tick.signals.absorption_ratio > 4.0 {
                Some(ExitReason::Climax)
            } else if v < -0.10 {
                Some(ExitReason::ContinuationFailed)
            } else if stagnant > 15_000_000_000 {
                Some(ExitReason::Stagnation)
            } else {
                None
            };
            if let Some(reason) = reason {
                return self
                    .issue_exit(self.remaining_qty.0, reason, false)
                    .map(Some);
            }
        }
        Ok(None)
    }
}
