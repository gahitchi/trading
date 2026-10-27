use trading_core_types::FixedQty;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderStatus {
    Draft, Sent, Acknowledged, CancelPending, Filled, Canceled, Rejected, Unknown,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderEventKind {
    Ack,
    CumulativeFill(FixedQty),
    CancelConfirmed,
    Reject,
    ReconcileRequired,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OrderEvent {
    pub client_id: u64,
    pub sequence: u64,
    pub event: OrderEventKind,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderTrackerError {
    InvalidInput, NonMonotonicSequence, InvalidTransition, UncertainState,
}
#[derive(Debug, Clone, Copy)]
pub struct OrderTracker {
    id: u64,
    quantity: FixedQty,
    cumulative: FixedQty,
    status: OrderStatus,
    last_sequence: u64,
    last_event: Option<OrderEventKind>,
}
impl OrderTracker {
    pub fn new(id: u64, quantity: FixedQty) -> Result<Self, OrderTrackerError> {
        if id == 0 || quantity.0 <= 0 { return Err(OrderTrackerError::InvalidInput); }
        Ok(Self {
            id, quantity, cumulative: FixedQty::ZERO,
            status: OrderStatus::Draft, last_sequence: 0, last_event: None,
        })
    }
    pub fn status(&self) -> OrderStatus { self.status }
    pub fn cumulative_filled(&self) -> FixedQty { self.cumulative }
    pub fn remaining(&self) -> FixedQty {
        FixedQty(self.quantity.0 - self.cumulative.0)
    }
    pub fn sent(&mut self) -> Result<(), OrderTrackerError> {
        if self.status != OrderStatus::Draft { return Err(OrderTrackerError::InvalidTransition); }
        self.status = OrderStatus::Sent;
        Ok(())
    }
    pub fn cancel_requested(&mut self) -> Result<(), OrderTrackerError> {
        if !matches!(self.status, OrderStatus::Sent | OrderStatus::Acknowledged) {
            return Err(OrderTrackerError::InvalidTransition);
        }
        self.status = OrderStatus::CancelPending;
        Ok(())
    }
    pub fn apply(&mut self, report: OrderEvent) -> Result<OrderStatus, OrderTrackerError> {
        if report.client_id != self.id || report.sequence == 0 {
            return Err(OrderTrackerError::InvalidInput);
        }
        if self.last_sequence == report.sequence && self.last_event == Some(report.event) {
            return Ok(self.status);
        }
        if report.sequence <= self.last_sequence {
            return Err(OrderTrackerError::NonMonotonicSequence);
        }
        if self.status == OrderStatus::Unknown { return Err(OrderTrackerError::UncertainState); }
        if matches!(self.status, OrderStatus::Draft | OrderStatus::Filled
            | OrderStatus::Canceled | OrderStatus::Rejected)
        {
            return Err(OrderTrackerError::InvalidTransition);
        }
        let (state, filled) = match report.event {
            OrderEventKind::ReconcileRequired => (OrderStatus::Unknown, self.cumulative),
            OrderEventKind::Ack if self.status == OrderStatus::Sent =>
                (OrderStatus::Acknowledged, self.cumulative),
            OrderEventKind::CumulativeFill(q) if q.0 > self.cumulative.0
                && q.0 <= self.quantity.0 => (
                    if q == self.quantity { OrderStatus::Filled } else { self.status }, q,
                ),
            OrderEventKind::CancelConfirmed if self.status == OrderStatus::CancelPending =>
                (OrderStatus::Canceled, self.cumulative),
            OrderEventKind::Reject if self.status == OrderStatus::Sent =>
                (OrderStatus::Rejected, self.cumulative),
            _ => return Err(OrderTrackerError::InvalidTransition),
        };
        self.status = state;
        self.cumulative = filled;
        self.last_sequence = report.sequence;
        self.last_event = Some(report.event);
        Ok(state)
    }
}
