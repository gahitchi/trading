//! Independent, integer-only, fail-closed pre-trade research risk engine.
//! The gate is NOT an exchange gateway. Positive verdicts are *preliminary*
//! and do not authorize trading without an atomic, durable reservation and
//! a broker-reconciled execution adapter.
mod gate;
mod latency;
mod limiter;
mod policy;
mod types;

pub use gate::RiskGate;
pub use latency::{LatencyCircuit, LatencyState};
pub use limiter::TokenBucket;
pub use types::{
    AccountEvidence, GateDecision, MarketEvidence, RiskContext, RiskDirective,
    RiskError, RiskPolicy, RiskReason, SafetySnapshot,
};
