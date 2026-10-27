use trading_core_types::{FixedPrice, OperatorSnapshot, TradingMode, TimeInForce, SCALE};
use trading_wallet_manager::Strategy;
use trading_strategy_engine::{
    EntryCandidate, EntryEngine, EntrySnapshot, ManualTrap, QuoteError, QuoteInputs, QuoteKind,
    StrategyError, choose_short_quote,
};

fn entry() -> EntrySnapshot {
    EntrySnapshot {
        observed_ns: 1_000_000_000,
        feed_live_and_synchronized: true,
        symbol_id: 0,
        microprice: FixedPrice(100 * SCALE),
        ofi_z: -3.5,
        bid_imbalance: -0.75,
        bid_vacuum: 0.25,
        signed_sell_toxicity: 0.55,
        isr_down: 3.0,
        hawkes_activity_proxy: 0.6,
        last_long_liquidation_ns: None,
        leader_isr_down: 1.0,
        leader_return_50ms_bps: 0.0,
        laggard_residual_bps: 0.0,
    }
}
fn test_candidate(s: Strategy) -> EntryCandidate {
    EntryCandidate { strategy: s, symbol_id: 0, decision_ts_ns: 1_000_000_000,
        short_only: true }
}
#[test]
fn a_vacuum_toxicity_trigger_is_research_only() {
    let mut e = EntryEngine::new();
    let out = e.evaluate(1_000_000_000, OperatorSnapshot::default(), entry()).unwrap();
    assert_eq!(out, Some(test_candidate(Strategy::A)));
}
#[test]
fn b_cascade_requires_recent_liquidation_and_uses_priority() {
    let mut e = EntryEngine::new();
    let mut x = entry();
    x.isr_down = 8.0;
    x.hawkes_activity_proxy = 0.95;
    x.last_long_liquidation_ns = Some(990_000_000);
    assert_eq!(e.evaluate(1_000_000_000, OperatorSnapshot::default(), x).unwrap(),
        Some(test_candidate(Strategy::B)));
    x.last_long_liquidation_ns = None;
    assert_eq!(e.evaluate(1_000_000_000, OperatorSnapshot::default(), x).unwrap(),
        Some(test_candidate(Strategy::A)));
}
#[test]
fn c_leader_laggard_requires_all_four_signals() {
    let mut e = EntryEngine::new();
    let mut x = entry();
    x.ofi_z = -1.5;
    x.leader_isr_down = 5.0;
    x.leader_return_50ms_bps = -15.0;
    x.laggard_residual_bps = -12.0;
    assert_eq!(e.evaluate(1_000_000_000, OperatorSnapshot::default(), x).unwrap(),
        Some(test_candidate(Strategy::C)));
    x.leader_isr_down = 3.0;
    assert!(e.evaluate(1_000_000_000, OperatorSnapshot::default(), x).unwrap().is_none());
}
#[test]
fn aggressiveness_changes_strict_thresholds() {
    let mut e = EntryEngine::new();
    let mut op = OperatorSnapshot { aggressiveness_pct: 50, ..Default::default() };
    assert!(e.evaluate(1_000_000_000, op, entry()).unwrap().is_none());
    op.aggressiveness_pct = 200;
    assert_eq!(e.evaluate(1_000_000_000, op, entry()).unwrap(),
        Some(test_candidate(Strategy::A)));
}
#[test]
fn disabled_strategy_and_halt_and_symbol_mask_fail_closed() {
    let mut e = EntryEngine::new();
    let mut op = OperatorSnapshot { strategy_enable_mask: 0, ..Default::default() };
    assert!(e.evaluate(1_000_000_000, op, entry()).unwrap().is_none());
    op.strategy_enable_mask = 15;
    op.mode = TradingMode::HaltAll;
    assert!(e.evaluate(1_000_000_000, op, entry()).unwrap().is_none());
    op.mode = TradingMode::ShortOnly;
    op.active_symbol_mask = 0;
    assert!(e.evaluate(1_000_000_000, op, entry()).unwrap().is_none());
}
#[test]
fn stale_future_or_disconnected_signals_error_without_decisions() {
    for change in 0..3 {
        let mut e = EntryEngine::new();
        let mut s = entry();
        match change {
            0 => s.observed_ns -= 100_000_000,
            1 => s.observed_ns += 10_000_000,
            _ => s.feed_live_and_synchronized = false,
        }
        assert_eq!(e.evaluate(1_000_000_000, OperatorSnapshot::default(), s),
            Err(StrategyError::StaleOrFutureSignal));
    }
}
#[test]
fn nan_and_out_of_bounds_signals_are_rejected() {
    let mut e = EntryEngine::new();
    let mut x = entry();
    x.ofi_z = f64::NAN;
    assert_eq!(e.evaluate(1_000_000_000, OperatorSnapshot::default(), x),
        Err(StrategyError::InvalidSignal));
    x.ofi_z = -3.5;
    x.bid_imbalance = -1.5;
    assert_eq!(e.evaluate(1_000_000_000, OperatorSnapshot::default(), x),
        Err(StrategyError::InvalidSignal));
}
#[test]
fn backwards_decision_time_is_disallowed() {
    let mut e = EntryEngine::new();
    e.evaluate(1_000_000_000, OperatorSnapshot::default(), entry()).unwrap();
    assert_eq!(e.evaluate(999_999_999, OperatorSnapshot::default(), entry()),
        Err(StrategyError::TimeReversal));
}
#[test]
fn manual_trap_requires_arm_and_fires_once() {
    let mut e = EntryEngine::new();
    let op = OperatorSnapshot { mode: TradingMode::ManualArmedTrap, ..Default::default() };
    let trap = ManualTrap {
        arm_id: 501, symbol_id: 0,
        threshold_price: FixedPrice(101 * SCALE), expires_at_ns: 2_000_000_000,
    };
    assert!(e.evaluate(1_000_000_000, op, entry()).unwrap().is_none());
    e.arm_trap(trap, 1_000_000_000, op).unwrap();
    let mut x = entry();
    x.bid_imbalance = -0.75;
    x.ofi_z = -3.5;
    // Arm only applies in manual mode and is one-shot.
    let fire = e.evaluate(1_000_000_000, op, x).unwrap();
    assert_eq!(fire, Some(test_candidate(Strategy::D)));
    assert_eq!(e.armed(), None);
    assert_eq!(e.evaluate(1_000_000_000, op, x).unwrap(), None);
}
#[test]
fn trap_expiry_and_disarm_on_feed_gap() {
    let mut e = EntryEngine::new();
    let op = OperatorSnapshot { mode: TradingMode::ManualArmedTrap, ..Default::default() };
    let trap = ManualTrap {
        arm_id: 500, symbol_id: 0,
        threshold_price: FixedPrice(100 * SCALE), expires_at_ns: 1_100_000_000,
    };
    e.arm_trap(trap, 1_000_000_000, op).unwrap();
    e.reset_on_feed_gap();
    assert!(e.armed().is_none());
    e.arm_trap(trap, 1_000_000_000, op).unwrap();
    assert!(e.evaluate(1_100_000_000, op,
        EntrySnapshot { observed_ns: 1_100_000_000, ..entry() })
        .unwrap().is_none());
}
fn quote() -> QuoteInputs {
    QuoteInputs {
        best_bid: FixedPrice(100 * SCALE),
        best_ask: FixedPrice(101 * SCALE),
        microprice: FixedPrice(100 * SCALE + SCALE / 2),
        microprice_divergence_bps: 1.0,
        isr_down: 3.0,
        book_imbalance: -0.2,
    }
}
#[test]
fn urgent_ioc_sell_limit_is_four_bps_below_bid() {
    let mut x = quote();
    x.isr_down = 30.0;
    let q = choose_short_quote(x).unwrap();
    assert_eq!(q.kind, QuoteKind::IocSweep);
    assert_eq!(q.time_in_force, TimeInForce::Ioc);
    assert_eq!(q.short_sell_limit, FixedPrice(100 * SCALE * 9996 / 10000));
}
#[test]
fn passive_sell_never_crosses_current_visible_best_ask() {
    let q = choose_short_quote(quote()).unwrap();
    assert_eq!(q.kind, QuoteKind::PassivePostOnly);
    assert_eq!(q.time_in_force, TimeInForce::PostOnly);
    assert!(q.short_sell_limit >= FixedPrice(101 * SCALE));
}
#[test]
fn invalid_crossed_book_and_nan_alpha_rejected() {
    let mut x = quote();
    x.best_ask = x.best_bid;
    assert_eq!(choose_short_quote(x), Err(QuoteError::InvalidBook));
    x = quote();
    x.isr_down = f64::NAN;
    assert_eq!(choose_short_quote(x), Err(QuoteError::InvalidSignal));
}
