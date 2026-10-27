use crate::latency::LatencyCircuit;
use crate::limiter::TokenBucket;
use crate::policy::venue_entry_policy;
use crate::types::{
    GateDecision, RiskContext, RiskDirective, RiskError, RiskPolicy, RiskReason, SafetySnapshot,
};
use trading_core_types::{
    AtomicOperatorState, FixedQty, FixedUsd, OrderIntent, OrderSide, SCALE, TradingMode, VenueId,
};
use trading_wallet_manager::WalletSnapshot;

const BPS: i64 = SCALE;

pub struct RiskGate {
    policy: RiskPolicy,
    latency: LatencyCircuit,
    rate: TokenBucket,
    last_approved_id: u64,
    halted: bool,
}
impl RiskGate {
    pub fn new(policy: RiskPolicy) -> Result<Self, RiskError> {
        policy.validate()?;
        Ok(Self {
            policy,
            latency: LatencyCircuit::new(),
            rate: TokenBucket::new(policy.burst_capacity, policy.max_orders_per_second)?,
            last_approved_id: 0,
            halted: false,
        })
    }
    pub fn snapshot(&self) -> SafetySnapshot {
        SafetySnapshot {
            latency_open: self.latency.state().blocked,
            latency_recovery_streak: self.latency.state().recovery_streak,
            halted: self.halted,
            last_approved_order_id: self.last_approved_id,
        }
    }
    /// The ingestion adapter must normalize both timestamps against a
    /// verified common clock. A bad sample cancels entry eligibility. The
    /// caller must execute any emitted directive; this crate has no I/O.
    pub fn on_market_packet(
        &mut self,
        exchange_ts_ns: u64,
        receive_ts_ns: u64,
    ) -> Result<RiskDirective, RiskError> {
        let state = self.latency.observe(exchange_ts_ns, receive_ts_ns)?;
        Ok(RiskDirective {
            cancel_resting_entries: state.blocked,
            ..RiskDirective::default()
        })
    }
    pub fn mark_connection_lost(&mut self) -> RiskDirective {
        self.latency.trip();
        RiskDirective {
            cancel_resting_entries: true,
            ..RiskDirective::default()
        }
    }
    /// Must be evaluated independently of strategy entry decisions, including
    /// while no order is pending. Detects CPPI floor and in-position squeeze.
    /// The *in-memory* operator mode latches HaltAll. External flatten and
    /// vault segregation require the future supervised execution gateway.
    pub fn monitor(
        &mut self,
        ctx: &RiskContext,
        wallet: WalletSnapshot,
        operator: &AtomicOperatorState,
    ) -> Result<RiskDirective, RiskError> {
        if wallet.active_equity.0 < 0 || wallet.session_floor.0 < 0 || wallet.total_equity.0 < 0 {
            return Err(RiskError::InvalidEvidence);
        }
        if wallet.halt_required || wallet.active_equity <= wallet.session_floor {
            operator.halt().map_err(|_| RiskError::InvalidEvidence)?;
            self.halted = true;
            return Ok(RiskDirective {
                cancel_resting_entries: true,
                require_flatten_all: true,
                seal_profit_vault: true,
                halt_all: true,
                ..RiskDirective::default()
            });
        }
        let m = ctx.market;
        let observed_is_fresh = m.certified_event_provenance
            && m.synchronized
            && m.quote_received_ns > 0
            && m.now_ns >= m.quote_received_ns
            && m.now_ns - m.quote_received_ns <= self.policy.max_quote_age_ns;
        let in_short = ctx.account.reconciled && ctx.account.confirmed_short_qty.0 > 0;
        if in_short
            && observed_is_fresh
            && m.isr_up_scaled > 5 * SCALE
            && m.ofi_z_scaled > 25 * SCALE / 10
        {
            operator.halt().map_err(|_| RiskError::InvalidEvidence)?;
            self.halted = true;
            return Ok(RiskDirective {
                cancel_resting_entries: true,
                require_reduce_only_short_cover: true,
                halt_all: true,
                ..RiskDirective::default()
            });
        }
        if !ctx.account.reconciled
            || !ctx.account.reservations_current
            || self.latency.state().blocked
            || !observed_is_fresh
            || !self.latency_sample_fresh(m.now_ns)
        {
            return Ok(RiskDirective {
                cancel_resting_entries: true,
                ..RiskDirective::default()
            });
        }
        Ok(RiskDirective::default())
    }

    fn latency_sample_fresh(&self, now_ns: u64) -> bool {
        self.latency.last_receive_ns().is_some_and(|received| {
            received <= now_ns && now_ns - received <= self.policy.max_quote_age_ns
        })
    }

    fn common_checks(&self, intent: &OrderIntent, ctx: &RiskContext) -> Result<(), RiskReason> {
        let m = ctx.market;
        if !ctx.paper_environment {
            return Err(RiskReason::InvalidPolicy);
        }
        if intent.client_order_id == 0
            || intent.created_at_ns == 0
            || intent.created_at_ns > m.now_ns
            || m.now_ns - intent.created_at_ns > self.policy.max_quote_age_ns
            || intent.time_in_force > 3
            || intent.max_slippage_bps > 10_000
            || m.isr_up_scaled < 0
            || intent.limit_price <= 0
            || intent.quantity <= 0
            || intent.symbol_id >= 9
            || intent.risk_budget_usd < 0
            || intent.flags & !1 != 0
            || intent.venue_id != m.venue as u8
            || intent.symbol_id != m.symbol_id
            || VenueId::try_from(intent.venue_id).is_err()
        {
            return Err(RiskReason::BadOrder);
        }
        if !m.synchronized {
            return Err(RiskReason::UnsynchronizedFeed);
        }
        if !m.certified_event_provenance {
            return Err(RiskReason::UncertifiedSource);
        }
        if m.quote_received_ns == 0
            || m.now_ns < m.quote_received_ns
            || m.now_ns - m.quote_received_ns > self.policy.max_quote_age_ns
        {
            return Err(RiskReason::StaleQuote);
        }
        if !ctx.account.reconciled {
            return Err(RiskReason::AccountUnreconciled);
        }
        Ok(())
    }

    /// A reduce-only BUY proposal can be examined during a stop/halt.
    /// It never spends entry tokens, never relies on reported strategy alpha,
    /// and never changes the short inventory before an authoritative fill.
    pub fn validate_cover(&self, intent: &OrderIntent, ctx: &RiskContext) -> GateDecision {
        if let Err(reason) = self.common_checks(intent, ctx) {
            return GateDecision::Rejected(reason);
        }
        if !intent.is_reduce_only() || intent.side != OrderSide::Buy as u8 {
            return GateDecision::Rejected(RiskReason::InvalidOrderType);
        }
        if ctx.account.confirmed_short_qty.0 <= 0
            || intent.quantity > ctx.account.confirmed_short_qty.0
        {
            return GateDecision::Rejected(RiskReason::NoConfirmedShort);
        }
        // The independent gateway must reserve *all* outstanding covers to
        // prevent two individually valid close orders from double-covering.
        if !ctx.account.reservations_current {
            return GateDecision::Rejected(RiskReason::MissingReservations);
        }
        GateDecision::ProposedCover {
            approved_qty: FixedQty(intent.quantity),
        }
    }

    /// Returns a proposal, NOT permission to send. In particular a proof flag
    /// in RiskContext cannot substitute for the missing durable account and
    /// reservation architecture. The execution gateway must validate again.
    pub fn validate_entry(
        &mut self,
        intent: &OrderIntent,
        ctx: &RiskContext,
        wallet: WalletSnapshot,
        operator: &AtomicOperatorState,
    ) -> Result<GateDecision, RiskError> {
        let emergency = self.monitor(ctx, wallet, operator)?;
        if emergency.halt_all || self.halted {
            return Ok(GateDecision::Rejected(RiskReason::DrawdownFloor));
        }
        if let Err(reason) = self.common_checks(intent, ctx) {
            return Ok(GateDecision::Rejected(reason));
        }
        if !ctx.account.reservations_current {
            return Ok(GateDecision::Rejected(RiskReason::MissingReservations));
        }
        if self.latency.state().blocked || !self.latency_sample_fresh(ctx.market.now_ns) {
            return Ok(GateDecision::Rejected(RiskReason::LatencyCircuitOpen));
        }
        if intent.is_reduce_only()
            || intent.side != OrderSide::Sell as u8
            || intent.stop_price <= intent.limit_price
            || intent.risk_budget_usd <= 0
        {
            return Ok(GateDecision::Rejected(RiskReason::InvalidOrderType));
        }
        if intent.client_order_id <= self.last_approved_id {
            return Ok(GateDecision::Rejected(RiskReason::ReplayOrderId));
        }
        let op = operator.load().map_err(|_| RiskError::InvalidEvidence)?;
        if op.mode == TradingMode::HaltAll {
            return Ok(GateDecision::Rejected(RiskReason::OperatorHalted));
        }
        if op.active_symbol_mask & (1 << intent.symbol_id) == 0 {
            return Ok(GateDecision::Rejected(RiskReason::SymbolDisabled));
        }
        if intent.strategy_id > 3 {
            return Ok(GateDecision::Rejected(RiskReason::InvalidOrderType));
        }
        if op.strategy_enable_mask & (1 << intent.strategy_id) == 0 {
            return Ok(GateDecision::Rejected(RiskReason::StrategyDisabled));
        }
        let mode_compatible = match op.mode {
            TradingMode::ShortOnly | TradingMode::ShortBiased | TradingMode::BiDirectional => {
                intent.strategy_id != 3
            }
            TradingMode::ManualArmedTrap => intent.strategy_id == 3,
            _ => false,
        };
        if !mode_compatible {
            return Ok(GateDecision::Rejected(RiskReason::WrongMode));
        }
        if let Err(reason) = venue_entry_policy(intent, ctx) {
            return Ok(GateDecision::Rejected(reason));
        }
        let m = ctx.market;
        let crypto = matches!(m.venue, VenueId::BinanceUsdM | VenueId::BybitLinear);
        let mut approved_qty = intent.quantity;
        if crypto {
            if m.funding_asof_ns == 0
                || m.basis_asof_ns == 0
                || m.now_ns < m.funding_asof_ns
                || m.now_ns < m.basis_asof_ns
                || m.now_ns - m.funding_asof_ns > self.policy.max_funding_age_ns
                || m.now_ns - m.basis_asof_ns > self.policy.max_basis_age_ns
            {
                return Ok(GateDecision::Rejected(RiskReason::StaleFundingBasis));
            }
            if m.funding_8h_bps_scaled < -3 * BPS && m.perp_index_basis_bps_scaled < -15 * BPS {
                if intent.strategy_id == 0 || intent.strategy_id == 2 {
                    return Ok(GateDecision::Rejected(RiskReason::FundingBasisSqueeze));
                }
                if intent.strategy_id == 1 {
                    approved_qty /= 4;
                    if approved_qty <= 0 {
                        return Ok(GateDecision::Rejected(RiskReason::FundingBasisSqueeze));
                    }
                }
            }
        }
        if ctx.account.maintenance_margin_headroom_usd.0 <= 0
            || ctx.account.gross_open_and_pending_usd.0 < 0
            || wallet.active_equity.0 <= 0
        {
            return Ok(GateDecision::Rejected(RiskReason::InsufficientCollateral));
        }
        // Wallet's planned stop budget is checked using integer i128.
        // This is only a loss *estimate*, not a maximum loss guarantee.
        let stop_distance = i128::from(intent.stop_price - intent.limit_price);
        let modeled_loss = stop_distance * i128::from(approved_qty) / i128::from(SCALE);
        if modeled_loss <= 0
            || modeled_loss > i128::from(intent.risk_budget_usd)
            || modeled_loss > i128::from(ctx.account.maintenance_margin_headroom_usd.0)
        {
            return Ok(GateDecision::Rejected(RiskReason::InsufficientCollateral));
        }
        let notional = i128::from(intent.limit_price)
            .checked_mul(i128::from(approved_qty))
            .ok_or(RiskError::NumericOverflow)?
            / i128::from(SCALE);
        let max_leverage = op
            .max_leverage_tenths
            .min(self.policy.absolute_max_gross_leverage_tenths);
        let exposure_limit = i128::from(wallet.active_equity.0) * i128::from(max_leverage) / 10;
        if notional <= 0
            || notional > i128::from(i64::MAX)
            || i128::from(ctx.account.gross_open_and_pending_usd.0) + notional > exposure_limit
        {
            return Ok(GateDecision::Rejected(RiskReason::ExceedsGrossLimit));
        }
        if !self.rate.take(m.now_ns)? {
            return Ok(GateDecision::Rejected(RiskReason::RateLimit));
        }
        self.last_approved_id = intent.client_order_id;
        Ok(GateDecision::ProposedEntry {
            approved_qty: FixedQty(approved_qty),
            approved_notional: FixedUsd(notional as i64),
        })
    }
}
