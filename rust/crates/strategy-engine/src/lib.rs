//! Research strategy triggers, deterministic short-trade lifecycle and order
//! acknowledgement state; these structures NEVER submit exchange orders.
//!
//! Signal quality, synchronized venue books, timestamp provenance and broker
//! reconciliation must be certified by an independent risk-gate before routing.
//! The continuation score and quoting heuristics are NOT solutions to HJB,
//! Snell-envelope, Avellaneda–Stoikov or Almgren–Chriss optimization problems.
mod entries;
mod lifecycle;
mod orders;
mod tactics;

pub use entries::{EntryCandidate, EntryEngine, EntrySnapshot, ManualTrap, StrategyError};
pub use lifecycle::{
    ExitProposal, ExitReason, ExitReportKind, LifecycleError, LifecyclePhase, LifecycleSignals,
    ShortTradeLifecycle, TradeTick, continuation_proxy,
};
pub use orders::{OrderEvent, OrderEventKind, OrderStatus, OrderTracker, OrderTrackerError};
pub use tactics::{QuoteDecision, QuoteError, QuoteInputs, QuoteKind, choose_short_quote};
