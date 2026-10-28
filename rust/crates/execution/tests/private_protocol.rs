use serde_json::json;
use trading_core_types::{ExecutionStatus, SCALE};
use trading_execution::{PrivateEvent, SigningMode, endpoint, parse_private_event};
use trading_market_data::{BinanceEnvironment, InstrumentSpecTable};

fn table() -> InstrumentSpecTable {
    let response = json!({"symbols":[{"symbol":"BTCUSDT","status":"TRADING",
        "contractType":"PERPETUAL","quoteAsset":"USDT","marginAsset":"USDT",
        "filters":[
            {"filterType":"PRICE_FILTER","tickSize":"0.10","minPrice":"0.10","maxPrice":"1000000"},
            {"filterType":"LOT_SIZE","stepSize":"0.001","minQty":"0.001","maxQty":"100"},
            {"filterType":"MARKET_LOT_SIZE","stepSize":"0.001","minQty":"0.001","maxQty":"100"},
            {"filterType":"MIN_NOTIONAL","notional":"5.0"}]}]});
    InstrumentSpecTable::from_exchange_info(
        &response,
        &["BTCUSDT".into()],
        BinanceEnvironment::Testnet,
    )
    .unwrap()
}
const ORDER: &str = r#"{
    "e":"ORDER_TRADE_UPDATE","E":1700000000000,"T":1700000000000,
    "o":{"s":"BTCUSDT","c":"tl-42","i":924,"S":"SELL",
    "X":"PARTIALLY_FILLED","x":"TRADE","L":"50000.10","l":"0.010",
    "z":"0.010","ap":"50000.10","n":"0.20","N":"USDT","rp":"1.50",
    "t":3001}
}"#;
#[test]
fn real_fill_payload_preserves_fills_and_fees_without_optimistic_account_mutation() {
    let event = parse_private_event(ORDER, &table()).unwrap();
    let PrivateEvent::Order(report) = event else {
        panic!("expected exchange fill")
    };
    assert_eq!(report.event.client_order_id, 42);
    assert_eq!(report.event.status, ExecutionStatus::PartiallyFilled as u8);
    assert_eq!(report.event.fill_price, 50_000 * SCALE + 10_000_000);
    assert_eq!(report.event.last_filled_qty, 1_000_000);
    assert_eq!(report.event.cumulative_qty, 1_000_000);
    assert_eq!(report.event.commission, 20_000_000);
    assert_eq!(report.event.realized_pnl, 150_000_000);
    assert_eq!(report.exchange_order_id, 924);
    assert_eq!(report.trade_id, 3001);
    assert_eq!(report.event.flags & 1, 1);
}
#[test]
fn private_account_wallet_is_usdt_and_position_amount_is_signed() {
    let raw = r#"{"e":"ACCOUNT_UPDATE","E":1700000000123,"T":1700000000110,
    "a":{"m":"ORDER","B":[{"a":"USDT","wb":"100.25","cw":"95.10","bc":"0"}],
    "P":[{"s":"BTCUSDT","pa":"-0.010","ep":"50000.10","ps":"BOTH"}]}}"#;
    let PrivateEvent::Account(a) = parse_private_event(raw, &table()).unwrap() else {
        panic!("expected account update")
    };
    assert_eq!(a.balances[0].wallet_balance_raw, 100 * SCALE + 25_000_000);
    assert_eq!(
        a.balances[0].cross_wallet_balance_raw,
        95 * SCALE + 10_000_000
    );
    assert_eq!(a.positions[0].position_amount_raw, -1_000_000);
    assert_eq!(a.positions[0].position_side, "BOTH");
}
#[test]
fn external_liquidation_and_expired_listenkey_trigger_reconciliation() {
    let raw = ORDER.replace("tl-42", "autoclose-123");
    assert!(matches!(
        parse_private_event(&raw, &table()).unwrap(),
        PrivateEvent::ReconciliationRequired(_)
    ));
    assert!(matches!(
        parse_private_event(r#"{"e":"listenKeyExpired","E":1700000000000}"#, &table()).unwrap(),
        PrivateEvent::ReconciliationRequired(_)
    ));
}
#[test]
fn non_usdt_commission_is_preserved_but_not_marked_converted() {
    let raw = ORDER.replace(r#""N":"USDT""#, r#""N":"BNB""#);
    let PrivateEvent::Order(e) = parse_private_event(&raw, &table()).unwrap() else {
        panic!("expected order")
    };
    assert_eq!(e.event.flags & 1, 0);
    assert_eq!(e.commission_asset, "BNB");
}
#[test]
fn a_real_order_ack_is_not_private_execution() {
    assert_eq!(
        endpoint(BinanceEnvironment::Testnet),
        "wss://testnet.binancefuture.com/ws-fapi/v1"
    );
    assert_ne!(SigningMode::Ed25519, SigningMode::HmacSha256);
}

#[test]
fn strict_production_order_ack_decoder_does_not_infer_fills() {
    use trading_execution::{OrderWireEvent,decode_order_response};
    let good = json!({"id":17,"status":200,"result":{
        "orderId":9871,"clientOrderId":"tl-42","symbol":"BTCUSDT","status":"NEW"
    }});
    assert_eq!(
        decode_order_response(17,"BTCUSDT","tl-42",&good.to_string()).unwrap(),
        OrderWireEvent::Accepted {id:17,exchange_order_id:Some(9871)}
    );
    let mut malformed=good.clone();
    malformed["result"]["orderId"]=json!(0);
    assert!(decode_order_response(17,"BTCUSDT","tl-42",&malformed.to_string()).is_err());
    let mut mismatched=good.clone();
    mismatched["result"]["clientOrderId"]=json!("tl-99");
    assert!(decode_order_response(17,"BTCUSDT","tl-42",&mismatched.to_string()).is_err());
    let mut mismatched_symbol=good.clone();
    mismatched_symbol["result"]["symbol"]=json!("ETHUSDT");
    assert!(decode_order_response(17,"BTCUSDT","tl-42",&mismatched_symbol.to_string()).is_err());
    let mut wrong_id=good.clone();
    wrong_id["id"]=json!(99);
    assert!(decode_order_response(17,"BTCUSDT","tl-42",&wrong_id.to_string()).is_err());
}
#[test]
fn failed_requests_only_become_rejected_when_unambiguous() {
    use trading_execution::{OrderWireEvent,decode_order_response};
    let rejected=json!({"id":23,"status":400,"error":{"code":-1121,"msg":"Invalid symbol."}});
    assert_eq!(decode_order_response(23,"BTCUSDT","tl-42",&rejected.to_string()).unwrap(),
        OrderWireEvent::Rejected{id:23,code:-1121});
    for (status,code) in [(503,-1000),(400,-1006),(400,-1007),(400,-2010),
        (429,-1003),(409,-1021)] {
        let uncertain=json!({"id":23,"status":status,"error":{"code":code}});
        assert_eq!(decode_order_response(23,"BTCUSDT","tl-42",&uncertain.to_string()).unwrap(),
            OrderWireEvent::Uncertain{id:23});
    }
    for bad in [
        json!({"id":23,"status":200,"result":{}}),
        json!({"id":23,"status":400,"error":{}}),
        json!({"id":23,"status":400,"result":{"orderId":3}}),
        json!({"id":23,"status":418,"error":{}}),
    ] {
        assert!(decode_order_response(23,"BTCUSDT","tl-42",&bad.to_string()).is_err());
    }
    assert!(decode_order_response(23,"BTCUSDT","tl-42","{invalid").is_err());
}
