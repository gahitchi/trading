use trading_core_types::{AtomicOperatorState, OperatorSnapshot, TradingMode};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PaperStatus {
    Disarmed,
    Armed,
    EntryHalted,
}
/// An explicit local one-time operator interlock. Not an identity verifier.
/// No external paper or live broker may be attached to this engine.
pub struct SafetyInterlock {
    state: PaperStatus,
}
impl Default for SafetyInterlock {
    fn default() -> Self {
        Self {
            state: PaperStatus::Disarmed,
        }
    }
}
impl SafetyInterlock {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn status(&self) -> PaperStatus {
        self.state
    }
    pub fn arm(
        &mut self,
        operator: &AtomicOperatorState,
        valid_stream: bool,
    ) -> Result<(), &'static str> {
        if self.state != PaperStatus::Disarmed || !valid_stream {
            return Err("operator disarmed or paper stream not qualified");
        }
        let snapshot = operator.load().map_err(|_| "invalid operator")?;
        if !matches!(
            snapshot.mode,
            TradingMode::ShortOnly | TradingMode::ShortBiased | TradingMode::BiDirectional
        ) {
            return Err("mode forbids short entries");
        }
        self.state = PaperStatus::Armed;
        Ok(())
    }
    pub fn entries_allowed(&self) -> bool {
        self.state == PaperStatus::Armed
    }
    pub fn halt_entries(&mut self, operator: &AtomicOperatorState) {
        // CAS update preserves concurrent changes; a failed operator update
        // still leaves the independent interlock latched entry-halted.
        let _ = operator.halt();
        self.state = PaperStatus::EntryHalted;
    }
    pub fn snapshot(&self) -> OperatorSnapshot {
        OperatorSnapshot {
            mode: if self.entries_allowed() {
                TradingMode::ShortOnly
            } else {
                TradingMode::HaltAll
            },
            ..OperatorSnapshot::default()
        }
    }
}
