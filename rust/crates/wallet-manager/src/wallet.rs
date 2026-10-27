use crate::{WalletError, narrow};
use trading_core_types::FixedUsd;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WalletSnapshot {
    pub margin_reserve: FixedUsd,
    pub locked_profit_vault: FixedUsd,
    pub active_equity: FixedUsd,
    pub session_high_water_mark: FixedUsd,
    pub session_floor: FixedUsd,
    pub total_equity: FixedUsd,
    /// A latched breach requires external reconciliation. No automatic rearm.
    pub halt_required: bool,
    pub last_settlement_id: u64,
}

/// Modelled *accounting buckets*, not physically segregated broker funds.
/// A 30% reserve does not by itself protect against margin liquidation.
/// All balance mutations are checked integer operations; settlement IDs
/// strictly increase so replays cannot credit the same closed trade twice.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WalletBuckets {
    margin_reserve: FixedUsd,
    locked_profit_vault: FixedUsd,
    active_equity: FixedUsd,
    high_water_mark: FixedUsd,
    floor: FixedUsd,
    halt_required: bool,
    last_settlement_id: u64,
}
impl WalletBuckets {
    pub fn new(initial_equity: FixedUsd) -> Result<Self, WalletError> {
        if initial_equity.0 <= 0 { return Err(WalletError::InvalidAmount); }
        let reserve = narrow(i128::from(initial_equity.0) * 30 / 100)?;
        let active = narrow(i128::from(initial_equity.0) - i128::from(reserve))?;
        let floor = narrow(i128::from(active) * 96 / 100)?;
        Ok(Self {
            margin_reserve: FixedUsd(reserve),
            locked_profit_vault: FixedUsd::ZERO,
            active_equity: FixedUsd(active),
            high_water_mark: FixedUsd(active),
            floor: FixedUsd(floor),
            halt_required: false,
            last_settlement_id: 0,
        })
    }

    pub fn snapshot(&self) -> Result<WalletSnapshot, WalletError> {
        let total = i128::from(self.margin_reserve.0)
            + i128::from(self.locked_profit_vault.0)
            + i128::from(self.active_equity.0);
        Ok(WalletSnapshot {
            margin_reserve: self.margin_reserve,
            locked_profit_vault: self.locked_profit_vault,
            active_equity: self.active_equity,
            session_high_water_mark: self.high_water_mark,
            session_floor: self.floor,
            total_equity: FixedUsd(narrow(total)?),
            halt_required: self.halt_required,
            last_settlement_id: self.last_settlement_id,
        })
    }

    pub fn halt_required(&self) -> bool { self.halt_required }

    /// Receive an externally verified *net realized* close P&L, including
    /// broker fees, slippage, financing and funding. The unique settlement
    /// ID must be strictly monotonic across the entire account.
    ///
    /// Profits are split into active/vault. Losses debit only active.
    /// If an unexpected loss would make active equity negative, latch halt
    /// and return Insolvent without manufacturing a negative bucket.
    /// External account reconciliation is mandatory in that case.
    pub fn settle_close(
        &mut self, settlement_id: u64, net_pnl: FixedUsd, lock_rate_pct: u8,
    ) -> Result<WalletSnapshot, WalletError> {
        if settlement_id == 0 || settlement_id <= self.last_settlement_id {
            return Err(WalletError::NonMonotonicSettlement);
        }
        if lock_rate_pct > 100 { return Err(WalletError::InvalidPercentage); }
        let (next_vault, next_active) = if net_pnl.0 > 0 {
            let sweep = i128::from(net_pnl.0) * i128::from(lock_rate_pct) / 100;
            (
                narrow(i128::from(self.locked_profit_vault.0) + sweep)?,
                narrow(i128::from(self.active_equity.0) + i128::from(net_pnl.0) - sweep)?,
            )
        } else {
            let new_active = i128::from(self.active_equity.0) + i128::from(net_pnl.0);
            if new_active < 0 {
                self.halt_required = true;
                return Err(WalletError::Insolvent);
            }
            (self.locked_profit_vault.0, narrow(new_active)?)
        };
        // Reconciliation must remain representable before mutating state;
        // returning an error after crediting funds would break atomicity.
        let next_total = i128::from(self.margin_reserve.0)
            + i128::from(next_vault) + i128::from(next_active);
        narrow(next_total)?;
        let next_hwm = self.high_water_mark.0.max(next_active);
        let next_floor = narrow(i128::from(next_hwm) * 96 / 100)?;
        self.locked_profit_vault = FixedUsd(next_vault);
        self.active_equity = FixedUsd(next_active);
        self.high_water_mark = FixedUsd(next_hwm);
        self.floor = FixedUsd(next_floor);
        self.last_settlement_id = settlement_id;
        if next_active <= next_floor { self.halt_required = true; }
        self.snapshot()
    }

    /// 1_000_000 = 100% cushion; integer squared CPPI damping.
    pub fn cppi_damper_ppm(&self) -> Result<u32, WalletError> {
        if self.halt_required { return Ok(0); }
        let cushion = i128::from(self.active_equity.0) - i128::from(self.floor.0);
        if cushion <= 0 { return Ok(0); }
        // The denominator is 4% of the *exact* high-water mark, not a
        // post-rounded floor, avoiding distortions at small balances.
        let denom = i128::from(self.high_water_mark.0) * 4;
        if denom == 0 { return Err(WalletError::InvalidAmount); }
        let linear_ppm = (cushion * 100_000_000 / denom).clamp(0, 1_000_000);
        let squared = linear_ppm * linear_ppm / 1_000_000;
        Ok(squared as u32)
    }
}
