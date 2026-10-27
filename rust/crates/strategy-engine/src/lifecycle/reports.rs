use super::engine::{PendingExit, ShortTradeLifecycle};
use super::types::{ExitReportKind, LifecycleError, LifecyclePhase};
use trading_core_types::FixedQty;

impl ShortTradeLifecycle {
    /// Apply the exact pending action's cumulative filled quantity. Reports
    /// are ordered by an authoritative adapter-local per-order sequence.
    /// Report order is not a substitute for venue reconciliation.
    pub fn on_exit_report(
        &mut self,
        action_id: u64,
        report_seq: u64,
        cumulative: FixedQty,
        kind: ExitReportKind,
    ) -> Result<(), LifecycleError> {
        let pending = self.pending.ok_or(LifecycleError::UnknownExit)?;
        if pending.proposal.action_id != action_id {
            return Err(LifecycleError::UnknownExit);
        }
        if report_seq == 0 || report_seq <= pending.sequence {
            return Err(LifecycleError::NonMonotonicReport);
        }
        if cumulative.0 < pending.cumulative.0 || cumulative.0 > pending.proposal.close_quantity.0 {
            return Err(LifecycleError::InvalidInput);
        }
        if kind == ExitReportKind::Filled && cumulative != pending.proposal.close_quantity {
            return Err(LifecycleError::InvalidInput);
        }
        let delta = cumulative.0 - pending.cumulative.0;
        if delta > self.remaining_qty.0 {
            return Err(LifecycleError::InvalidInput);
        }
        self.remaining_qty = FixedQty(self.remaining_qty.0 - delta);
        if self.phase == LifecyclePhase::PartialExitPending {
            self.partial_realized_raw += delta;
        }
        if kind == ExitReportKind::Unknown {
            self.pending = None;
            self.phase = LifecyclePhase::ReconciliationRequired;
            return Ok(());
        }
        if matches!(
            kind,
            ExitReportKind::Canceled | ExitReportKind::Rejected | ExitReportKind::Filled
        ) {
            self.pending = None;
            if self.remaining_qty.0 == 0 {
                self.phase = LifecyclePhase::Closed;
            } else if self.phase == LifecyclePhase::PartialExitPending {
                if self.partial_realized_raw >= self.partial_target_raw {
                    self.phase = LifecyclePhase::Runner;
                }
                // If canceled/rejected early, remain in PartialExitPending;
                // next fresh tick reissues ONLY the unfilled portion.
            } else {
                // A failed full-exit leaves unresolved short inventory.
                // No auto re-entry or second close without reconciliation.
                self.phase = LifecyclePhase::ReconciliationRequired;
            }
        } else {
            self.pending = Some(PendingExit {
                proposal: pending.proposal,
                cumulative,
                sequence: report_seq,
            });
        }
        Ok(())
    }
}
