use trading_core_types::{FixedPrice, FixedQty, SCALE};
use trading_strategy_engine::{
    ExitReason, ExitReportKind, LifecycleError, LifecyclePhase, LifecycleSignals, OrderEvent,
    OrderEventKind, OrderStatus, OrderTracker, OrderTrackerError, ShortTradeLifecycle, TradeTick,
    continuation_proxy,
};
use trading_wallet_manager::Strategy;
fn px(v: i64) -> FixedPrice {
    FixedPrice(v * SCALE)
}
fn qty(v: i64) -> FixedQty {
    FixedQty(v * SCALE)
}
fn sig() -> LifecycleSignals {
    LifecycleSignals {
        ofi_z: -3.0,
        book_imbalance: -0.6,
        hawkes_acceleration: 3.0,
        hawkes_activity_proxy: 0.8,
        bid_vacuum_ratio: 0.2,
        absorption_ratio: 0.0,
    }
}
fn tick(t: u64, ask: i64, micro: i64) -> TradeTick {
    TradeTick {
        symbol_id: 0,
        timestamp_ns: t,
        synchronized: true,
        observed_ns: t,
        best_ask_to_cover: px(ask),
        microprice: px(micro),
        spread_bps: 1.0,
        sigma_10s_bps: 2.0,
        nearest_bid_wall: None,
        signals: sig(),
    }
}
fn short() -> ShortTradeLifecycle {
    ShortTradeLifecycle::from_confirmed_short(Strategy::B, 0, 0, px(100), qty(10), 20.0, 5.0)
        .unwrap()
}
#[test]
fn lifecycle_has_real_short_initial_stop_and_exposure() {
    let l = short();
    assert_eq!(l.phase(), LifecyclePhase::InitialRisk);
    assert_eq!(l.remaining(), qty(10));
    assert!(l.stop_price() > px(100));
    assert!(l.pending_exit().is_none());
}
#[test]
fn hard_stop_proposes_reduce_only_cover_without_fake_fill() {
    let mut l = short();
    let out = l.on_tick(tick(1, 101, 101)).unwrap().unwrap();
    assert_eq!(out.close_quantity, qty(10));
    assert_eq!(out.reason, ExitReason::HardStop);
    assert!(out.reduce_only);
    assert_eq!(l.remaining(), qty(10)); // never optimistic fill
    assert_eq!(l.phase(), LifecyclePhase::ExitPending);
}
#[test]
fn fakeout_aborts_within_1500ms() {
    let mut l = short();
    let mut t = tick(100_000_000, 100, 100);
    t.signals.ofi_z = 5.0;
    t.signals.book_imbalance = 0.8;
    t.signals.hawkes_acceleration = -5.0;
    t.signals.hawkes_activity_proxy = 0.0;
    t.signals.bid_vacuum_ratio = 2.0;
    t.signals.absorption_ratio = 5.0;
    assert!(continuation_proxy(t.signals, 0) < -0.25);
    assert_eq!(l.on_tick(t).unwrap().unwrap().reason, ExitReason::Fakeout);
}
#[test]
fn full_risk_pending_cannot_double_cover_on_subsequent_ticks() {
    let mut l = short();
    let act = l.on_tick(tick(1, 101, 101)).unwrap().unwrap();
    assert!(l.on_tick(tick(2, 100, 100)).unwrap().is_none());
    assert_eq!(l.pending_exit(), Some(act));
    assert_eq!(l.remaining(), qty(10));
}
#[test]
fn pending_stop_breach_is_exposed_to_supervisor_not_silenced() {
    let mut l = short();
    l.on_tick(tick(1, 101, 101)).unwrap().unwrap();
    assert_eq!(
        l.on_tick(tick(2, 102, 102)),
        Err(LifecycleError::PendingExitAtRisk)
    );
    assert_eq!(l.remaining(), qty(10));
}
#[test]
fn breakeven_stop_ratchets_only_down_for_short() {
    let mut l = short();
    l.on_tick(tick(2_000_000_000, 99, 99)).unwrap();
    assert_eq!(l.phase(), LifecyclePhase::Breakeven);
    assert!(l.stop_price() < px(100));
    let prior = l.stop_price();
    l.on_tick(tick(2_100_000_000, 99, 99)).unwrap();
    assert!(l.stop_price() <= prior);
}
#[test]
fn half_position_partial_exit_requires_confirmed_cumulative_fills() {
    let mut l = short();
    l.on_tick(tick(2_000_000_000, 99, 99)).unwrap();
    let mut t = tick(3_000_000_000, 97, 97);
    t.signals.ofi_z = 4.0;
    t.signals.hawkes_acceleration = -8.0;
    t.signals.hawkes_activity_proxy = 0.1;
    t.signals.bid_vacuum_ratio = 2.0;
    let exit = l.on_tick(t).unwrap().unwrap();
    assert_eq!(exit.reason, ExitReason::PartialHarvest);
    assert_eq!(exit.close_quantity, qty(5));
    assert_eq!(l.remaining(), qty(10));
    l.on_exit_report(exit.action_id, 1, qty(2), ExitReportKind::Working)
        .unwrap();
    assert_eq!(l.remaining(), qty(8));
    assert!(l.on_tick(tick(3_100_000_000, 97, 97)).unwrap().is_none());
    l.on_exit_report(exit.action_id, 2, qty(2), ExitReportKind::Canceled)
        .unwrap();
    assert_eq!(l.phase(), LifecyclePhase::PartialExitPending);
    let retry = l.on_tick(tick(3_200_000_000, 97, 97)).unwrap().unwrap();
    assert_ne!(retry.action_id, exit.action_id);
    assert_eq!(retry.close_quantity, qty(3));
    l.on_exit_report(retry.action_id, 1, qty(3), ExitReportKind::Filled)
        .unwrap();
    assert_eq!(l.phase(), LifecyclePhase::Runner);
    assert_eq!(l.remaining(), qty(5));
    let mut climax = tick(3_300_000_000, 96, 96);
    climax.signals.absorption_ratio = 5.0;
    let final_exit = l.on_tick(climax).unwrap().unwrap();
    assert_eq!(final_exit.reason, ExitReason::Climax);
    assert_eq!(final_exit.close_quantity, qty(5));
    l.on_exit_report(final_exit.action_id, 1, qty(5), ExitReportKind::Filled)
        .unwrap();
    assert_eq!(l.phase(), LifecyclePhase::Closed);
    assert_eq!(l.remaining(), FixedQty::ZERO);
}
#[test]
fn partial_fill_overflow_or_replayed_sequence_cannot_mutate_exposure() {
    let mut l = short();
    let a = l.on_tick(tick(1, 101, 101)).unwrap().unwrap();
    assert_eq!(
        l.on_exit_report(a.action_id, 1, qty(11), ExitReportKind::Working),
        Err(LifecycleError::InvalidInput)
    );
    assert_eq!(l.remaining(), qty(10));
    l.on_exit_report(a.action_id, 1, qty(3), ExitReportKind::Working)
        .unwrap();
    assert_eq!(
        l.on_exit_report(a.action_id, 1, qty(3), ExitReportKind::Working),
        Err(LifecycleError::NonMonotonicReport)
    );
    assert_eq!(l.remaining(), qty(7));
}
#[test]
fn lost_exit_confirmation_latches_reconciliation_required() {
    let mut l = short();
    let a = l.on_tick(tick(1, 101, 101)).unwrap().unwrap();
    l.on_exit_report(a.action_id, 1, qty(1), ExitReportKind::Unknown)
        .unwrap();
    assert_eq!(l.phase(), LifecyclePhase::ReconciliationRequired);
    assert_eq!(l.remaining(), qty(9));
    assert_eq!(
        l.on_tick(tick(2, 102, 102)),
        Err(LifecycleError::InvalidTransition)
    );
}
#[test]
fn canceled_full_exit_does_not_enable_reentry() {
    let mut l = short();
    let a = l.on_tick(tick(1, 101, 101)).unwrap().unwrap();
    l.on_exit_report(a.action_id, 1, FixedQty::ZERO, ExitReportKind::Canceled)
        .unwrap();
    assert_eq!(l.phase(), LifecyclePhase::ReconciliationRequired);
}
#[test]
fn mismatched_symbol_stale_tick_and_nonmonotonic_time_are_rejected() {
    let mut l = short();
    let mut t = tick(100, 100, 100);
    t.symbol_id = 1;
    assert_eq!(l.on_tick(t), Err(LifecycleError::InvalidInput));
    t.symbol_id = 0;
    t.observed_ns = 101;
    assert_eq!(l.on_tick(t), Err(LifecycleError::InvalidInput));
    l.on_tick(tick(100, 100, 100)).unwrap();
    assert_eq!(
        l.on_tick(tick(100, 100, 100)),
        Err(LifecycleError::TimeReversal)
    );
}
#[test]
fn nonfinite_lifecycle_signals_are_rejected() {
    let mut l = short();
    let mut t = tick(100, 100, 100);
    t.signals.ofi_z = f64::INFINITY;
    assert_eq!(l.on_tick(t), Err(LifecycleError::InvalidInput));
}
fn report(id: u64, seq: u64, event: OrderEventKind) -> OrderEvent {
    OrderEvent {
        client_id: id,
        sequence: seq,
        event,
    }
}
#[test]
fn order_tracker_partial_fill_then_cancel_ack_is_not_full_fill() {
    let mut o = OrderTracker::new(7, qty(10)).unwrap();
    o.sent().unwrap();
    assert_eq!(
        o.apply(report(7, 1, OrderEventKind::Ack)).unwrap(),
        OrderStatus::Acknowledged
    );
    o.cancel_requested().unwrap();
    o.apply(report(7, 2, OrderEventKind::CumulativeFill(qty(3))))
        .unwrap();
    assert_eq!(o.status(), OrderStatus::CancelPending);
    o.apply(report(7, 3, OrderEventKind::CancelConfirmed))
        .unwrap();
    assert_eq!(o.remaining(), qty(7));
    assert_eq!(o.status(), OrderStatus::Canceled);
}
#[test]
fn order_tracker_duplicate_source_sequence_is_idempotent() {
    let mut o = OrderTracker::new(50, qty(10)).unwrap();
    o.sent().unwrap();
    let r = report(50, 1, OrderEventKind::Ack);
    assert_eq!(o.apply(r).unwrap(), OrderStatus::Acknowledged);
    assert_eq!(o.apply(r).unwrap(), OrderStatus::Acknowledged);
    assert_eq!(
        o.apply(report(50, 1, OrderEventKind::CumulativeFill(qty(1)))),
        Err(OrderTrackerError::NonMonotonicSequence)
    );
}
#[test]
fn order_tracker_rejects_overfills_and_stale_client_id() {
    let mut o = OrderTracker::new(50, qty(10)).unwrap();
    o.sent().unwrap();
    assert_eq!(
        o.apply(report(50, 1, OrderEventKind::CumulativeFill(qty(11)))),
        Err(OrderTrackerError::InvalidTransition)
    ); // invalid branch, not accepted
    assert_eq!(o.cumulative_filled(), FixedQty::ZERO);
    assert_eq!(
        o.apply(report(51, 1, OrderEventKind::Ack)),
        Err(OrderTrackerError::InvalidInput)
    );
}
#[test]
fn uncertain_order_state_refuses_subsequent_reports() {
    let mut o = OrderTracker::new(9, qty(5)).unwrap();
    o.sent().unwrap();
    o.apply(report(9, 1, OrderEventKind::ReconcileRequired))
        .unwrap();
    assert_eq!(o.status(), OrderStatus::Unknown);
    assert_eq!(
        o.apply(report(9, 2, OrderEventKind::Ack)),
        Err(OrderTrackerError::UncertainState)
    );
}
#[test]
fn validated_full_fill_reaches_terminal_state() {
    let mut o = OrderTracker::new(8, qty(5)).unwrap();
    o.sent().unwrap();
    o.apply(report(8, 1, OrderEventKind::CumulativeFill(qty(2))))
        .unwrap();
    assert_eq!(
        o.apply(report(8, 2, OrderEventKind::CumulativeFill(qty(5))))
            .unwrap(),
        OrderStatus::Filled
    );
    assert_eq!(o.remaining(), FixedQty::ZERO);
    assert_eq!(
        o.apply(report(8, 3, OrderEventKind::CumulativeFill(qty(5)))),
        Err(OrderTrackerError::InvalidTransition)
    );
}
