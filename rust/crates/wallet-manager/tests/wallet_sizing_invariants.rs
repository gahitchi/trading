use std::str::FromStr;
use trading_core_types::{FixedPrice, FixedQty, FixedUsd, OperatorSnapshot, TradingMode, SCALE};
use trading_orderbook::{FlatOrderBook, PriceLevel};
use trading_wallet_manager::{
    BayesianKelly, ConvictionInputs, EntrySizingInputs, NoTradeReason,
    SizingDecision, Strategy, WalletBuckets, WalletError, compute_short_position_size,
};

fn usd(x: &str) -> FixedUsd { FixedUsd::from_str(x).unwrap() }
fn price(x: i64) -> FixedPrice { FixedPrice(x * SCALE) }
fn qty(x: i64) -> FixedQty { FixedQty(x * SCALE) }

fn book() -> FlatOrderBook {
    let mut book = FlatOrderBook::new(20).unwrap();
    let bids: Vec<_> = (0..10).map(|i| PriceLevel {
        price: FixedPrice(price(100).0 - i * SCALE / 100),
        qty: qty(100),
    }).collect();
    let asks: Vec<_> = (0..10).map(|i| PriceLevel {
        price: FixedPrice(price(101).0 + i * SCALE / 100),
        qty: qty(100),
    }).collect();
    book.load_snapshot(&bids, &asks).unwrap();
    book
}

fn ready_kelly() -> BayesianKelly {
    let mut k = BayesianKelly::new();
    for _ in 0..42 {
        k.observe(Strategy::A, usd("1"), usd("1")).unwrap();
    }
    k
}
fn entry_inputs() -> EntrySizingInputs {
    EntrySizingInputs {
        sigma_1m_bps_raw: 15 * SCALE,
        gross_open_and_pending_notional: usd("0"),
        minimum_order_notional: usd("1"),
        conviction: ConvictionInputs {
            ofi_abs_z_raw: 280_000_000,
            isr_down_raw: 450_000_000,
            vpin_short_raw: 45_000_000,
        },
    }
}
#[test]
fn initial_wallet_is_exactly_three_bucket_70_30() {
    let w = WalletBuckets::new(usd("1000")).unwrap();
    let x = w.snapshot().unwrap();
    assert_eq!(x.margin_reserve, usd("300"));
    assert_eq!(x.active_equity, usd("700"));
    assert_eq!(x.locked_profit_vault, usd("0"));
    assert_eq!(x.session_floor, usd("672"));
    assert_eq!(x.session_high_water_mark, usd("700"));
    assert_eq!(x.total_equity, usd("1000"));
    assert_eq!(w.cppi_damper_ppm().unwrap(), 1_000_000);
}

#[test]
fn profit_closure_sweeps_35_percent_to_untouchable_vault() {
    let mut w = WalletBuckets::new(usd("1000")).unwrap();
    let x = w.settle_close(1, usd("100"), 35).unwrap();
    assert_eq!(x.locked_profit_vault, usd("35"));
    assert_eq!(x.active_equity, usd("765"));
    assert_eq!(x.total_equity, usd("1100"));
    assert_eq!(x.session_high_water_mark, usd("765"));
    assert_eq!(x.session_floor, usd("734.4"));
}

#[test]
fn losses_debit_active_and_never_debit_vault_or_reserve() {
    let mut w = WalletBuckets::new(usd("1000")).unwrap();
    w.settle_close(1, usd("100"), 35).unwrap();
    let x = w.settle_close(2, usd("-20"), 35).unwrap();
    assert_eq!(x.active_equity, usd("745"));
    assert_eq!(x.locked_profit_vault, usd("35"));
    assert_eq!(x.margin_reserve, usd("300"));
    assert_eq!(x.total_equity, usd("1080"));
    assert_eq!(x.session_high_water_mark, usd("765"));
    assert!(!x.halt_required);
    assert!(w.cppi_damper_ppm().unwrap() < 1_000_000);
}

#[test]
fn cppi_floor_breaches_and_latches_halt() {
    let mut w = WalletBuckets::new(usd("1000")).unwrap();
    let x = w.settle_close(1, usd("-28"), 35).unwrap();
    assert!(x.halt_required);
    assert_eq!(x.active_equity, usd("672"));
    assert_eq!(w.cppi_damper_ppm().unwrap(), 0);
    let y = w.settle_close(2, usd("100"), 35).unwrap();
    assert!(y.halt_required);
}

#[test]
fn insolvency_is_not_turned_into_negative_bucket() {
    let mut w = WalletBuckets::new(usd("1000")).unwrap();
    assert_eq!(w.settle_close(1, usd("-701"), 35), Err(WalletError::Insolvent));
    let x = w.snapshot().unwrap();
    assert_eq!(x.active_equity, usd("700"));
    assert_eq!(x.last_settlement_id, 0);
    assert!(x.halt_required);
}

#[test]
fn replayed_out_of_order_or_zero_settlement_ids_fail_closed() {
    let mut w = WalletBuckets::new(usd("1000")).unwrap();
    w.settle_close(100, usd("1"), 35).unwrap();
    assert_eq!(w.settle_close(100, usd("1"), 35), Err(WalletError::NonMonotonicSettlement));
    assert_eq!(w.settle_close(99, usd("1"), 35), Err(WalletError::NonMonotonicSettlement));
    assert_eq!(w.settle_close(0, usd("1"), 35), Err(WalletError::NonMonotonicSettlement));
    assert_eq!(w.snapshot().unwrap().total_equity, usd("1001"));
}

#[test]
fn wallet_validation_and_overflow_are_transactional() {
    assert!(WalletBuckets::new(usd("0")).is_err());
    let mut w = WalletBuckets::new(usd("1000")).unwrap();
    assert_eq!(w.settle_close(1, usd("1"), 101), Err(WalletError::InvalidPercentage));
    assert_eq!(w.snapshot().unwrap().last_settlement_id, 0);
    let mut w = WalletBuckets::new(FixedUsd(i64::MAX - 10)).unwrap();
    assert_eq!(w.settle_close(1, FixedUsd(i64::MAX), 0), Err(WalletError::ArithmeticOverflow));
    assert_eq!(w.snapshot().unwrap().last_settlement_id, 0);
}

#[test]
fn kelly_priors_are_exact_and_require_calibration() {
    let k = BayesianKelly::new();
    let e = k.estimate(Strategy::A).unwrap();
    assert_eq!(e.win_probability_raw, 55_000_000);
    assert_eq!(e.payoff_ratio_raw, 150_000_000);
    assert_eq!(e.risk_fraction_raw, 3_500_000);
    assert!(!e.sample_ready);
    assert_eq!(e.effective_samples_raw, 0);
}

#[test]
fn kelly_evidence_is_strictly_strategy_scoped() {
    let mut k = ready_kelly();
    assert!(k.estimate(Strategy::A).unwrap().sample_ready);
    assert!(!k.estimate(Strategy::B).unwrap().sample_ready);
    assert!(!k.estimate(Strategy::C).unwrap().sample_ready);
    assert!(!k.estimate(Strategy::D).unwrap().sample_ready);
    for _ in 0..250 { k.observe(Strategy::B, usd("-1"), usd("1")).unwrap(); }
    let b = k.estimate(Strategy::B).unwrap();
    assert!(b.sample_ready);
    assert_eq!(b.risk_fraction_raw, 0); // Never force a 0.4% trade on negative edge.
}

#[test]
fn kelly_rejects_invalid_risk_samples_without_mutation() {
    let mut k = BayesianKelly::new();
    let before = k.estimate(Strategy::A).unwrap();
    assert_eq!(k.observe(Strategy::A, usd("1"), usd("0")), Err(WalletError::InvalidObservation));
    assert_eq!(k.observe(Strategy::A, usd("0"), usd("1")), Err(WalletError::InvalidObservation));
    assert_eq!(k.observe(Strategy::A, usd("101"), usd("1")), Err(WalletError::InvalidObservation));
    assert_eq!(k.estimate(Strategy::A).unwrap(), before);
}

#[test]
fn conviction_formula_and_clamps_are_fixed_point_exact() {
    let m = entry_inputs().conviction.multiplier_raw().unwrap();
    assert_eq!(m, SCALE);
    let none = ConvictionInputs { ofi_abs_z_raw: 0, isr_down_raw: 0, vpin_short_raw: 0 };
    assert_eq!(none.multiplier_raw().unwrap(), 60_000_000);
    let all = ConvictionInputs {
        ofi_abs_z_raw: 1_000 * SCALE,
        isr_down_raw: 1_000 * SCALE,
        vpin_short_raw: SCALE,
    };
    assert_eq!(all.multiplier_raw().unwrap(), 185_000_000);
}

#[test]
fn conviction_rejects_out_of_domain_inputs() {
    let mut x = entry_inputs().conviction;
    x.vpin_short_raw = SCALE + 1;
    assert_eq!(x.multiplier_raw(), Err(WalletError::InvalidObservation));
    x.vpin_short_raw = 0;
    x.ofi_abs_z_raw = -1;
    assert_eq!(x.multiplier_raw(), Err(WalletError::InvalidObservation));
}

#[test]
fn default_short_mode_permits_positive_edge_with_depth_and_leverage() {
    let w = WalletBuckets::new(usd("10000")).unwrap();
    let k = ready_kelly();
    let x = compute_short_position_size(&w, &k, Strategy::A,
        OperatorSnapshot::default(), &book(), entry_inputs()).unwrap();
    let SizingDecision::Permit(p) = x else { panic!("expected bounded short proposal") };
    assert!(p.final_notional.0 > 0);
    assert!(p.final_notional.0 <= p.kelly_notional_cap.0);
    assert!(p.final_notional.0 <= p.liquidity_notional_cap.0);
    assert!(p.final_notional.0 <= p.leverage_notional_cap.0);
    assert_eq!(p.conviction_multiplier_raw, SCALE);
    assert_eq!(p.stop_distance_bps_raw, 18 * SCALE);
}

#[test]
fn positive_edge_without_observation_gate_cannot_open() {
    let w = WalletBuckets::new(usd("10000")).unwrap();
    let x = compute_short_position_size(&w, &BayesianKelly::new(), Strategy::A,
        OperatorSnapshot::default(), &book(), entry_inputs()).unwrap();
    assert_eq!(x, SizingDecision::Reject(NoTradeReason::InsufficientSample));
}

#[test]
fn signal_sizing_respects_operator_mode_and_strategy_mask() {
    let w = WalletBuckets::new(usd("10000")).unwrap();
    let k = ready_kelly();
    let mut operator = OperatorSnapshot::default();
    operator.mode = TradingMode::HaltAll;
    assert_eq!(compute_short_position_size(&w, &k, Strategy::A, operator, &book(), entry_inputs()).unwrap(),
        SizingDecision::Reject(NoTradeReason::OperatorHalt));
    operator.mode = TradingMode::LongOnly;
    assert_eq!(compute_short_position_size(&w, &k, Strategy::A, operator, &book(), entry_inputs()).unwrap(),
        SizingDecision::Reject(NoTradeReason::WrongMode));
    operator.mode = TradingMode::ShortOnly;
    operator.strategy_enable_mask = 0;
    assert_eq!(compute_short_position_size(&w, &k, Strategy::A, operator, &book(), entry_inputs()).unwrap(),
        SizingDecision::Reject(NoTradeReason::StrategyDisabled));
}

#[test]
fn leverage_reservation_blocks_additional_entries() {
    let w = WalletBuckets::new(usd("10000")).unwrap();
    let k = ready_kelly();
    let mut input = entry_inputs();
    input.gross_open_and_pending_notional = usd("21000"); // 3x active 7000
    assert_eq!(compute_short_position_size(&w, &k, Strategy::A,
        OperatorSnapshot::default(), &book(), input).unwrap(),
        SizingDecision::Reject(NoTradeReason::NoLeverageCapacity));
}

#[test]
fn liquidity_cap_never_exceeds_one_quarter_top_five() {
    let w = WalletBuckets::new(usd("10000")).unwrap();
    let k = ready_kelly();
    let x = compute_short_position_size(&w, &k, Strategy::A,
        OperatorSnapshot::default(), &book(), entry_inputs()).unwrap();
    let SizingDecision::Permit(p) = x else { panic!("expected position") };
    let quarter_top5 = book().bids().iter().take(5).map(|x|
        i128::from(x.price.0) * i128::from(x.qty.0) / i128::from(SCALE)
    ).sum::<i128>() / 4;
    assert!(i128::from(p.liquidity_notional_cap.0) <= quarter_top5);
}

#[test]
fn cppi_halt_blocks_sizing_even_after_rebound() {
    let mut w = WalletBuckets::new(usd("10000")).unwrap();
    w.settle_close(1, usd("-280"), 35).unwrap();
    let k = ready_kelly();
    assert_eq!(compute_short_position_size(&w, &k, Strategy::A,
        OperatorSnapshot::default(), &book(), entry_inputs()).unwrap(),
        SizingDecision::Reject(NoTradeReason::SessionFloor));
}

#[test]
fn missing_book_depth_and_subminimum_orders_fail_closed() {
    let w = WalletBuckets::new(usd("10000")).unwrap();
    let k = ready_kelly();
    let mut sparse = FlatOrderBook::new(5).unwrap();
    let b = book();
    sparse.load_snapshot(&b.bids()[..5], &b.asks()[..5]).unwrap();
    let x = compute_short_position_size(&w, &k, Strategy::A,
        OperatorSnapshot::default(), &sparse, entry_inputs()).unwrap();
    assert_eq!(x, SizingDecision::Reject(NoTradeReason::InsufficientBook));
    let mut input = entry_inputs();
    input.minimum_order_notional = usd("10000000");
    assert_eq!(compute_short_position_size(&w, &k, Strategy::A,
        OperatorSnapshot::default(), &b, input).unwrap(),
        SizingDecision::Reject(NoTradeReason::BelowMinimumNotional));
}

#[test]
fn negative_volatility_and_negative_open_exposure_are_errors() {
    let w = WalletBuckets::new(usd("10000")).unwrap();
    let k = ready_kelly();
    let mut input = entry_inputs();
    input.sigma_1m_bps_raw = -1;
    assert_eq!(compute_short_position_size(&w, &k, Strategy::A,
        OperatorSnapshot::default(), &book(), input), Err(WalletError::InvalidAmount));
    input.sigma_1m_bps_raw = 0;
    input.gross_open_and_pending_notional = usd("-1");
    assert_eq!(compute_short_position_size(&w, &k, Strategy::A,
        OperatorSnapshot::default(), &book(), input), Err(WalletError::InvalidAmount));
}
