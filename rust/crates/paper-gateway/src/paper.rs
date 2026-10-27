use crate::journal::{Journal, JournalError, RecordKind, ReplaySummary};
use crate::supervision::{PaperStatus, SafetyInterlock};
use std::path::Path;
use trading_core_types::{
    AtomicOperatorState, ComplianceContext, FixedPrice, FixedQty, FixedUsd, OrderIntent, OrderSide,
    SCALE, TimeInForce, VenueId,
};
use trading_risk_gate::{
    AccountEvidence, GateDecision, MarketEvidence, RiskContext, RiskGate, RiskPolicy, RiskReason,
};
use trading_wallet_manager::{WalletBuckets, WalletSnapshot};

#[derive(Debug)]
pub enum PaperError {
    Journal(JournalError),
    Risk(RiskReason),
    InvalidQuote,
    InvalidFlow,
    NoFill,
    NotArmed,
    UnexpectedDecision,
    Arithmetic,
}
impl From<JournalError> for PaperError {
    fn from(x: JournalError) -> Self {
        Self::Journal(x)
    }
}
impl std::fmt::Display for PaperError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for PaperError {}

/// Deterministically generated, synthetic, USD-denominated security market
/// depth. This is NEVER represented as actual exchange market data.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PaperQuote {
    pub seq: u64,
    pub exchange_ns: u64,
    pub recv_ns: u64,
    pub best_bid: FixedPrice,
    pub best_ask: FixedPrice,
    pub bid_quantity: FixedQty,
    pub ask_quantity: FixedQty,
}
impl PaperQuote {
    pub fn validate(self) -> Result<(), PaperError> {
        if self.seq == 0
            || self.exchange_ns == 0
            || self.recv_ns < self.exchange_ns
            || self.best_bid.0 <= 0
            || self.best_ask.0 <= self.best_bid.0
            || self.bid_quantity.0 < 0
            || self.ask_quantity.0 < 0
        {
            return Err(PaperError::InvalidQuote);
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Copy)]
pub struct DemoReport {
    pub entry_id: u64,
    pub cover_id: u64,
    pub entry_filled: FixedQty,
    pub cover_filled: FixedQty,
    pub realized_net_pnl: FixedUsd,
    pub end_wallet: WalletSnapshot,
    pub journal_events: u64,
}

/// Supervised offline execution flow. No public exchange connector and no
/// broker credential capability. Internal risk proof flags mean **synthetic
/// paper simulator only** and never assert a real account was reconciled.
pub struct OfflinePaperSession {
    journal: Journal,
    gate: RiskGate,
    operator: AtomicOperatorState,
    interlock: SafetyInterlock,
    quote: Option<PaperQuote>,
}
impl OfflinePaperSession {
    pub fn create(path: &Path) -> Result<Self, PaperError> {
        let journal = Journal::create(path)?;
        let gate = RiskGate::new(RiskPolicy::default()).map_err(|_| PaperError::InvalidFlow)?;
        Ok(Self {
            journal,
            gate,
            operator: AtomicOperatorState::new(Default::default())
                .map_err(|_| PaperError::InvalidFlow)?,
            interlock: SafetyInterlock::new(),
            quote: None,
        })
    }
    pub fn summary(&self) -> &ReplaySummary {
        self.journal.summary()
    }
    pub fn status(&self) -> PaperStatus {
        self.interlock.status()
    }
    pub fn wallet(&self) -> WalletSnapshot {
        self.journal.summary().wallet()
    }

    fn context(&self, q: PaperQuote) -> RiskContext {
        let s = self.journal.summary();
        // Reserved entry gross notional is included in current exposure
        // where appropriate. All accounts and collateral are paper-only.
        let current_pos_notional =
            i128::from(s.confirmed_short_qty_raw) * i128::from(q.best_ask.0) / i128::from(SCALE);
        let pending_entry_notional = if s.pending_is_cover() {
            0
        } else {
            i128::from(s.pending_quantity_raw()) * i128::from(q.best_ask.0) / i128::from(SCALE)
        };
        let gross = current_pos_notional
            .saturating_add(pending_entry_notional)
            .clamp(0, i128::from(i64::MAX));
        RiskContext {
            market: MarketEvidence {
                now_ns: q.recv_ns,
                quote_received_ns: q.recv_ns,
                synchronized: true,
                certified_event_provenance: true, // synthetic fixture provenance
                venue: VenueId::Equities,
                symbol_id: 0,
                funding_8h_bps_scaled: 0,
                perp_index_basis_bps_scaled: 0,
                funding_asof_ns: q.recv_ns,
                basis_asof_ns: q.recv_ns,
                isr_up_scaled: SCALE,
                ofi_z_scaled: 0,
            },
            account: AccountEvidence {
                reconciled: true,           // from local journal, NOT an actual broker
                reservations_current: true, // single pending journal record
                gross_open_and_pending_usd: FixedUsd(gross as i64),
                confirmed_short_qty: FixedQty(s.confirmed_short_qty_raw),
                maintenance_margin_headroom_usd: FixedUsd(10_000 * SCALE),
                rollover_or_policy_blackout: false,
            },
            equities: ComplianceContext {
                national_best_bid: q.best_bid.0,
                tick_size: SCALE / 100, // synthetic one-cent tick
                short_sale_restriction_active: false,
                located_for_open_short: true, // synthetic locate, NEVER real
                trading_blackout: false,
            },
            paper_environment: true,
        }
    }
    pub fn observe_quote(&mut self, quote: PaperQuote) -> Result<(), PaperError> {
        quote.validate()?;
        if self
            .quote
            .is_some_and(|last| quote.seq <= last.seq || quote.recv_ns <= last.recv_ns)
        {
            return Err(PaperError::InvalidQuote);
        }
        // There is no external source to qualify here; the quote is a
        // deterministic simulator fixture and can never be live market data.
        let directive = self
            .gate
            .on_market_packet(quote.exchange_ns, quote.recv_ns)
            .map_err(|_| PaperError::InvalidQuote)?;
        self.quote = Some(quote);
        if directive.cancel_resting_entries && self.interlock.entries_allowed() {
            self.interlock.halt_entries(&self.operator);
        }
        let ctx = self.context(quote);
        let directive = self
            .gate
            .monitor(&ctx, self.wallet(), &self.operator)
            .map_err(|_| PaperError::InvalidFlow)?;
        if directive.halt_all {
            self.interlock.halt_entries(&self.operator);
        }
        Ok(())
    }
    /// Generates exactly fifty local synthetic packet samples, each with
    /// known matched exchange/receive timestamps. No external feed is read.
    pub fn initialize_synthetic_stream(&mut self, quote: PaperQuote) -> Result<(), PaperError> {
        quote.validate()?;
        if self.quote.is_some() || quote.recv_ns <= 100_000_000 {
            return Err(PaperError::InvalidFlow);
        }
        for i in 0..50 {
            let receive = quote.recv_ns - (49 - i) * 1_000_000;
            self.gate
                .on_market_packet(receive - 5_000_000, receive)
                .map_err(|_| PaperError::InvalidFlow)?;
        }
        self.quote = Some(quote);
        if self.gate.snapshot().latency_open {
            return Err(PaperError::InvalidFlow);
        }
        Ok(())
    }
    pub fn arm(&mut self) -> Result<(), PaperError> {
        let valid = self.quote.is_some()
            && !self.gate.snapshot().latency_open
            && self.journal.summary().records == 0;
        self.interlock
            .arm(&self.operator, valid)
            .map_err(|_| PaperError::NotArmed)
    }
    fn current_quote(&self) -> Result<PaperQuote, PaperError> {
        self.quote.ok_or(PaperError::InvalidQuote)
    }
    /// The risk gate consumes one entry token. Only after its verdict is
    /// the proposed synthetic order durably reserved. Never send it anywhere.
    pub fn reserve_short(&mut self, intent: &OrderIntent) -> Result<FixedQty, PaperError> {
        if !self.interlock.entries_allowed() || self.journal.summary().unresolved_order().is_some()
        {
            return Err(PaperError::NotArmed);
        }
        let quote = self.current_quote()?;
        if intent.venue_id != VenueId::Equities as u8
            || intent.symbol_id != 0
            || intent.side != OrderSide::Sell as u8
            || intent.time_in_force != TimeInForce::Ioc as u8
        {
            return Err(PaperError::InvalidFlow);
        }
        let ctx = self.context(quote);
        let decision = self
            .gate
            .validate_entry(intent, &ctx, self.wallet(), &self.operator)
            .map_err(|_| PaperError::InvalidFlow)?;
        let amount = match decision {
            GateDecision::ProposedEntry { approved_qty, .. } => approved_qty,
            GateDecision::Rejected(reason) => return Err(PaperError::Risk(reason)),
            _ => return Err(PaperError::UnexpectedDecision),
        };
        self.journal.append(
            RecordKind::EntryReserve,
            intent.client_order_id,
            amount.0,
            intent.limit_price,
            0,
            self.journal.summary().confirmed_short_qty_raw,
        )?;
        Ok(amount)
    }
    pub fn reserve_cover(&mut self, intent: &OrderIntent) -> Result<FixedQty, PaperError> {
        if self.journal.summary().unresolved_order().is_some()
            || self.journal.summary().confirmed_short_qty_raw <= 0
        {
            return Err(PaperError::InvalidFlow);
        }
        let quote = self.current_quote()?;
        if intent.venue_id != VenueId::Equities as u8
            || intent.symbol_id != 0
            || intent.side != OrderSide::Buy as u8
            || !intent.is_reduce_only()
            || intent.time_in_force != TimeInForce::Ioc as u8
            || intent.client_order_id <= self.journal.summary().last_order_id
        {
            return Err(PaperError::InvalidFlow);
        }
        let ctx = self.context(quote);
        let amount = match self.gate.validate_cover(intent, &ctx) {
            GateDecision::ProposedCover { approved_qty } => approved_qty,
            GateDecision::Rejected(reason) => return Err(PaperError::Risk(reason)),
            _ => return Err(PaperError::UnexpectedDecision),
        };
        self.journal.append(
            RecordKind::CoverReserve,
            intent.client_order_id,
            amount.0,
            intent.limit_price,
            0,
            self.journal.summary().confirmed_short_qty_raw,
        )?;
        Ok(amount)
    }
    /// Explicit single-event simulator capacity is NEVER presumed full.
    /// The journal is fsynced *before* paper inventory or cash changes.
    pub fn simulate_fill(&mut self, id: u64, quantity: FixedQty) -> Result<(), PaperError> {
        let q = self.current_quote()?;
        let s = self.journal.summary();
        if s.unresolved_order() != Some(id)
            || quantity.0 <= 0
            || quantity.0 > s.pending_quantity_raw()
        {
            return Err(PaperError::InvalidFlow);
        }
        let is_cover = s.pending_is_cover();
        let (kind, px, capacity) = if is_cover {
            (RecordKind::CoverFill, q.best_ask.0, q.ask_quantity.0)
        } else {
            (RecordKind::EntryFill, q.best_bid.0, q.bid_quantity.0)
        };
        if quantity.0 > capacity {
            return Err(PaperError::NoFill);
        }
        let pnl = if is_cover {
            self.journal.cover_pnl(quantity.0, px)?
        } else {
            0
        };
        let next_pos = if is_cover {
            s.confirmed_short_qty_raw.checked_sub(quantity.0)
        } else {
            s.confirmed_short_qty_raw.checked_add(quantity.0)
        }
        .ok_or(PaperError::Arithmetic)?;
        self.journal
            .append(kind, id, quantity.0, px, pnl, next_pos)?;
        Ok(())
    }
    /// An IOC remainder cancellation is an explicit simulator event.
    /// Real exchange cancel ACKs require a different production adapter.
    pub fn confirm_paper_cancel(&mut self, id: u64) -> Result<(), PaperError> {
        let s = self.journal.summary();
        if s.unresolved_order() != Some(id) {
            return Err(PaperError::InvalidFlow);
        }
        self.journal.append(
            if s.pending_is_cover() {
                RecordKind::CoverCancel
            } else {
                RecordKind::EntryCancel
            },
            id,
            0,
            0,
            0,
            s.confirmed_short_qty_raw,
        )?;
        Ok(())
    }
    /// Stop new entries. A paper account must still resolve pending orders
    /// and cover confirmed shorts before calling the journal "closed".
    pub fn halt_entries(&mut self) {
        self.interlock.halt_entries(&self.operator);
    }
    pub fn seal_flat_session(&mut self) -> Result<(), PaperError> {
        let s = self.journal.summary();
        if s.unresolved_order().is_some() || s.confirmed_short_qty_raw != 0 {
            return Err(PaperError::InvalidFlow);
        }
        self.interlock.halt_entries(&self.operator);
        self.journal
            .append(RecordKind::EmergencyHalt, 0, 0, 0, 0, 0)?;
        Ok(())
    }
}
/// A deterministic supervised demonstration, not market performance.
/// The CLI calls this only when the user explicitly supplies a new path.
pub fn run_demo(path: &Path) -> Result<DemoReport, PaperError> {
    let mut p = OfflinePaperSession::create(path)?;
    let now = 1_000_000_000u64;
    let entry_quote = PaperQuote {
        seq: 1,
        exchange_ns: now - 5_000_000,
        recv_ns: now,
        best_bid: FixedPrice(100 * SCALE),
        best_ask: FixedPrice(100 * SCALE + SCALE / 10),
        bid_quantity: FixedQty(60_000_000),
        ask_quantity: FixedQty(SCALE),
    };
    p.initialize_synthetic_stream(entry_quote)?;
    p.arm()?;
    let entry = OrderIntent::new(
        1,
        now,
        FixedPrice(100 * SCALE),
        FixedQty(SCALE),
        Some(FixedPrice(101 * SCALE)),
        FixedUsd(5 * SCALE),
        0,
        5,
        VenueId::Equities,
        0,
        OrderSide::Sell,
        TimeInForce::Ioc,
        false,
    )
    .map_err(|_| PaperError::InvalidFlow)?;
    p.reserve_short(&entry)?;
    p.simulate_fill(1, FixedQty(60_000_000))?;
    p.confirm_paper_cancel(1)?;

    let cover_quote = PaperQuote {
        seq: 2,
        exchange_ns: now - 4_000_000 + 1_000_000,
        recv_ns: now + 1_000_000,
        best_bid: FixedPrice(99 * SCALE),
        best_ask: FixedPrice(99 * SCALE + SCALE / 10),
        bid_quantity: FixedQty(SCALE),
        ask_quantity: FixedQty(40_000_000),
    };
    p.observe_quote(cover_quote)?;
    let cover = OrderIntent::new(
        2,
        now + 1_000_000,
        FixedPrice(100 * SCALE),
        FixedQty(60_000_000),
        None,
        FixedUsd::ZERO,
        0,
        5,
        VenueId::Equities,
        0,
        OrderSide::Buy,
        TimeInForce::Ioc,
        true,
    )
    .map_err(|_| PaperError::InvalidFlow)?;
    p.reserve_cover(&cover)?;
    p.simulate_fill(2, FixedQty(40_000_000))?;
    p.observe_quote(PaperQuote {
        seq: 3,
        exchange_ns: now - 3_000_000 + 2_000_000,
        recv_ns: now + 2_000_000,
        ask_quantity: FixedQty(20_000_000),
        ..cover_quote
    })?;
    p.simulate_fill(2, FixedQty(20_000_000))?;
    p.seal_flat_session()?;
    let s = p.summary();
    Ok(DemoReport {
        entry_id: 1,
        cover_id: 2,
        entry_filled: FixedQty(60_000_000),
        cover_filled: FixedQty(60_000_000),
        realized_net_pnl: FixedUsd(s.realized_net_pnl_raw),
        end_wallet: s.wallet(),
        journal_events: s.records,
    })
}
