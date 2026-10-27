use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use trading_core_types::{
    FixedPrice, FixedQty, FixedUsd, OrderIntent, OrderSide, SCALE, TimeInForce, VenueId,
};
use trading_strategy_engine::OrderStatus;
use trading_paper_gateway::{
    Journal, JournalError, OfflinePaperSession, PaperError, PaperQuote, PaperStatus, RecordKind,
    run_demo,
};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Scratch(PathBuf);
impl Scratch {
    fn new() -> Self {
        let id = NEXT.fetch_add(1, Ordering::Relaxed);
        Self(std::env::temp_dir().join(format!("rust-part6-paper-{}-{id}", std::process::id())))
    }
    fn path(&self) -> &Path {
        &self.0
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}
fn quote() -> PaperQuote {
    PaperQuote {
        seq: 1,
        exchange_ns: 995_000_000,
        recv_ns: 1_000_000_000,
        best_bid: FixedPrice(100 * SCALE),
        best_ask: FixedPrice(100 * SCALE + SCALE / 10),
        bid_quantity: FixedQty(60_000_000),
        ask_quantity: FixedQty(60_000_000),
    }
}
fn entry(id: u64) -> OrderIntent {
    OrderIntent::new(
        id,
        1_000_000_000,
        FixedPrice(100 * SCALE),
        FixedQty(SCALE),
        Some(FixedPrice(101 * SCALE)),
        FixedUsd(5 * SCALE),
        0,
        5,
        VenueId::Equities,
        0,
        OrderSide::Sell,
        TimeInForce::Ioc,
        false,
    )
    .unwrap()
}
fn session(path: &Path) -> OfflinePaperSession {
    let mut p = OfflinePaperSession::create(path).unwrap();
    p.initialize_synthetic_stream(quote()).unwrap();
    p.arm().unwrap();
    p
}
#[test]
fn synthetic_demo_round_trip_has_no_pending_and_positive_net_settlement() {
    let tmp = Scratch::new();
    let result = run_demo(tmp.path()).unwrap();
    assert_eq!(result.journal_events, 7);
    assert_eq!(result.entry_filled.0, 60_000_000);
    assert_eq!(result.cover_filled, result.entry_filled);
    assert!(result.realized_net_pnl.0 > 0);
    let j = Journal::inspect(tmp.path()).unwrap();
    assert_eq!(j.summary().records, 7);
    assert_eq!(j.summary().confirmed_short_qty_raw, 0);
    assert_eq!(j.summary().realized_net_pnl_raw, result.realized_net_pnl.0);
    assert!(j.summary().halted);
    assert!(j.is_inspect_only());
}
#[test]
fn existing_journal_never_restarts_automatically() {
    let tmp = Scratch::new();
    run_demo(tmp.path()).unwrap();
    assert!(OfflinePaperSession::create(tmp.path()).is_err());
}
#[test]
fn truncation_is_detected_and_cannot_auto_resume() {
    let tmp = Scratch::new();
    run_demo(tmp.path()).unwrap();
    let mut bytes = std::fs::read(tmp.path()).unwrap();
    bytes.pop();
    std::fs::write(tmp.path(), bytes).unwrap();
    assert!(Journal::inspect(tmp.path()).is_err());
}
#[test]
fn record_byte_tampering_is_detected() {
    let tmp = Scratch::new();
    run_demo(tmp.path()).unwrap();
    let mut bytes = std::fs::read(tmp.path()).unwrap();
    let offset = bytes.iter().position(|x| *x == b'|').unwrap() + 1;
    let offset = offset + bytes[offset..].iter().position(|x| *x == b'|').unwrap() + 1;
    bytes[offset] = if bytes[offset] == b'1' { b'2' } else { b'1' };
    std::fs::write(tmp.path(), bytes).unwrap();
    assert!(Journal::inspect(tmp.path()).is_err());
}
#[test]
fn journal_inspect_cannot_append_or_route() {
    let tmp = Scratch::new();
    run_demo(tmp.path()).unwrap();
    let mut inspected = Journal::inspect(tmp.path()).unwrap();
    assert!(matches!(
        inspected.append(RecordKind::EmergencyHalt, 0, 0, 0, 0, 0),
        Err(JournalError::InspectOnly),
    ));
}
#[test]
fn open_pending_order_is_visible_on_crash_recovery() {
    let tmp = Scratch::new();
    {
        let mut p = session(tmp.path());
        p.reserve_short(&entry(1)).unwrap();
        assert_eq!(p.summary().unresolved_order(), Some(1));
    }
    let j = Journal::inspect(tmp.path()).unwrap();
    assert_eq!(j.summary().unresolved_order(), Some(1));
    assert_eq!(j.summary().pending_quantity_raw(), SCALE);
    assert!(j.is_inspect_only());
}
#[test]
fn entry_is_denied_before_operator_arm() {
    let tmp = Scratch::new();
    let mut p = OfflinePaperSession::create(tmp.path()).unwrap();
    p.initialize_synthetic_stream(quote()).unwrap();
    assert_eq!(p.status(), PaperStatus::Disarmed);
    assert!(matches!(
        p.reserve_short(&entry(1)),
        Err(PaperError::NotArmed)
    ));
}
#[test]
fn paper_source_disallows_reversed_time_or_crossed_quote() {
    let tmp = Scratch::new();
    let mut p = session(tmp.path());
    assert!(p.observe_quote(quote()).is_err());
    let mut invalid = quote();
    invalid.seq = 2;
    invalid.recv_ns = quote().recv_ns + 1;
    invalid.best_ask = invalid.best_bid;
    assert!(p.observe_quote(invalid).is_err());
}
#[test]
fn entry_then_full_cancel_leaves_no_short_position() {
    let tmp = Scratch::new();
    let mut p = session(tmp.path());
    p.reserve_short(&entry(1)).unwrap();
    p.confirm_paper_cancel(1).unwrap();
    assert_eq!(p.summary().confirmed_short_qty_raw, 0);
    assert_eq!(p.summary().unresolved_order(), None);
    p.seal_flat_session().unwrap();
    assert!(Journal::inspect(tmp.path()).unwrap().summary().halted);
}
#[test]
fn partial_entry_fill_reserves_remainder_until_explicit_cancel() {
    let tmp = Scratch::new();
    let mut p = session(tmp.path());
    p.reserve_short(&entry(1)).unwrap();
    assert_eq!(p.order_status(), Some(OrderStatus::Acknowledged));
    p.simulate_fill(1, FixedQty(30_000_000)).unwrap();
    assert_eq!(p.order_status(), Some(OrderStatus::Acknowledged));
    assert_eq!(p.summary().confirmed_short_qty_raw, 30_000_000);
    assert_eq!(p.summary().pending_quantity_raw(), 70_000_000);
    assert!(p.reserve_short(&entry(2)).is_err());
    p.confirm_paper_cancel(1).unwrap();
    assert_eq!(p.order_status(), None);
    assert_eq!(p.summary().unresolved_order(), None);
    assert_eq!(p.summary().confirmed_short_qty_raw, 30_000_000);
}
#[test]
fn fill_cannot_exceed_event_liquidity_or_pending_order() {
    let tmp = Scratch::new();
    let mut p = session(tmp.path());
    p.reserve_short(&entry(1)).unwrap();
    assert!(matches!(
        p.simulate_fill(1, FixedQty(SCALE)),
        Err(PaperError::NoFill)
    ));
    assert!(p.simulate_fill(1, FixedQty(70_000_000)).is_err());
    assert_eq!(p.summary().confirmed_short_qty_raw, 0);
    assert_eq!(p.summary().pending_quantity_raw(), SCALE);
}
#[test]
fn close_orders_require_confirmed_short_and_reduce_only_direction() {
    let tmp = Scratch::new();
    let mut p = session(tmp.path());
    let mut wrong = entry(2);
    wrong.side = OrderSide::Buy as u8;
    assert!(p.reserve_cover(&wrong).is_err());
    assert_eq!(p.summary().confirmed_short_qty_raw, 0);
}
#[test]
fn simulator_halt_blocks_new_entries_but_allows_covers() {
    let tmp = Scratch::new();
    let mut p = session(tmp.path());
    p.reserve_short(&entry(1)).unwrap();
    p.simulate_fill(1, FixedQty(60_000_000)).unwrap();
    p.confirm_paper_cancel(1).unwrap();
    p.halt_entries();
    assert_eq!(p.status(), PaperStatus::EntryHalted);
    assert!(p.reserve_short(&entry(2)).is_err());
    let cover = OrderIntent::new(
        2,
        1_000_000_000,
        FixedPrice(101 * SCALE),
        FixedQty(60_000_000),
        None,
        FixedUsd::ZERO,
        0,
        5,
        VenueId::Equities,
        0,
        OrderSide::Buy,
        TimeInForce::Ioc,
        true,
    )
    .unwrap();
    assert_eq!(p.reserve_cover(&cover).unwrap().0, 60_000_000);
}
#[test]
fn no_reused_entry_order_id_can_pass_journal_recovery_boundary() {
    let tmp = Scratch::new();
    let mut p = session(tmp.path());
    p.reserve_short(&entry(1)).unwrap();
    p.confirm_paper_cancel(1).unwrap();
    assert!(p.reserve_short(&entry(1)).is_err());
    assert_eq!(p.summary().last_order_id, 1);
}
#[test]
fn reducing_short_changes_wallet_only_after_durable_cover_fill() {
    let tmp = Scratch::new();
    let mut p = session(tmp.path());
    p.reserve_short(&entry(1)).unwrap();
    p.simulate_fill(1, FixedQty(60_000_000)).unwrap();
    p.confirm_paper_cancel(1).unwrap();
    let initial = p.wallet().total_equity;
    let cover = OrderIntent::new(
        2,
        1_000_000_000,
        FixedPrice(101 * SCALE),
        FixedQty(60_000_000),
        None,
        FixedUsd::ZERO,
        0,
        5,
        VenueId::Equities,
        0,
        OrderSide::Buy,
        TimeInForce::Ioc,
        true,
    )
    .unwrap();
    p.reserve_cover(&cover).unwrap();
    assert_eq!(p.wallet().total_equity, initial);
    p.simulate_fill(2, FixedQty(40_000_000)).unwrap();
    assert_eq!(p.summary().confirmed_short_qty_raw, 20_000_000);
    assert!(p.wallet().total_equity.0 < initial.0);
    assert_eq!(
        Journal::inspect(tmp.path())
            .unwrap()
            .summary()
            .confirmed_short_qty_raw,
        20_000_000
    );
}
#[test]
fn cannot_seal_session_with_unresolved_inventory_or_order() {
    let tmp = Scratch::new();
    let mut p = session(tmp.path());
    p.reserve_short(&entry(1)).unwrap();
    assert!(p.seal_flat_session().is_err());
    p.simulate_fill(1, FixedQty(20_000_000)).unwrap();
    p.confirm_paper_cancel(1).unwrap();
    assert!(p.seal_flat_session().is_err());
}
#[test]
fn no_real_market_venue_or_long_side_is_allowed() {
    let tmp = Scratch::new();
    let mut p = session(tmp.path());
    let mut wrong = entry(1);
    wrong.venue_id = VenueId::BinanceUsdM as u8;
    assert!(p.reserve_short(&wrong).is_err());
    wrong.venue_id = VenueId::Equities as u8;
    wrong.side = OrderSide::Buy as u8;
    assert!(p.reserve_short(&wrong).is_err());
}
