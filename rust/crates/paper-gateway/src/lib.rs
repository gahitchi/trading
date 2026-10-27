//! Part 6: supervised, self-contained **offline** paper execution laboratory.
//! There is no exchange client, network socket, credential import, or live route.
//! Durable events are fsynced before paper-account state changes are applied.
//! Restart is inspect-only: a new journal is required to begin another session.
mod journal;
mod paper;
mod supervision;

pub use journal::{Journal, JournalError, Record, RecordKind, ReplaySummary};
pub use paper::{DemoReport, OfflinePaperSession, PaperError, PaperQuote, run_demo};
pub use supervision::{PaperStatus, SafetyInterlock};
