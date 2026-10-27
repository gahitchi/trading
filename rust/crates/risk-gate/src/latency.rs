use crate::types::RiskError;

const MAX_NS: u64 = 150_000_000;
const HEALTHY_NS: u64 = 25_000_000;
const REQUIRED_GOOD: u8 = 50;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LatencyState {
    pub blocked: bool,
    pub recovery_streak: u8,
}
#[derive(Debug, Clone, Copy)]
pub struct LatencyCircuit {
    state: LatencyState,
    last_receive_ns: Option<u64>,
}
impl Default for LatencyCircuit {
    fn default() -> Self {
        // Cold start must never assume an unseen connection is healthy.
        Self {
            state: LatencyState { blocked: true, recovery_streak: 0 },
            last_receive_ns: None,
        }
    }
}
impl LatencyCircuit {
    pub fn new() -> Self { Self::default() }
    pub fn state(&self) -> LatencyState { self.state }
    pub fn last_receive_ns(&self) -> Option<u64> { self.last_receive_ns }
    pub fn trip(&mut self) {
        self.state = LatencyState { blocked: true, recovery_streak: 0 };
    }
    pub fn observe(&mut self, exchange_ts_ns: u64, recv_ts_ns: u64)
        -> Result<LatencyState, RiskError> {
        // The ingestion adapter must prove timestamps are normalized into
        // the same calibrated clock domain and are genuine venue events.
        if recv_ts_ns < exchange_ts_ns
            || self.last_receive_ns.is_some_and(|last| recv_ts_ns <= last)
        {
            self.trip();
            return Err(RiskError::ClockReversal);
        }
        self.last_receive_ns = Some(recv_ts_ns);
        let latency = recv_ts_ns - exchange_ts_ns;
        if latency > MAX_NS {
            self.trip();
        } else if self.state.blocked {
            if latency < HEALTHY_NS {
                self.state.recovery_streak += 1;
                if self.state.recovery_streak >= REQUIRED_GOOD {
                    self.state.blocked = false;
                }
            } else {
                self.state.recovery_streak = 0;
            }
        }
        Ok(self.state)
    }
}
