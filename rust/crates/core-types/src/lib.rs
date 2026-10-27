//! Bounded, explicitly scaled, allocation-free-on-use primitives for a trading engine.
//! All monetary and quantity values are integers in units of 10^-8.

use core::fmt;
use core::str::FromStr;
use core::sync::atomic::{AtomicU64, Ordering};
use crossbeam_channel::{Receiver, Sender, TryRecvError, TrySendError};

pub const SCALE: i64 = 100_000_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NumericError {
    Overflow,
    DivisionByZero,
    InvalidDecimal,
    TooPrecise,
}

impl fmt::Display for NumericError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for NumericError {}

fn narrow(value: i128) -> Result<i64, NumericError> {
    i64::try_from(value).map_err(|_| NumericError::Overflow)
}

/// Exact decimal parser: no floating point, no rounding, and at most eight
/// significant fractional digits. Additional zeroes are accepted.
fn parse_scaled(input: &str) -> Result<i64, NumericError> {
    let bytes = input.as_bytes();
    if bytes.is_empty() {
        return Err(NumericError::InvalidDecimal);
    }
    let (negative, digits) = match bytes[0] {
        b'-' => (true, &bytes[1..]),
        b'+' => (false, &bytes[1..]),
        _ => (false, bytes),
    };
    if digits.is_empty() {
        return Err(NumericError::InvalidDecimal);
    }
    let mut integral: i128 = 0;
    let mut fractional: i128 = 0;
    let mut fraction_count: u32 = 0;
    let mut dot_seen = false;
    let mut saw_digit = false;
    for &byte in digits {
        if byte == b'.' && !dot_seen {
            dot_seen = true;
            continue;
        }
        if !byte.is_ascii_digit() {
            return Err(NumericError::InvalidDecimal);
        }
        saw_digit = true;
        let d = i128::from(byte - b'0');
        if !dot_seen {
            integral = integral
                .checked_mul(10)
                .and_then(|x| x.checked_add(d))
                .ok_or(NumericError::Overflow)?;
        } else if fraction_count < 8 {
            fractional = fractional * 10 + d;
            fraction_count += 1;
        } else if d != 0 {
            return Err(NumericError::TooPrecise);
        }
    }
    if !saw_digit {
        return Err(NumericError::InvalidDecimal);
    }
    for _ in fraction_count..8 {
        fractional *= 10;
    }
    let magnitude = integral
        .checked_mul(i128::from(SCALE))
        .and_then(|x| x.checked_add(fractional))
        .ok_or(NumericError::Overflow)?;
    narrow(if negative { -magnitude } else { magnitude })
}

macro_rules! fixed_type {
    ($name:ident) => {
        #[derive(Default, Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
        #[repr(transparent)]
        pub struct $name(pub i64);
        impl $name {
            pub const ZERO: Self = Self(0);
            pub const fn from_raw(raw: i64) -> Self {
                Self(raw)
            }
            pub const fn raw(self) -> i64 {
                self.0
            }
            pub fn from_integer(whole: i64) -> Result<Self, NumericError> {
                whole
                    .checked_mul(SCALE)
                    .map(Self)
                    .ok_or(NumericError::Overflow)
            }
            pub fn checked_add(self, rhs: Self) -> Result<Self, NumericError> {
                self.0
                    .checked_add(rhs.0)
                    .map(Self)
                    .ok_or(NumericError::Overflow)
            }
            pub fn checked_sub(self, rhs: Self) -> Result<Self, NumericError> {
                self.0
                    .checked_sub(rhs.0)
                    .map(Self)
                    .ok_or(NumericError::Overflow)
            }
            pub fn checked_mul(self, rhs: Self) -> Result<Self, NumericError> {
                narrow(i128::from(self.0) * i128::from(rhs.0) / i128::from(SCALE)).map(Self)
            }
            pub fn checked_div(self, rhs: Self) -> Result<Self, NumericError> {
                if rhs.0 == 0 {
                    return Err(NumericError::DivisionByZero);
                }
                narrow(i128::from(self.0) * i128::from(SCALE) / i128::from(rhs.0)).map(Self)
            }
        }
        impl FromStr for $name {
            type Err = NumericError;
            fn from_str(s: &str) -> Result<Self, Self::Err> {
                parse_scaled(s).map(Self)
            }
        }
    };
}
fixed_type!(FixedPrice);
fixed_type!(FixedQty);
fixed_type!(FixedUsd);
// Scaled basis points per one US dollar of displayed notional; not money.
fixed_type!(FixedBpsPerUsd);

/// Price times base quantity, in quote-currency units. Currency conversion
/// must be performed separately when the quote currency is not USD.
pub fn checked_notional(price: FixedPrice, quantity: FixedQty) -> Result<FixedUsd, NumericError> {
    narrow(i128::from(price.0) * i128::from(quantity.0) / i128::from(SCALE)).map(FixedUsd)
}

#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VenueId {
    BinanceUsdM = 0,
    BybitLinear = 1,
    Equities = 2,
    Forex = 3,
    Commodities = 4,
}
impl TryFrom<u8> for VenueId {
    type Error = ();
    fn try_from(value: u8) -> Result<Self, Self::Error> {
        match value {
            0 => Ok(Self::BinanceUsdM),
            1 => Ok(Self::BybitLinear),
            2 => Ok(Self::Equities),
            3 => Ok(Self::Forex),
            4 => Ok(Self::Commodities),
            _ => Err(()),
        }
    }
}

#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MarketEventType {
    DepthUpdate = 0,
    AggTrade = 1,
    ForceOrderLiq = 2,
    MarkPriceFunding = 3,
}

pub const FLAG_BID_OR_BUYER_MAKER: u8 = 1;
pub const FLAG_END_OF_BATCH: u8 = 2;
pub const FLAG_SNAPSHOT: u8 = 4;

/// One normalized level/trade event. The canonical 64-byte layout leaves
/// exactly 11 bytes of padding (six 8-byte fields + five metadata bytes).
/// Exchange-specific `pu` and Bybit `seq` must be carried by batch metadata,
/// not silently discarded or overloaded in these fields.
#[repr(C, align(64))]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MarketEvent {
    pub exchange_ts_ns: u64,
    pub recv_ts_ns: u64,
    pub price: i64,
    pub qty: i64,
    pub seq_id_first: u64,
    pub seq_id_final: u64,
    pub symbol_id: u16,
    pub venue_id: u8,
    pub event_type: u8,
    pub flags: u8,
    pub _padding: [u8; 11],
}
impl MarketEvent {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        exchange_ts_ns: u64,
        recv_ts_ns: u64,
        price: FixedPrice,
        qty: FixedQty,
        first_id: u64,
        final_id: u64,
        symbol_id: u16,
        venue: VenueId,
        event_type: MarketEventType,
        flags: u8,
    ) -> Self {
        Self {
            exchange_ts_ns,
            recv_ts_ns,
            price: price.0,
            qty: qty.0,
            seq_id_first: first_id,
            seq_id_final: final_id,
            symbol_id,
            venue_id: venue as u8,
            event_type: event_type as u8,
            flags,
            _padding: [0; 11],
        }
    }
}

#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderSide {
    Buy = 0,
    Sell = 1,
}
#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimeInForce {
    Gtc = 0,
    Ioc = 1,
    Fok = 2,
    PostOnly = 3,
}
pub const ORDER_FLAG_REDUCE_ONLY: u8 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IntentError {
    ZeroOrderId,
    InvalidPrice,
    InvalidQuantity,
    ZeroSymbolBudget,
}

/// The side, TIF and flags must be checked at the routing trust boundary.
/// Explicit padding prevents accidental 128-byte alignment expansion.
#[repr(C, align(64))]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OrderIntent {
    pub client_order_id: u64,
    pub created_at_ns: u64,
    pub limit_price: i64,
    pub quantity: i64,
    pub stop_price: i64,
    pub risk_budget_usd: i64,
    pub symbol_id: u16,
    pub max_slippage_bps: u16,
    pub venue_id: u8,
    pub strategy_id: u8,
    pub side: u8,
    pub time_in_force: u8,
    pub flags: u8,
    pub _padding: [u8; 7],
}
impl OrderIntent {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        client_order_id: u64,
        created_at_ns: u64,
        limit_price: FixedPrice,
        quantity: FixedQty,
        stop_price: Option<FixedPrice>,
        risk_budget: FixedUsd,
        symbol_id: u16,
        max_slippage_bps: u16,
        venue: VenueId,
        strategy_id: u8,
        side: OrderSide,
        tif: TimeInForce,
        reduce_only: bool,
    ) -> Result<Self, IntentError> {
        if client_order_id == 0 {
            return Err(IntentError::ZeroOrderId);
        }
        if limit_price.0 <= 0 || stop_price.is_some_and(|p| p.0 <= 0) {
            return Err(IntentError::InvalidPrice);
        }
        if quantity.0 <= 0 {
            return Err(IntentError::InvalidQuantity);
        }
        if risk_budget.0 < 0 {
            return Err(IntentError::ZeroSymbolBudget);
        }
        Ok(Self {
            client_order_id,
            created_at_ns,
            limit_price: limit_price.0,
            quantity: quantity.0,
            stop_price: stop_price.map_or(0, |p| p.0),
            risk_budget_usd: risk_budget.0,
            symbol_id,
            max_slippage_bps,
            venue_id: venue as u8,
            strategy_id,
            side: side as u8,
            time_in_force: tif as u8,
            flags: if reduce_only {
                ORDER_FLAG_REDUCE_ONLY
            } else {
                0
            },
            _padding: [0; 7],
        })
    }
    pub const fn is_reduce_only(&self) -> bool {
        self.flags & ORDER_FLAG_REDUCE_ONLY != 0
    }
}

const _: [(); 64] = [(); core::mem::size_of::<MarketEvent>()];
const _: [(); 64] = [(); core::mem::align_of::<MarketEvent>()];
const _: [(); 64] = [(); core::mem::size_of::<OrderIntent>()];
const _: [(); 64] = [(); core::mem::align_of::<OrderIntent>()];

#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TradingMode {
    HaltAll = 0,
    ShortOnly = 1,
    ShortBiased = 2,
    BiDirectional = 3,
    LongOnly = 4,
    ManualArmedTrap = 5,
}
impl TryFrom<u8> for TradingMode {
    type Error = OperatorError;
    fn try_from(value: u8) -> Result<Self, Self::Error> {
        match value {
            0 => Ok(Self::HaltAll),
            1 => Ok(Self::ShortOnly),
            2 => Ok(Self::ShortBiased),
            3 => Ok(Self::BiDirectional),
            4 => Ok(Self::LongOnly),
            5 => Ok(Self::ManualArmedTrap),
            _ => Err(OperatorError::InvalidState),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OperatorError {
    InvalidState,
    InvalidField,
    Full,
    Disconnected,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OperatorSnapshot {
    pub mode: TradingMode,
    /// Lower four bits enable strategies A-D.
    pub strategy_enable_mask: u8,
    /// 10 is 1.0x, 30 is 3.0x. Effective cap: 0.1x..10.0x.
    pub max_leverage_tenths: u8,
    /// 50..200 denotes 0.50x..2.00x.
    pub aggressiveness_pct: u8,
    pub profit_lock_pct: u8,
    pub active_symbol_mask: u16,
    /// 24-bit Unix-seconds residue: no authentication or freshness proof.
    pub heartbeat_rolling_sec: u32,
}
impl Default for OperatorSnapshot {
    fn default() -> Self {
        Self {
            mode: TradingMode::ShortOnly,
            strategy_enable_mask: 0b1111,
            max_leverage_tenths: 30,
            aggressiveness_pct: 100,
            profit_lock_pct: 35,
            active_symbol_mask: 0x01ff,
            heartbeat_rolling_sec: 0,
        }
    }
}
impl OperatorSnapshot {
    pub fn validate(self) -> Result<(), OperatorError> {
        if self.strategy_enable_mask > 0x0f
            || !(1..=100).contains(&self.max_leverage_tenths)
            || !(50..=200).contains(&self.aggressiveness_pct)
            || self.profit_lock_pct > 100
            || self.active_symbol_mask > 0x1ff
            || self.heartbeat_rolling_sec > 0x00ff_ffff
        {
            return Err(OperatorError::InvalidField);
        }
        Ok(())
    }
    pub fn pack(self) -> Result<u64, OperatorError> {
        self.validate()?;
        Ok((self.mode as u64)
            | (u64::from(self.strategy_enable_mask) << 3)
            | (u64::from(self.max_leverage_tenths) << 7)
            | (u64::from(self.aggressiveness_pct) << 15)
            | (u64::from(self.profit_lock_pct) << 23)
            | (u64::from(self.active_symbol_mask) << 31)
            | (u64::from(self.heartbeat_rolling_sec) << 40))
    }
    pub fn unpack(raw: u64) -> Result<Self, OperatorError> {
        let snapshot = Self {
            mode: TradingMode::try_from((raw & 0b111) as u8)?,
            strategy_enable_mask: ((raw >> 3) & 0x0f) as u8,
            max_leverage_tenths: ((raw >> 7) & 0xff) as u8,
            aggressiveness_pct: ((raw >> 15) & 0xff) as u8,
            profit_lock_pct: ((raw >> 23) & 0xff) as u8,
            active_symbol_mask: ((raw >> 31) & 0x1ff) as u16,
            heartbeat_rolling_sec: ((raw >> 40) & 0x00ff_ffff) as u32,
        };
        snapshot.validate()?;
        Ok(snapshot)
    }
    /// A stale wrapped 24-bit seconds value is not an authorization signal.
    pub fn heartbeat_age_seconds(self, now_unix_seconds: u64) -> u32 {
        ((now_unix_seconds as u32).wrapping_sub(self.heartbeat_rolling_sec)) & 0x00ff_ffff
    }
}

pub struct AtomicOperatorState(AtomicU64);
impl AtomicOperatorState {
    pub fn new(initial: OperatorSnapshot) -> Result<Self, OperatorError> {
        Ok(Self(AtomicU64::new(initial.pack()?)))
    }
    pub fn load(&self) -> Result<OperatorSnapshot, OperatorError> {
        OperatorSnapshot::unpack(self.0.load(Ordering::Acquire))
    }
    pub fn store(&self, updated: OperatorSnapshot) -> Result<(), OperatorError> {
        self.0.store(updated.pack()?, Ordering::Release);
        Ok(())
    }
    /// CAS preserves unrelated operator updates instead of overwriting them.
    pub fn update(
        &self,
        transform: impl Fn(OperatorSnapshot) -> Result<OperatorSnapshot, OperatorError>,
    ) -> Result<OperatorSnapshot, OperatorError> {
        let mut previous = self.0.load(Ordering::Acquire);
        loop {
            let next = transform(OperatorSnapshot::unpack(previous)?)?;
            let packed = next.pack()?;
            match self.0.compare_exchange_weak(
                previous,
                packed,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => return Ok(next),
                Err(observed) => previous = observed,
            }
        }
    }
    pub fn halt(&self) -> Result<(), OperatorError> {
        self.update(|mut state| {
            state.mode = TradingMode::HaltAll;
            Ok(state)
        })?;
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OperatorCommand {
    FireManualShortSweep {
        symbol_id: u16,
        risk_fraction_bps: u16,
        max_slippage_bps: u16,
    },
    FireManualLongSweep {
        symbol_id: u16,
        risk_fraction_bps: u16,
        max_slippage_bps: u16,
    },
    ArmBreakdownTrap {
        symbol_id: u16,
        trigger_price: i64,
        is_short: bool,
        risk_fraction_bps: u16,
    },
    CashOutPositionPct {
        symbol_id: u16,
        pct_bps: u16,
    },
    SweepActiveProfitToVault {
        pct_bps: u16,
    },
    PanicFlattenAll,
}
impl OperatorCommand {
    pub fn validate(self) -> Result<(), OperatorError> {
        match self {
            Self::FireManualShortSweep {
                risk_fraction_bps,
                max_slippage_bps,
                ..
            }
            | Self::FireManualLongSweep {
                risk_fraction_bps,
                max_slippage_bps,
                ..
            } => {
                if risk_fraction_bps == 0 || risk_fraction_bps > 10_000 || max_slippage_bps > 10_000
                {
                    return Err(OperatorError::InvalidField);
                }
            }
            Self::ArmBreakdownTrap {
                trigger_price,
                risk_fraction_bps,
                ..
            } => {
                if trigger_price <= 0 || risk_fraction_bps == 0 || risk_fraction_bps > 10_000 {
                    return Err(OperatorError::InvalidField);
                }
            }
            Self::CashOutPositionPct { pct_bps, .. }
            | Self::SweepActiveProfitToVault { pct_bps } => {
                if pct_bps == 0 || pct_bps > 10_000 {
                    return Err(OperatorError::InvalidField);
                }
            }
            Self::PanicFlattenAll => (),
        }
        Ok(())
    }
}

/// Ownership of separate endpoints makes the intended single producer and
/// single consumer explicit. Crossbeam bounded queues are internally MPMC;
/// the endpoint API does not expose cloning either handle.
pub struct OperatorCommandTx(Sender<OperatorCommand>);
pub struct OperatorCommandRx(Receiver<OperatorCommand>);
pub fn operator_command_channel(
    capacity: usize,
) -> Result<(OperatorCommandTx, OperatorCommandRx), OperatorError> {
    if capacity == 0 {
        return Err(OperatorError::InvalidField);
    }
    let (tx, rx) = crossbeam_channel::bounded(capacity);
    Ok((OperatorCommandTx(tx), OperatorCommandRx(rx)))
}
impl OperatorCommandTx {
    pub fn try_send(&self, cmd: OperatorCommand) -> Result<(), OperatorError> {
        cmd.validate()?;
        self.0.try_send(cmd).map_err(|err| match err {
            TrySendError::Full(_) => OperatorError::Full,
            TrySendError::Disconnected(_) => OperatorError::Disconnected,
        })
    }
}
impl OperatorCommandRx {
    pub fn try_recv(&self) -> Result<Option<OperatorCommand>, OperatorError> {
        match self.0.try_recv() {
            Ok(cmd) => Ok(Some(cmd)),
            Err(TryRecvError::Empty) => Ok(None),
            Err(TryRecvError::Disconnected) => Err(OperatorError::Disconnected),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ComplianceReason {
    WrongVenue,
    NoLocate,
    InvalidReference,
    Blackout,
    UnsupportedOrder,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ComplianceDecision {
    Allow,
    /// Caller MUST re-run execution/tick/marketability validation after repricing.
    Reprice {
        minimum_limit_price: i64,
    },
    Block(ComplianceReason),
}

/// Trust boundary input, not inferred from stale market quotes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ComplianceContext {
    pub national_best_bid: i64,
    pub tick_size: i64,
    pub short_sale_restriction_active: bool,
    pub located_for_open_short: bool,
    pub trading_blackout: bool,
}

pub trait VenueComplianceTrait: Send + Sync {
    fn evaluate(&self, order: &OrderIntent, ctx: &ComplianceContext) -> ComplianceDecision;
}

pub struct EquitiesCompliance;
impl VenueComplianceTrait for EquitiesCompliance {
    fn evaluate(&self, order: &OrderIntent, ctx: &ComplianceContext) -> ComplianceDecision {
        if order.venue_id != VenueId::Equities as u8 {
            return ComplianceDecision::Block(ComplianceReason::WrongVenue);
        }
        if ctx.trading_blackout {
            return ComplianceDecision::Block(ComplianceReason::Blackout);
        }
        if order.side != OrderSide::Sell as u8 || order.is_reduce_only() {
            return ComplianceDecision::Allow;
        }
        if !ctx.located_for_open_short {
            return ComplianceDecision::Block(ComplianceReason::NoLocate);
        }
        if ctx.short_sale_restriction_active {
            if ctx.national_best_bid <= 0 || ctx.tick_size <= 0 {
                return ComplianceDecision::Block(ComplianceReason::InvalidReference);
            }
            let Some(min_price) = ctx.national_best_bid.checked_add(ctx.tick_size) else {
                return ComplianceDecision::Block(ComplianceReason::InvalidReference);
            };
            if order.limit_price < min_price {
                if order.time_in_force == TimeInForce::Ioc as u8
                    || order.time_in_force == TimeInForce::Fok as u8
                {
                    return ComplianceDecision::Block(ComplianceReason::UnsupportedOrder);
                }
                return ComplianceDecision::Reprice {
                    minimum_limit_price: min_price,
                };
            }
        }
        ComplianceDecision::Allow
    }
}

pub struct ForexCommoditiesCompliance;
impl VenueComplianceTrait for ForexCommoditiesCompliance {
    fn evaluate(&self, order: &OrderIntent, ctx: &ComplianceContext) -> ComplianceDecision {
        if order.venue_id != VenueId::Forex as u8 && order.venue_id != VenueId::Commodities as u8 {
            return ComplianceDecision::Block(ComplianceReason::WrongVenue);
        }
        if ctx.trading_blackout {
            ComplianceDecision::Block(ComplianceReason::Blackout)
        } else {
            ComplianceDecision::Allow
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::mem::{align_of, size_of};

    #[test]
    fn cache_lines_are_exactly_sixty_four_bytes() {
        assert_eq!(
            (size_of::<MarketEvent>(), align_of::<MarketEvent>()),
            (64, 64)
        );
        assert_eq!(
            (size_of::<OrderIntent>(), align_of::<OrderIntent>()),
            (64, 64)
        );
    }
    #[test]
    fn decimal_parsing_is_exact_and_rejects_precision_loss() {
        assert_eq!("-1.23".parse::<FixedUsd>().unwrap().raw(), -123_000_000);
        assert_eq!("0.0000000100".parse::<FixedPrice>().unwrap().raw(), 1);
        assert_eq!(
            "0.000000001".parse::<FixedPrice>(),
            Err(NumericError::TooPrecise)
        );
        assert_eq!(
            "1e2".parse::<FixedPrice>(),
            Err(NumericError::InvalidDecimal)
        );
        assert_eq!("".parse::<FixedQty>(), Err(NumericError::InvalidDecimal));
    }
    #[test]
    fn checked_fixed_arithmetic_catches_overflow_and_div_by_zero() {
        let a: FixedUsd = "10.25".parse().unwrap();
        let b: FixedUsd = "2".parse().unwrap();
        assert_eq!(a.checked_mul(b).unwrap(), "20.5".parse().unwrap());
        assert_eq!(a.checked_div(b).unwrap(), "5.125".parse().unwrap());
        assert_eq!(
            a.checked_div(FixedUsd::ZERO),
            Err(NumericError::DivisionByZero)
        );
        assert_eq!(
            FixedUsd(i64::MAX).checked_add(FixedUsd(1)),
            Err(NumericError::Overflow)
        );
        assert_eq!(
            checked_notional("100".parse().unwrap(), "0.2".parse().unwrap()).unwrap(),
            "20".parse().unwrap()
        );
    }
    #[test]
    fn packed_operator_register_round_trip_and_rejects_invalid_fields() {
        let state = OperatorSnapshot::default();
        assert_eq!(OperatorSnapshot::unpack(state.pack().unwrap()), Ok(state));
        let mut bad = state;
        bad.active_symbol_mask = 0x200;
        assert_eq!(bad.pack(), Err(OperatorError::InvalidField));
        assert_eq!(
            OperatorSnapshot::unpack(7),
            Err(OperatorError::InvalidState)
        );
        let reg = AtomicOperatorState::new(state).unwrap();
        reg.update(|mut snapshot| {
            snapshot.profit_lock_pct = 70;
            Ok(snapshot)
        })
        .unwrap();
        reg.halt().unwrap();
        assert_eq!(reg.load().unwrap().mode, TradingMode::HaltAll);
        assert_eq!(reg.load().unwrap().profit_lock_pct, 70);
    }
    #[test]
    fn operator_channel_is_bounded_and_commands_validated() {
        let (tx, rx) = operator_command_channel(1).unwrap();
        assert_eq!(rx.try_recv(), Ok(None));
        assert_eq!(
            tx.try_send(OperatorCommand::CashOutPositionPct {
                symbol_id: 0,
                pct_bps: 0
            }),
            Err(OperatorError::InvalidField)
        );
        tx.try_send(OperatorCommand::PanicFlattenAll).unwrap();
        assert_eq!(
            tx.try_send(OperatorCommand::PanicFlattenAll),
            Err(OperatorError::Full)
        );
        assert_eq!(rx.try_recv(), Ok(Some(OperatorCommand::PanicFlattenAll)));
    }
    fn short_equity_order(tif: TimeInForce) -> OrderIntent {
        OrderIntent::new(
            1,
            42,
            "100".parse().unwrap(),
            "1".parse().unwrap(),
            None,
            FixedUsd::ZERO,
            4,
            5,
            VenueId::Equities,
            0,
            OrderSide::Sell,
            tif,
            false,
        )
        .unwrap()
    }
    #[test]
    fn equities_rule_201_and_locate_fail_closed() {
        let mut ctx = ComplianceContext {
            national_best_bid: 100 * SCALE,
            tick_size: SCALE / 100,
            short_sale_restriction_active: true,
            located_for_open_short: false,
            trading_blackout: false,
        };
        assert_eq!(
            EquitiesCompliance.evaluate(&short_equity_order(TimeInForce::Gtc), &ctx),
            ComplianceDecision::Block(ComplianceReason::NoLocate)
        );
        ctx.located_for_open_short = true;
        assert_eq!(
            EquitiesCompliance.evaluate(&short_equity_order(TimeInForce::Gtc), &ctx),
            ComplianceDecision::Reprice {
                minimum_limit_price: 100 * SCALE + SCALE / 100
            }
        );
        assert_eq!(
            EquitiesCompliance.evaluate(&short_equity_order(TimeInForce::Ioc), &ctx),
            ComplianceDecision::Block(ComplianceReason::UnsupportedOrder)
        );
    }
}

pub mod live;
pub use live::{
    ExecutionReportEvent, ExecutionStatus, InstrumentError, InstrumentSpec, TickRounding,
};
