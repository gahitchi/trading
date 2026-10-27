use trading_core_types::{
    AtomicOperatorState, ComplianceContext, FixedPrice, FixedQty, FixedUsd, OperatorSnapshot,
    OrderIntent, OrderSide, SCALE, TimeInForce, TradingMode, VenueId,
};
use trading_risk_gate::{
    AccountEvidence, GateDecision, LatencyCircuit, MarketEvidence, RiskContext, RiskGate,
    RiskPolicy, RiskReason, TokenBucket,
};
use trading_wallet_manager::WalletBuckets;

fn price(x: i64) -> FixedPrice {
    FixedPrice(x * SCALE)
}
fn qty(x: i64) -> FixedQty {
    FixedQty(x * SCALE)
}
fn usd(x: i64) -> FixedUsd {
    FixedUsd(x * SCALE)
}
const NOW: u64 = 1_000_000_000;
fn context(venue: VenueId) -> RiskContext {
    RiskContext {
        market: MarketEvidence {
            now_ns: NOW,
            quote_received_ns: NOW - 5_000_000,
            synchronized: true,
            certified_event_provenance: true,
            venue,
            symbol_id: 0,
            funding_8h_bps_scaled: 2 * SCALE,
            perp_index_basis_bps_scaled: 3 * SCALE,
            funding_asof_ns: NOW - 1_000_000,
            basis_asof_ns: NOW - 1_000_000,
            isr_up_scaled: SCALE,
            ofi_z_scaled: 0,
        },
        account: AccountEvidence {
            reconciled: true,
            reservations_current: true,
            gross_open_and_pending_usd: usd(0),
            confirmed_short_qty: qty(5),
            maintenance_margin_headroom_usd: usd(100),
            rollover_or_policy_blackout: false,
        },
        equities: ComplianceContext {
            national_best_bid: price(100).0,
            tick_size: price(1).0 / 100,
            short_sale_restriction_active: false,
            located_for_open_short: true,
            trading_blackout: false,
        },
        paper_environment: true,
    }
}
fn order(venue: VenueId, strategy: u8, id: u64) -> OrderIntent {
    OrderIntent::new(
        id,
        NOW,
        price(100),
        qty(1),
        Some(price(101)),
        usd(5),
        0,
        5,
        venue,
        strategy,
        OrderSide::Sell,
        TimeInForce::Gtc,
        false,
    )
    .unwrap()
}
fn cover(venue: VenueId, amount: i64) -> OrderIntent {
    OrderIntent::new(
        900,
        NOW,
        price(100),
        qty(amount),
        None,
        usd(0),
        0,
        5,
        venue,
        0,
        OrderSide::Buy,
        TimeInForce::Ioc,
        true,
    )
    .unwrap()
}
fn wallet() -> trading_wallet_manager::WalletSnapshot {
    WalletBuckets::new(usd(1000)).unwrap().snapshot().unwrap()
}
fn operator() -> AtomicOperatorState {
    AtomicOperatorState::new(OperatorSnapshot::default()).unwrap()
}
fn ready_gate() -> RiskGate {
    let mut g = RiskGate::new(RiskPolicy::default()).unwrap();
    for i in 1..=50u64 {
        let recv = NOW - 50_000_000 + i * 1_000_000;
        let directive = g.on_market_packet(recv - 5_000_000, recv).unwrap();
        assert_eq!(directive.cancel_resting_entries, i < 50);
    }
    assert!(!g.snapshot().latency_open);
    g
}
fn proposed(d: GateDecision) -> i64 {
    match d {
        GateDecision::ProposedEntry { approved_qty, .. } => approved_qty.0,
        other => panic!("expected research proposal: {other:?}"),
    }
}
#[test]
fn latency_starts_open_and_needs_fifty_strict_healthy_samples() {
    let mut g = RiskGate::new(RiskPolicy::default()).unwrap();
    assert!(g.snapshot().latency_open);
    for i in 1..50u64 {
        let r = i * 1_000_000 + 30_000_000;
        g.on_market_packet(r - 20_000_000, r).unwrap();
    }
    assert!(g.snapshot().latency_open);
    let recv = 90_000_000u64;
    g.on_market_packet(recv - 5_000_000, recv).unwrap();
    assert!(!g.snapshot().latency_open);
}
#[test]
fn latency_over_150ms_trips_and_recovery_requires_new_fifty() {
    let mut g = ready_gate();
    let r = NOW + 300_000_000;
    assert!(
        g.on_market_packet(r - 151_000_000, r)
            .unwrap()
            .cancel_resting_entries
    );
    assert!(g.snapshot().latency_open);
    assert_eq!(g.snapshot().latency_recovery_streak, 0);
    for i in 1..50 {
        let recv = r + i * 1_000_000;
        g.on_market_packet(recv - 5_000_000, recv).unwrap();
    }
    assert!(g.snapshot().latency_open);
    let recv = r + 50 * 1_000_000;
    g.on_market_packet(recv - 5_000_000, recv).unwrap();
    assert!(!g.snapshot().latency_open);
}
#[test]
fn exactly_25ms_is_not_a_recovery_sample() {
    let mut l = LatencyCircuit::new();
    l.observe(1_000_000, 26_000_000).unwrap();
    assert_eq!(l.state().recovery_streak, 0);
    l.observe(25_000_001, 27_000_000).unwrap();
    assert_eq!(l.state().recovery_streak, 1);
}
#[test]
fn reversed_packet_timestamp_fails_closed_and_resets_streak() {
    let mut g = ready_gate();
    assert!(g.on_market_packet(100_000_000, 50_000_000).is_err());
    assert!(g.snapshot().latency_open);
    assert!(g.on_market_packet(500_000_000, 40_000_000).is_err());
}
#[test]
fn cold_gate_blocks_valid_entry_before_feed_recovery() {
    let mut g = RiskGate::new(RiskPolicy::default()).unwrap();
    let o = order(VenueId::BinanceUsdM, 0, 1);
    let d = g
        .validate_entry(&o, &context(VenueId::BinanceUsdM), wallet(), &operator())
        .unwrap();
    assert_eq!(d, GateDecision::Rejected(RiskReason::LatencyCircuitOpen));
}
#[test]
fn fully_attested_short_is_only_a_preliminary_proposal() {
    let mut g = ready_gate();
    let q = proposed(
        g.validate_entry(
            &order(VenueId::BinanceUsdM, 0, 1),
            &context(VenueId::BinanceUsdM),
            wallet(),
            &operator(),
        )
        .unwrap(),
    );
    assert_eq!(q, qty(1).0);
    assert_eq!(g.snapshot().last_approved_order_id, 1);
}
#[test]
fn funding_basis_blocks_a_c_and_quarters_b_at_strict_threshold() {
    let mut g = ready_gate();
    let mut c = context(VenueId::BinanceUsdM);
    c.market.funding_8h_bps_scaled = -4 * SCALE;
    c.market.perp_index_basis_bps_scaled = -16 * SCALE;
    let op = operator();
    for s in [0, 2] {
        assert_eq!(
            g.validate_entry(&order(VenueId::BinanceUsdM, s, 1), &c, wallet(), &op)
                .unwrap(),
            GateDecision::Rejected(RiskReason::FundingBasisSqueeze)
        );
    }
    assert_eq!(
        proposed(
            g.validate_entry(&order(VenueId::BinanceUsdM, 1, 2), &c, wallet(), &op)
                .unwrap()
        ),
        qty(1).0 / 4
    );
}
#[test]
fn funding_breaker_does_not_trigger_if_only_one_negative() {
    let mut g = ready_gate();
    let mut c = context(VenueId::BinanceUsdM);
    c.market.funding_8h_bps_scaled = -4 * SCALE;
    c.market.perp_index_basis_bps_scaled = -14 * SCALE;
    assert_eq!(
        proposed(
            g.validate_entry(
                &order(VenueId::BinanceUsdM, 0, 1),
                &c,
                wallet(),
                &operator()
            )
            .unwrap()
        ),
        qty(1).0
    );
}
#[test]
fn missing_or_future_funding_is_rejected_on_perpetuals() {
    let mut g = ready_gate();
    let mut c = context(VenueId::BinanceUsdM);
    c.market.funding_asof_ns = 0;
    assert_eq!(
        g.validate_entry(
            &order(VenueId::BinanceUsdM, 0, 1),
            &c,
            wallet(),
            &operator()
        )
        .unwrap(),
        GateDecision::Rejected(RiskReason::StaleFundingBasis)
    );
    c.market.funding_asof_ns = NOW + 1;
    assert_eq!(
        g.validate_entry(
            &order(VenueId::BinanceUsdM, 0, 1),
            &c,
            wallet(),
            &operator()
        )
        .unwrap(),
        GateDecision::Rejected(RiskReason::StaleFundingBasis)
    );
}
#[test]
fn unsafe_account_sources_and_unreserved_orders_are_denied() {
    let mut g = ready_gate();
    let o = order(VenueId::BinanceUsdM, 0, 1);
    let mut c = context(VenueId::BinanceUsdM);
    c.account.reconciled = false;
    assert_eq!(
        g.validate_entry(&o, &c, wallet(), &operator()).unwrap(),
        GateDecision::Rejected(RiskReason::AccountUnreconciled)
    );
    c.account.reconciled = true;
    c.account.reservations_current = false;
    assert_eq!(
        g.validate_entry(&o, &c, wallet(), &operator()).unwrap(),
        GateDecision::Rejected(RiskReason::MissingReservations)
    );
    c.account.reservations_current = true;
    c.market.certified_event_provenance = false;
    assert_eq!(
        g.validate_entry(&o, &c, wallet(), &operator()).unwrap(),
        GateDecision::Rejected(RiskReason::UncertifiedSource)
    );
}
#[test]
fn unresolved_reconciliation_or_missing_reservations_cancel_entries() {
    let mut g = ready_gate();
    let op = operator();
    let mut c = context(VenueId::BybitLinear);
    c.account.reconciled = false;
    assert!(g.monitor(&c, wallet(), &op).unwrap().cancel_resting_entries);
    c.account.reconciled = true;
    c.account.reservations_current = false;
    assert!(g.monitor(&c, wallet(), &op).unwrap().cancel_resting_entries);
}
#[test]
fn forged_order_timestamps_tif_or_negative_hawkes_intensity_are_rejected() {
    let mut g = ready_gate();
    let op = operator();
    let c = context(VenueId::BybitLinear);
    let mut o = order(VenueId::BybitLinear, 0, 1);
    o.created_at_ns = NOW + 1;
    assert_eq!(
        g.validate_entry(&o, &c, wallet(), &op).unwrap(),
        GateDecision::Rejected(RiskReason::BadOrder)
    );
    o.created_at_ns = NOW;
    o.time_in_force = 255;
    assert_eq!(
        g.validate_entry(&o, &c, wallet(), &op).unwrap(),
        GateDecision::Rejected(RiskReason::BadOrder)
    );
    o.time_in_force = TimeInForce::Gtc as u8;
    let mut invalid = c;
    invalid.market.isr_up_scaled = -1;
    assert_eq!(
        g.validate_entry(&o, &invalid, wallet(), &op).unwrap(),
        GateDecision::Rejected(RiskReason::BadOrder)
    );
}
#[test]
fn latency_or_source_outage_requests_cancel_resting_entries() {
    let mut g = ready_gate();
    let mut c = context(VenueId::BinanceUsdM);
    c.market.synchronized = false;
    assert!(
        g.monitor(&c, wallet(), &operator())
            .unwrap()
            .cancel_resting_entries
    );
    let d = g.mark_connection_lost();
    assert!(d.cancel_resting_entries);
    assert!(g.snapshot().latency_open);
}
#[test]
fn session_drawdown_latches_atomic_operator_halt_and_flatten_directive() {
    let mut g = ready_gate();
    let op = operator();
    let mut w = wallet();
    w.active_equity = w.session_floor;
    let c = context(VenueId::BinanceUsdM);
    let d = g.monitor(&c, w, &op).unwrap();
    assert!(d.halt_all && d.seal_profit_vault && d.require_flatten_all);
    assert!(d.cancel_resting_entries);
    assert_eq!(op.load().unwrap().mode, TradingMode::HaltAll);
    assert_eq!(
        g.validate_entry(&order(VenueId::BinanceUsdM, 0, 1), &c, wallet(), &op)
            .unwrap(),
        GateDecision::Rejected(RiskReason::DrawdownFloor)
    );
}
#[test]
fn squeeze_detection_halts_all_new_entries_and_requests_close() {
    let mut g = ready_gate();
    let op = operator();
    let mut c = context(VenueId::BybitLinear);
    c.market.isr_up_scaled = 6 * SCALE;
    c.market.ofi_z_scaled = 3 * SCALE;
    let d = g.monitor(&c, wallet(), &op).unwrap();
    assert!(d.require_reduce_only_short_cover && d.halt_all);
    assert!(!d.require_flatten_all);
    assert_eq!(op.load().unwrap().mode, TradingMode::HaltAll);
    assert_eq!(
        g.validate_entry(&order(VenueId::BybitLinear, 0, 1), &c, wallet(), &op)
            .unwrap(),
        GateDecision::Rejected(RiskReason::DrawdownFloor)
    );
}
#[test]
fn cover_during_halt_is_reduce_only_and_limited_by_confirmed_short() {
    let mut g = ready_gate();
    let c = context(VenueId::BybitLinear);
    let op = operator();
    op.halt().unwrap();
    assert_eq!(
        g.validate_cover(&cover(VenueId::BybitLinear, 5), &c),
        GateDecision::ProposedCover {
            approved_qty: qty(5)
        }
    );
    assert_eq!(
        g.validate_cover(&cover(VenueId::BybitLinear, 6), &c),
        GateDecision::Rejected(RiskReason::NoConfirmedShort)
    );
    let _ = g.mark_connection_lost();
    assert_eq!(
        g.validate_cover(&cover(VenueId::BybitLinear, 1), &c),
        GateDecision::ProposedCover {
            approved_qty: qty(1)
        }
    );
}
#[test]
fn equities_require_locate_and_reject_ssr_reprice() {
    let mut g = ready_gate();
    let mut c = context(VenueId::Equities);
    c.equities.located_for_open_short = false;
    assert_eq!(
        g.validate_entry(&order(VenueId::Equities, 0, 1), &c, wallet(), &operator())
            .unwrap(),
        GateDecision::Rejected(RiskReason::ShortLocateMissing)
    );
    c.equities.located_for_open_short = true;
    c.equities.short_sale_restriction_active = true;
    assert_eq!(
        g.validate_entry(&order(VenueId::Equities, 0, 1), &c, wallet(), &operator())
            .unwrap(),
        GateDecision::Rejected(RiskReason::ShortSalePriceRestricted)
    );
    c.equities.short_sale_restriction_active = false;
    assert_eq!(
        proposed(
            g.validate_entry(&order(VenueId::Equities, 0, 2), &c, wallet(), &operator())
                .unwrap()
        ),
        qty(1).0
    );
}
#[test]
fn forex_blackout_denies_entry_but_not_a_reconciled_cover() {
    let mut g = ready_gate();
    let mut c = context(VenueId::Forex);
    c.account.rollover_or_policy_blackout = true;
    assert_eq!(
        g.validate_entry(&order(VenueId::Forex, 0, 1), &c, wallet(), &operator())
            .unwrap(),
        GateDecision::Rejected(RiskReason::ComplianceBlackout)
    );
    assert_eq!(
        g.validate_cover(&cover(VenueId::Forex, 1), &c),
        GateDecision::ProposedCover {
            approved_qty: qty(1)
        }
    );
}
#[test]
fn leverage_and_margin_headroom_are_hard_denials() {
    let mut g = ready_gate();
    let mut c = context(VenueId::BybitLinear);
    c.account.gross_open_and_pending_usd = usd(2100);
    assert_eq!(
        g.validate_entry(
            &order(VenueId::BybitLinear, 0, 1),
            &c,
            wallet(),
            &operator()
        )
        .unwrap(),
        GateDecision::Rejected(RiskReason::ExceedsGrossLimit)
    );
    c.account.gross_open_and_pending_usd = usd(0);
    c.account.maintenance_margin_headroom_usd = usd(0);
    assert_eq!(
        g.validate_entry(
            &order(VenueId::BybitLinear, 0, 1),
            &c,
            wallet(),
            &operator()
        )
        .unwrap(),
        GateDecision::Rejected(RiskReason::InsufficientCollateral)
    );
}
#[test]
fn invalid_stop_and_direction_fail_closed() {
    let mut g = ready_gate();
    let mut o = order(VenueId::BybitLinear, 0, 1);
    o.stop_price = o.limit_price;
    assert_eq!(
        g.validate_entry(&o, &context(VenueId::BybitLinear), wallet(), &operator())
            .unwrap(),
        GateDecision::Rejected(RiskReason::InvalidOrderType)
    );
    o.stop_price = price(101).0;
    o.side = OrderSide::Buy as u8;
    assert_eq!(
        g.validate_entry(&o, &context(VenueId::BybitLinear), wallet(), &operator())
            .unwrap(),
        GateDecision::Rejected(RiskReason::InvalidOrderType)
    );
}
#[test]
fn replayed_order_id_is_not_authorized_twice() {
    let mut g = ready_gate();
    let op = operator();
    let c = context(VenueId::BybitLinear);
    proposed(
        g.validate_entry(&order(VenueId::BybitLinear, 0, 1), &c, wallet(), &op)
            .unwrap(),
    );
    assert_eq!(
        g.validate_entry(&order(VenueId::BybitLinear, 0, 1), &c, wallet(), &op)
            .unwrap(),
        GateDecision::Rejected(RiskReason::ReplayOrderId)
    );
}
#[test]
fn invalid_operator_and_symbol_are_denied_without_token_spend() {
    let mut g = ready_gate();
    let op = OperatorSnapshot {
        strategy_enable_mask: 0,
        ..OperatorSnapshot::default()
    };
    let a = AtomicOperatorState::new(op).unwrap();
    assert_eq!(
        g.validate_entry(
            &order(VenueId::BinanceUsdM, 0, 1),
            &context(VenueId::BinanceUsdM),
            wallet(),
            &a
        )
        .unwrap(),
        GateDecision::Rejected(RiskReason::StrategyDisabled)
    );
}
#[test]
fn token_bucket_has_fractional_refills_and_no_negative_balance() {
    let mut t = TokenBucket::new(2, 2).unwrap();
    assert!(t.take(1_000).unwrap());
    assert!(t.take(1_000).unwrap());
    assert!(!t.take(1_000).unwrap());
    assert!(!t.take(250_001_000).unwrap());
    assert!(t.take(500_001_000).unwrap());
    assert!(!t.take(500_001_000).unwrap());
    assert!(t.take(1_000_001_000).unwrap());
}
#[test]
fn token_bucket_rejects_clock_regression() {
    let mut t = TokenBucket::new(1, 1).unwrap();
    t.take(100).unwrap();
    assert!(t.take(99).is_err());
}
#[test]
fn risk_policy_rejects_unbounded_limits() {
    let mut p = RiskPolicy {
        burst_capacity: 0,
        ..RiskPolicy::default()
    };
    assert!(RiskGate::new(p).is_err());
    p.burst_capacity = 10;
    p.max_quote_age_ns = 999_999_999;
    assert!(RiskGate::new(p).is_err());
}
#[test]
fn gate_never_accepts_non_paper_environment() {
    let mut g = ready_gate();
    let mut c = context(VenueId::BybitLinear);
    c.paper_environment = false;
    assert_eq!(
        g.validate_entry(
            &order(VenueId::BybitLinear, 0, 1),
            &c,
            wallet(),
            &operator()
        )
        .unwrap(),
        GateDecision::Rejected(RiskReason::InvalidPolicy)
    );
    assert_eq!(
        g.validate_cover(&cover(VenueId::BybitLinear, 1), &c),
        GateDecision::Rejected(RiskReason::InvalidPolicy)
    );
}
