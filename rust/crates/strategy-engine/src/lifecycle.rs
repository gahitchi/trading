mod engine;
mod reports;
mod types;

pub use engine::{ShortTradeLifecycle, continuation_proxy};
pub use types::{
    ExitProposal, ExitReason, ExitReportKind, LifecycleError, LifecyclePhase, LifecycleSignals,
    TradeTick,
};
