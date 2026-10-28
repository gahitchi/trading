use serde_json::{Value, json};
use trading_core_types::SCALE;
use trading_execution::{
    AccountReconciler, PrivateEvent, ReconcileOutcome, ReconciliationStatus, parse_bootstrap,
    parse_private_event,
};
use trading_market_data::{BinanceEnvironment, InstrumentSpecTable};
fn instruments() -> InstrumentSpecTable {
    let v = json!({"symbols":[{"symbol":"BTCUSDT","status":"TRADING","contractType":"PERPETUAL",
        "quoteAsset":"USDT","marginAsset":"USDT",
        "filters":[
            {"filterType":"PRICE_FILTER","tickSize":"0.10","minPrice":"0.10","maxPrice":"1000000"},
            {"filterType":"LOT_SIZE","stepSize":"0.001","minQty":"0.001","maxQty":"100"},
            {"filterType":"MARKET_LOT_SIZE","stepSize":"0.001","minQty":"0.001","maxQty":"100"},
            {"filterType":"MIN_NOTIONAL","notional":"5"}]}]});
    InstrumentSpecTable::from_exchange_info(&v, &["BTCUSDT".into()], BinanceEnvironment::Testnet)
        .unwrap()
}
fn account() -> Value {
    json!({"multiAssetsMargin":false,
        "totalWalletBalance":"100.00","totalCrossWalletBalance":"100.00",
        "availableBalance":"95.00","totalMarginBalance":"102.00",
        "totalUnrealizedProfit":"2.00","totalMaintMargin":"1.00",
        "totalOpenOrderInitialMargin":"0.00",
        "assets":[{"asset":"USDT","walletBalance":"100.00"}],
        "positions":[{"symbol":"BTCUSDT","positionAmt":"-0.010",
            "entryPrice":"50000","positionSide":"BOTH"}]})
}
fn snapshot() -> trading_execution::AccountSnapshot {
    parse_bootstrap(
        &account(),
        &json!([]),
        &json!({"dualSidePosition":false}),
        &instruments(),
        1_700_000_000_000,
    )
    .unwrap()
}
fn prepared() -> AccountReconciler {
    let mut reconciler = AccountReconciler::new(snapshot(), instruments()).unwrap();
    assert_eq!(
        reconciler.apply(&PrivateEvent::Connected),
        ReconcileOutcome::Applied
    );
    reconciler
}
fn trade(qty: &str, cum: &str, tid: u64, fee_asset: &str) -> PrivateEvent {
    let v = json!({"e":"ORDER_TRADE_UPDATE","E":1_700_000_000_005u64,
        "T":1_700_000_000_005u64,
        "o":{"s":"BTCUSDT","c":"tl-42","i":44,"S":"SELL",
            "X":"PARTIALLY_FILLED","L":"50000.1","l":qty,"z":cum,
            "n":"0.01","N":fee_asset,"rp":"0.04","t":tid}});
    parse_private_event(&v.to_string(), &instruments()).unwrap()
}
#[test]
fn signed_rest_schema_is_strict_and_balance_is_not_fabricated() {
    let s = snapshot();
    assert_eq!(s.wallet_usdt, 100 * SCALE);
    assert_eq!(s.total_margin_usdt, 102 * SCALE);
    assert_eq!(s.positions[0].quantity_raw, -1_000_000);
    assert_eq!(s.available_usdt, 95 * SCALE);
    let mirror = AccountReconciler::new(s, instruments()).unwrap();
    assert_eq!(mirror.status(), ReconciliationStatus::AwaitingStream);
    assert!(!mirror.trading_permitted());
}
#[test]
fn unsupported_modes_or_unknown_positions_cannot_bootstrap() {
    let mut a = account();
    a["multiAssetsMargin"] = json!(true);
    assert!(
        parse_bootstrap(
            &a,
            &json!([]),
            &json!({"dualSidePosition":false}),
            &instruments(),
            1
        )
        .is_err()
    );
    let mut a = account();
    a["positions"][0]["symbol"] = json!("XRPUSDT");
    assert!(
        parse_bootstrap(
            &a,
            &json!([]),
            &json!({"dualSidePosition":false}),
            &instruments(),
            1
        )
        .is_err()
    );
    assert!(
        parse_bootstrap(
            &account(),
            &json!([]),
            &json!({"dualSidePosition":true}),
            &instruments(),
            1
        )
        .is_err()
    );
}
#[test]
fn open_orders_are_reported_not_silently_canceled() {
    let ord = json!([{"symbol":"BTCUSDT","clientOrderId":"outside-7",
        "orderId":78,"origQty":"0.1","executedQty":"0.03"}]);
    let snapshot = parse_bootstrap(
        &account(),
        &ord,
        &json!({"dualSidePosition":false}),
        &instruments(),
        1,
    )
    .unwrap();
    assert_eq!(snapshot.open_orders[0].remaining_qty_raw, 7_000_000);
    assert_eq!(
        AccountReconciler::new(snapshot, instruments())
            .unwrap()
            .open_orders_at_bootstrap(),
        1
    );
}
#[test]
fn account_update_is_delta_and_never_clears_absent_usdt() {
    let mut mirror = prepared();
    let update = json!({"e":"ACCOUNT_UPDATE","E":1_700_000_000_020u64,
        "T":1_700_000_000_020u64,
        "a":{"m":"ORDER","B":[],"P":[{"s":"BTCUSDT","pa":"-0.02",
            "ep":"50010","ps":"BOTH"}]}});
    let event = parse_private_event(&update.to_string(), &instruments()).unwrap();
    assert_eq!(mirror.apply(&event), ReconcileOutcome::Applied);
    assert_eq!(
        mirror.observed_balance("USDT").unwrap().wallet_raw,
        100 * SCALE
    );
    assert_eq!(
        mirror.observed_position("BTCUSDT").unwrap().quantity_raw,
        -2_000_000
    );
    assert!(!mirror.trading_permitted());
}
#[test]
fn exact_replayed_trade_is_idempotent_and_conflicting_trade_quarantines() {
    let mut mirror = prepared();
    let first = trade("0.01", "0.01", 123, "USDT");
    assert_eq!(mirror.apply(&first), ReconcileOutcome::Applied);
    assert_eq!(mirror.apply(&first), ReconcileOutcome::Duplicate);
    assert_eq!(mirror.observed_unique_trades(), 1);
    assert_eq!(mirror.observed_trade_pnl_raw(), 3_000_000);
    let conflicting = trade("0.01", "0.02", 123, "USDT");
    assert_eq!(mirror.apply(&conflicting), ReconcileOutcome::Quarantined);
    assert_eq!(mirror.status(), ReconciliationStatus::Quarantined);
    assert!(!mirror.trading_permitted());
}
#[test]
fn missing_trade_or_cross_currency_fee_fails_closed() {
    let mut r = prepared();
    assert_eq!(
        r.apply(&trade("0.01", "0.02", 1, "USDT")),
        ReconcileOutcome::Quarantined
    );
    let mut r = prepared();
    assert_eq!(
        r.apply(&trade("0.01", "0.01", 1, "BNB")),
        ReconcileOutcome::Quarantined
    );
}
#[test]
fn disconnection_and_unknown_account_event_revoke_observation() {
    let mut r = prepared();
    assert_eq!(
        r.apply(&PrivateEvent::Disconnected),
        ReconcileOutcome::Quarantined
    );
    assert_eq!(
        r.apply(&PrivateEvent::Connected),
        ReconcileOutcome::Quarantined
    );
}
#[test]
fn old_transaction_timestamp_is_not_silently_applied() {
    let mut r = prepared();
    let update = json!({"e":"ACCOUNT_UPDATE","E":1_699_999_999_998u64,
        "T":1_699_999_999_998u64,"a":{"m":"ORDER","B":[],"P":[]}});
    let event = parse_private_event(&update.to_string(), &instruments()).unwrap();
    assert_eq!(r.apply(&event), ReconcileOutcome::Quarantined);
}
