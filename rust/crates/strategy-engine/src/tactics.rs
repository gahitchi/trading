use trading_core_types::{FixedPrice, SCALE, TimeInForce};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QuoteError {
    InvalidBook,
    InvalidSignal,
    ArithmeticOverflow,
}
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct QuoteInputs {
    pub best_bid: FixedPrice,
    pub best_ask: FixedPrice,
    pub microprice: FixedPrice,
    pub microprice_divergence_bps: f64,
    pub isr_down: f64,
    pub book_imbalance: f64,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QuoteKind {
    IocSweep,
    PassivePostOnly,
}
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct QuoteDecision {
    pub kind: QuoteKind,
    pub time_in_force: TimeInForce,
    pub short_sell_limit: FixedPrice,
    pub urgency_bps: f64,
    pub spread_bps: f64,
}
/// Limit price heuristic for *short sells*. For IOC, bids below the best
/// bid permit at most 4bp of adverse execution in the absence of gaps. For
/// passive sells, pricing is clamped to at least the visible best ask. The
/// exchange's tick/lot filter, best-quote timestamp and maker/taker status
/// MUST be rechecked before any order routing.
///
/// This is NOT an optimized Avellaneda–Stoikov market making model or an
/// Almgren–Chriss impact optimizer; it is a transparent threshold heuristic.
pub fn choose_short_quote(q: QuoteInputs) -> Result<QuoteDecision, QuoteError> {
    if q.best_bid.0 <= 0
        || q.best_ask.0 <= q.best_bid.0
        || q.microprice.0 < q.best_bid.0
        || q.microprice.0 > q.best_ask.0
    {
        return Err(QuoteError::InvalidBook);
    }
    if !q.microprice_divergence_bps.is_finite()
        || !q.isr_down.is_finite()
        || q.isr_down < 0.0
        || !q.book_imbalance.is_finite()
        || !(-1.0..=1.0).contains(&q.book_imbalance)
    {
        return Err(QuoteError::InvalidSignal);
    }
    let mid = (q.best_bid.0 as f64 + q.best_ask.0 as f64) / 2.0;
    let spread_bps = (q.best_ask.0 - q.best_bid.0) as f64 / mid * 10_000.0;
    let urgency_bps = q.microprice_divergence_bps.abs() + 1.5 * (q.isr_down - 3.0).max(0.0);
    let urgent = urgency_bps > 1.5 * spread_bps;
    let (kind, time_in_force, raw_price) = if urgent {
        // Floor to a raw fixed-price quantum; venue tick rounding still needed.
        (
            QuoteKind::IocSweep,
            TimeInForce::Ioc,
            i128::from(q.best_bid.0) * 9_996 / 10_000,
        )
    } else {
        let spread = (q.best_ask.0 - q.best_bid.0) as f64;
        let micro = q.microprice.0 as f64;
        let res = micro + spread / 2.0 * (1.0 - q.book_imbalance.clamp(-0.8, 0.8));
        if !res.is_finite() || res >= i64::MAX as f64 {
            return Err(QuoteError::ArithmeticOverflow);
        }
        // Passive short placement must not cross the visible best bid.
        // A proposed sell price below the best ask is lifted to best ask.
        let target = (res.ceil() as i128).max(i128::from(q.best_ask.0));
        (QuoteKind::PassivePostOnly, TimeInForce::PostOnly, target)
    };
    if raw_price <= 0
        || raw_price > i128::from(i64::MAX)
        || raw_price < i128::from(SCALE) / 100_000_000
    {
        return Err(QuoteError::ArithmeticOverflow);
    }
    Ok(QuoteDecision {
        kind,
        time_in_force,
        short_sell_limit: FixedPrice(raw_price as i64),
        urgency_bps,
        spread_bps,
    })
}
