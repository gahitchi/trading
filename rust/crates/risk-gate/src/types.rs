use trading_core_types::{ComplianceContext, FixedQty, FixedUsd, VenueId};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RiskError {
    InvalidConfiguration,
    InvalidEvidence,
    NumericOverflow,
    ClockReversal,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RiskReason {
    OperatorHalted,
    StrategyDisabled,
    SymbolDisabled,
    WrongMode,
    UncertifiedSource,
    UnsynchronizedFeed,
    StaleQuote,
    StaleFundingBasis,
    BadOrder,
    InvalidOrderType,
    ComplianceBlackout,
    ShortLocateMissing,
    ShortSalePriceRestricted,
    FundingBasisSqueeze,
    LatencyCircuitOpen,
    RateLimit,
    DrawdownFloor,
    AccountUnreconciled,
    MissingReservations,
    ExceedsGrossLimit,
    InsufficientCollateral,
    NoConfirmedShort,
    SqueezeFlattenRequired,
    InvalidPolicy,
    ReplayOrderId,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GateDecision {
    /// A preliminary *proposal* only: an independent atomic reservation and
    /// durable paper-execution gateway are required before it is executable.
    ProposedEntry {
        approved_qty: FixedQty,
        approved_notional: FixedUsd,
    },
    /// Buy-to-cover the reconciled short, strictly reduce-only.
    ProposedCover {
        approved_qty: FixedQty,
    },
    Rejected(RiskReason),
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct RiskDirective {
    pub cancel_resting_entries: bool,
    pub require_flatten_all: bool,
    pub require_reduce_only_short_cover: bool,
    pub seal_profit_vault: bool,
    pub halt_all: bool,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RiskPolicy {
    pub max_quote_age_ns: u64,
    pub max_funding_age_ns: u64,
    pub max_basis_age_ns: u64,
    pub max_orders_per_second: u32,
    pub burst_capacity: u32,
    /// Absolute gross exposure cap, in multiples of *active* equity, times 10.
    pub absolute_max_gross_leverage_tenths: u8,
}
impl Default for RiskPolicy {
    fn default() -> Self {
        Self {
            max_quote_age_ns: 50_000_000,
            max_funding_age_ns: 3_000_000_000,
            max_basis_age_ns: 500_000_000,
            max_orders_per_second: 10,
            burst_capacity: 20,
            absolute_max_gross_leverage_tenths: 30,
        }
    }
}
impl RiskPolicy {
    pub fn validate(self) -> Result<(), RiskError> {
        if self.max_quote_age_ns == 0
            || self.max_quote_age_ns > 150_000_000
            || self.max_funding_age_ns == 0
            || self.max_basis_age_ns == 0
            || self.max_orders_per_second == 0
            || self.burst_capacity == 0
            || !(1..=100).contains(&self.absolute_max_gross_leverage_tenths)
        {
            return Err(RiskError::InvalidConfiguration);
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MarketEvidence {
    /// Supplied by a qualified monotonic/exchange clock normalization adapter.
    pub now_ns: u64,
    pub quote_received_ns: u64,
    pub synchronized: bool,
    pub certified_event_provenance: bool,
    pub venue: VenueId,
    pub symbol_id: u16,
    /// 1e8-scaled basis points, *not* a percentage or raw funding fraction.
    pub funding_8h_bps_scaled: i64,
    pub perp_index_basis_bps_scaled: i64,
    pub funding_asof_ns: u64,
    pub basis_asof_ns: u64,
    /// 1e8-scaled upward ISR and signed OFI Z.
    pub isr_up_scaled: i64,
    pub ofi_z_scaled: i64,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AccountEvidence {
    /// A proof flag from independently reconciled broker positions and cash.
    pub reconciled: bool,
    /// A proof flag from a durable atomic open+pending reservation system.
    pub reservations_current: bool,
    pub gross_open_and_pending_usd: FixedUsd,
    /// Nonnegative *absolute* confirmed short base quantity.
    pub confirmed_short_qty: FixedQty,
    pub maintenance_margin_headroom_usd: FixedUsd,
    /// Forex / commodity hard blackout condition.
    pub rollover_or_policy_blackout: bool,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RiskContext {
    pub market: MarketEvidence,
    pub account: AccountEvidence,
    pub equities: ComplianceContext,
    /// True ONLY for an isolated paper-only broker adapter. This is NOT
    /// an account-level entitlement or credential.
    pub paper_environment: bool,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SafetySnapshot {
    pub latency_open: bool,
    pub latency_recovery_streak: u8,
    pub halted: bool,
    pub last_approved_order_id: u64,
}
