//! Paper-only bridge between the independent Rust risk gate and the exact
//! exchange wire contract. This is deliberately placed inside protocol-lab,
//! never in the authenticated execution adapter.
use crate::reservations::{PaperReservationJournal, Reservation};
use trading_core_types::{
    AtomicOperatorState, FixedPrice, FixedQty, FixedUsd, OrderIntent, OrderSide as IntentSide,
    TimeInForce, VenueId, checked_notional,
};
use trading_execution::{ExchangeOrder, OrderKind, OrderSide, PrepareOrder};
use trading_market_data::InstrumentSpecTable;
use trading_risk_gate::{GateDecision, RiskContext, RiskGate, RiskReason};
use trading_wallet_manager::WalletSnapshot;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResearchPlanError {
    NotPaper,
    UnsupportedMarket,
    InvalidSymbol,
    InvalidIntent,
    Rejected(RiskReason),
    RiskFailed,
    QuantityChanged,
    NotionalMismatch,
    ExchangeFilters,
    ReservationMismatch,
    ReservationFailed,
}

/// Qualification-only proposed order. This type contains no live routing
/// handle; no credential, exchange socket, or filled-wallet mutation exists.
pub struct PaperOrderPlan {
    pub order: ExchangeOrder,
    pub modeled_qty: FixedQty,
    pub modeled_notional: FixedUsd,
}

/// A conservative seam: never upgrade a risk proposal into authorization.
/// All evidence must be explicitly tagged as synthetic PAPER evidence.
/// This function is not called from the real production execution gateway.
pub fn plan_paper_entry(
    gate: &mut RiskGate,
    intent: &OrderIntent,
    ctx: &RiskContext,
    wallet: WalletSnapshot,
    operator: &AtomicOperatorState,
    table: &InstrumentSpecTable,
) -> Result<PaperOrderPlan, ResearchPlanError> {
    if !ctx.paper_environment {
        return Err(ResearchPlanError::NotPaper);
    }
    if ctx.market.venue != VenueId::BinanceUsdM
        || intent.venue_id != VenueId::BinanceUsdM as u8
        || intent.symbol_id != ctx.market.symbol_id
    {
        return Err(ResearchPlanError::UnsupportedMarket);
    }
    // A lab fixture cannot use an unconfigured symbol or mismatched table
    // index to build a live-looking signed order.
    let instrument = table
        .by_id(intent.symbol_id)
        .ok_or(ResearchPlanError::InvalidSymbol)?;
    if instrument.symbol_id != intent.symbol_id {
        return Err(ResearchPlanError::InvalidSymbol);
    }
    if intent.side != IntentSide::Sell as u8
        || intent.is_reduce_only()
        || intent.time_in_force != TimeInForce::Ioc as u8
        || intent.limit_price <= 0
        || intent.quantity <= 0
    {
        return Err(ResearchPlanError::InvalidIntent);
    }
    // Qualify venue filters BEFORE consuming a gate token or accepting an
    // order ID. A malformed price/quantity must never advance RiskGate.
    let from_price = FixedPrice(intent.limit_price);
    let order = PrepareOrder {
        client_id: intent.client_order_id,
        symbol_id: intent.symbol_id,
        side: OrderSide::Sell,
        kind: OrderKind::IocLimit,
        quantity: FixedQty(intent.quantity),
        limit: Some(from_price),
    }
    .prepare(&instrument.symbol, instrument.spec)
    .map_err(|_| ResearchPlanError::ExchangeFilters)?;
    let decision = gate
        .validate_entry(intent, ctx, wallet, operator)
        .map_err(|_| ResearchPlanError::RiskFailed)?;
    let (approved_qty, approved_notional) = match decision {
        GateDecision::ProposedEntry {
            approved_qty,
            approved_notional,
        } => (approved_qty, approved_notional),
        GateDecision::Rejected(reason) => return Err(ResearchPlanError::Rejected(reason)),
        GateDecision::ProposedCover { .. } => return Err(ResearchPlanError::InvalidIntent),
    };
    // A squeeze-related reduction is a new proposed quantity, not an
    // authorization to silently execute a different order.
    if approved_qty.0 != intent.quantity {
        return Err(ResearchPlanError::QuantityChanged);
    }
    let notional = checked_notional(from_price, approved_qty)
        .map_err(|_| ResearchPlanError::NotionalMismatch)?;
    if notional != approved_notional {
        return Err(ResearchPlanError::NotionalMismatch);
    }
    let Some(order_qty) = order.params.get("quantity").and_then(|v| v.as_str()) else {
        return Err(ResearchPlanError::ExchangeFilters);
    };
    let parsed_qty = order_qty
        .parse::<FixedQty>()
        .map_err(|_| ResearchPlanError::ExchangeFilters)?;
    if parsed_qty != approved_qty
        || order.params["symbol"] != instrument.symbol
        || order.params["timeInForce"] != "IOC"
    {
        return Err(ResearchPlanError::ExchangeFilters);
    }
    Ok(PaperOrderPlan {
        order,
        modeled_qty: approved_qty,
        modeled_notional: approved_notional,
    })
}

/// Persist a paper-only, never-released gross exposure claim after a
/// separately returned paper risk proposal. An ACK does not free capacity.
/// This is not a broker reservation and must not be called by a live router.
pub fn reserve_paper_plan(
    journal: &mut PaperReservationJournal,
    intent: &OrderIntent,
    plan: &PaperOrderPlan,
) -> Result<Reservation, ResearchPlanError> {
    if plan.order.client_id != intent.client_order_id
        || plan.modeled_qty.0 != intent.quantity
        || plan.order.params["newClientOrderId"] != format!("tl-{}", intent.client_order_id)
        || plan.order.params["timeInForce"] != "IOC"
        || intent.is_reduce_only()
        || intent.side != IntentSide::Sell as u8
        || intent.venue_id != VenueId::BinanceUsdM as u8
    {
        return Err(ResearchPlanError::ReservationMismatch);
    }
    let expected = checked_notional(FixedPrice(intent.limit_price), plan.modeled_qty)
        .map_err(|_| ResearchPlanError::ReservationMismatch)?;
    if expected != plan.modeled_notional {
        return Err(ResearchPlanError::ReservationMismatch);
    }
    journal
        .reserve(
            intent.client_order_id,
            intent.symbol_id,
            plan.modeled_qty,
            FixedPrice(intent.limit_price),
        )
        .map_err(|_| ResearchPlanError::ReservationFailed)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::atomic::{AtomicU64, Ordering};
    use trading_core_types::{ComplianceContext, FixedUsd, OperatorSnapshot, SCALE};
    use trading_risk_gate::{AccountEvidence, MarketEvidence, RiskPolicy};
    use trading_strategy_engine::{OrderEvent, OrderEventKind, OrderStatus, OrderTracker};
    use trading_wallet_manager::WalletBuckets;

    static NEXT_JOURNAL: AtomicU64 = AtomicU64::new(1);
    fn journal_path() -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "trading-risk-reserve-{}-{}.log",
            std::process::id(),
            NEXT_JOURNAL.fetch_add(1, Ordering::Relaxed)
        ))
    }
    fn table() -> InstrumentSpecTable {
        let data = json!({"symbols":[{"symbol":"BTCUSDT","status":"TRADING",
        "contractType":"PERPETUAL","quoteAsset":"USDT","marginAsset":"USDT",
        "filters":[
            {"filterType":"PRICE_FILTER","tickSize":"0.10","minPrice":"0.10","maxPrice":"1000000"},
            {"filterType":"LOT_SIZE","stepSize":"0.001","minQty":"0.001","maxQty":"100"},
            {"filterType":"MARKET_LOT_SIZE","stepSize":"0.001","minQty":"0.001","maxQty":"100"},
            {"filterType":"MIN_NOTIONAL","notional":"5"}
        ]}]});
        InstrumentSpecTable::from_exchange_info(
            &data,
            &["BTCUSDT".into()],
            trading_market_data::BinanceEnvironment::Testnet,
        )
        .unwrap()
    }
    fn gate(qualified: bool) -> RiskGate {
        let mut g = RiskGate::new(RiskPolicy::default()).unwrap();
        if qualified {
            for i in 0..50_u64 {
                let received = 1_000_000_000 + i * 1_000_000;
                g.on_market_packet(received - 5_000_000, received).unwrap();
            }
            assert!(!g.snapshot().latency_open);
        }
        g
    }
    fn context() -> RiskContext {
        let now = 1_050_000_000_u64;
        RiskContext {
            market: MarketEvidence {
                now_ns: now,
                quote_received_ns: now - 1_000_000,
                synchronized: true,
                certified_event_provenance: true,
                venue: VenueId::BinanceUsdM,
                symbol_id: 0,
                funding_8h_bps_scaled: 0,
                perp_index_basis_bps_scaled: 0,
                funding_asof_ns: now,
                basis_asof_ns: now,
                isr_up_scaled: SCALE,
                ofi_z_scaled: 0,
            },
            account: AccountEvidence {
                reconciled: true,
                reservations_current: true,
                gross_open_and_pending_usd: FixedUsd::ZERO,
                confirmed_short_qty: FixedQty::ZERO,
                maintenance_margin_headroom_usd: FixedUsd(50_000 * SCALE),
                rollover_or_policy_blackout: false,
            },
            equities: ComplianceContext {
                national_best_bid: 0,
                tick_size: 0,
                short_sale_restriction_active: false,
                located_for_open_short: false,
                trading_blackout: false,
            },
            paper_environment: true,
        }
    }
    fn intent() -> OrderIntent {
        OrderIntent::new(
            42,
            1_049_000_000,
            FixedPrice(50_000 * SCALE + 10_000_000),
            FixedQty(2_000_000),
            Some(FixedPrice(50_010 * SCALE + 10_000_000)),
            FixedUsd(SCALE),
            0,
            15,
            VenueId::BinanceUsdM,
            1,
            IntentSide::Sell,
            TimeInForce::Ioc,
            false,
        )
        .unwrap()
    }
    fn wallet() -> WalletSnapshot {
        WalletBuckets::new(FixedUsd(10_000 * SCALE))
            .unwrap()
            .snapshot()
            .unwrap()
    }
    #[test]
    fn real_risk_gate_wallet_and_exact_exchange_order_contract_are_connected() {
        let mut g = gate(true);
        let op = AtomicOperatorState::new(OperatorSnapshot::default()).unwrap();
        let p = plan_paper_entry(&mut g, &intent(), &context(), wallet(), &op, &table()).unwrap();
        assert_eq!(p.order.client_id, 42);
        assert_eq!(p.order.params["symbol"], "BTCUSDT");
        assert_eq!(p.order.params["quantity"], "0.02");
        assert_eq!(p.order.params["price"], "50000.1");
        assert_eq!(p.modeled_notional.0, 100_000_200_000);
        assert_eq!(g.snapshot().last_approved_order_id, 42);
    }
    #[test]
    fn paper_only_and_unreconciled_evidence_never_produce_order_contract() {
        let op = AtomicOperatorState::new(OperatorSnapshot::default()).unwrap();
        let mut ctx = context();
        ctx.paper_environment = false;
        let mut g = gate(true);
        assert!(matches!(
            plan_paper_entry(&mut g, &intent(), &ctx, wallet(), &op, &table()),
            Err(ResearchPlanError::NotPaper)
        ));
        assert_eq!(g.snapshot().last_approved_order_id, 0);
        let mut ctx = context();
        ctx.account.reconciled = false;
        assert!(matches!(
            plan_paper_entry(&mut g, &intent(), &ctx, wallet(), &op, &table()),
            Err(ResearchPlanError::Rejected(RiskReason::AccountUnreconciled))
        ));
        assert_eq!(g.snapshot().last_approved_order_id, 0);
    }
    #[test]
    fn stale_books_and_uncalibrated_latency_are_blocked() {
        let op = AtomicOperatorState::new(OperatorSnapshot::default()).unwrap();
        let ctx = context();
        let mut cold = gate(false);
        assert!(matches!(
            plan_paper_entry(&mut cold, &intent(), &ctx, wallet(), &op, &table()),
            Err(ResearchPlanError::Rejected(RiskReason::LatencyCircuitOpen))
        ));
        let mut ctx = context();
        ctx.market.synchronized = false;
        let mut g = gate(true);
        assert!(matches!(
            plan_paper_entry(&mut g, &intent(), &ctx, wallet(), &op, &table()),
            Err(ResearchPlanError::Rejected(RiskReason::UnsynchronizedFeed))
        ));
    }
    #[test]
    fn squeeze_reduction_requires_reapproval_not_silent_routing() {
        let op = AtomicOperatorState::new(OperatorSnapshot::default()).unwrap();
        let mut ctx = context();
        ctx.market.funding_8h_bps_scaled = -4 * SCALE;
        ctx.market.perp_index_basis_bps_scaled = -16 * SCALE;
        let mut g = gate(true);
        assert!(matches!(
            plan_paper_entry(&mut g, &intent(), &ctx, wallet(), &op, &table()),
            Err(ResearchPlanError::QuantityChanged)
        ));
    }
    #[test]
    fn exchange_filters_reject_bad_tick_even_after_risk_proposal() {
        let op = AtomicOperatorState::new(OperatorSnapshot::default()).unwrap();
        let mut i = intent();
        i.limit_price += 1;
        let mut g = gate(true);
        assert!(matches!(
            plan_paper_entry(&mut g, &i, &context(), wallet(), &op, &table()),
            Err(ResearchPlanError::ExchangeFilters)
        ));
    }
    #[test]
    fn no_wallet_fill_on_ack_only_execution_report_can_change_order_tracker() {
        let before = wallet();
        let mut tracker = OrderTracker::new(42, FixedQty(2_000_000)).unwrap();
        tracker.sent().unwrap();
        tracker
            .apply(OrderEvent {
                client_id: 42,
                sequence: 1,
                event: OrderEventKind::Ack,
            })
            .unwrap();
        assert_eq!(tracker.status(), OrderStatus::Acknowledged);
        assert_eq!(tracker.cumulative_filled(), FixedQty::ZERO);
        assert_eq!(wallet(), before);
        tracker
            .apply(OrderEvent {
                client_id: 42,
                sequence: 2,
                event: OrderEventKind::CumulativeFill(FixedQty(1_000_000)),
            })
            .unwrap();
        assert_eq!(tracker.cumulative_filled(), FixedQty(1_000_000));
        assert_eq!(wallet(), before);
    }
    #[test]
    fn paper_gate_proposal_is_durably_reserved_before_any_possible_send() {
        let path = journal_path();
        let mut journal = PaperReservationJournal::create(&path, 2000 * SCALE).unwrap();
        let mut g = gate(true);
        let op = AtomicOperatorState::new(OperatorSnapshot::default()).unwrap();
        let original = intent();
        let plan =
            plan_paper_entry(&mut g, &original, &context(), wallet(), &op, &table()).unwrap();
        let proof = reserve_paper_plan(&mut journal, &original, &plan).unwrap();
        assert_eq!(proof.client_id, 42);
        assert_eq!(proof.notional_raw, plan.modeled_notional.0);
        assert_eq!(journal.snapshot().gross_reserved_raw, proof.notional_raw);
        let original_bytes = std::fs::read(&path).unwrap();
        assert!(original_bytes.ends_with(b"\n"));
        drop(journal);
        let inspected = PaperReservationJournal::inspect(&path).unwrap();
        assert!(inspected.snapshot().inspect_only);
        assert_eq!(inspected.snapshot().gross_reserved_raw, proof.notional_raw);
        std::fs::remove_file(path).unwrap();
    }
    #[test]
    fn invalid_exchange_tick_does_not_consume_risk_approval() {
        let mut i = intent();
        i.limit_price += 1;
        let op = AtomicOperatorState::new(OperatorSnapshot::default()).unwrap();
        let mut g = gate(true);
        assert!(matches!(
            plan_paper_entry(&mut g, &i, &context(), wallet(), &op, &table()),
            Err(ResearchPlanError::ExchangeFilters)
        ));
        assert_eq!(g.snapshot().last_approved_order_id, 0);
    }
}
