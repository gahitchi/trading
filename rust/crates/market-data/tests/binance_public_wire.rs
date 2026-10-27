use serde_json::json;
use tokio::sync::mpsc;
use trading_market_data::{
    BinanceEnvironment, InstrumentSpecTable, LiveBookConsumer, LiveFrame, SnapshotRequest,
    StreamGroup, build_stream_url, parse_combined, parse_depth_snapshot,
};
use trading_orderbook::SyncOutcome;
fn table(env: BinanceEnvironment) -> InstrumentSpecTable {
    let x = json!({"symbols":[
    {"symbol":"BTCUSDT","status":"TRADING","contractType":"PERPETUAL",
     "quoteAsset":"USDT","marginAsset":"USDT",
     "filters":[
        {"filterType":"PRICE_FILTER","tickSize":"0.10","minPrice":"0.10","maxPrice":"1000000"},
        {"filterType":"LOT_SIZE","stepSize":"0.001","minQty":"0.001","maxQty":"100"},
        {"filterType":"MARKET_LOT_SIZE","stepSize":"0.001","minQty":"0.001","maxQty":"100"},
        {"filterType":"MIN_NOTIONAL","notional":"5.0"}
     ]}]});
    InstrumentSpecTable::from_exchange_info(&x, &["BTCUSDT".into()], env).unwrap()
}
fn parse(s: &str, group: StreamGroup) -> LiveFrame {
    parse_combined(
        s,
        group,
        1,
        20_000_000_000,
        &table(BinanceEnvironment::Mainnet),
    )
    .unwrap()
}
fn with_generation(depth: &LiveFrame, generation: u64) -> LiveFrame {
    let mut frame = depth.clone();
    if let LiveFrame::Depth { generation: g, .. } = &mut frame {
        *g = generation;
    }
    frame
}
#[test]
fn parses_exchange_filters_not_display_precisions() {
    let t = table(BinanceEnvironment::Testnet);
    let s = t.by_id(0).unwrap().spec;
    assert_eq!(s.tick_size, 10_000_000);
    assert_eq!(s.step_size, 100_000);
    assert_eq!(s.min_notional, 500_000_000);
}
#[test]
fn mainnet_routes_separate_public_depth_from_regular_market() {
    let t = table(BinanceEnvironment::Mainnet);
    assert_eq!(
        build_stream_url(&t, StreamGroup::PublicDepth).unwrap(),
        "wss://fstream.binance.com/public/stream?streams=btcusdt@depth@100ms"
    );
    let url = build_stream_url(&t, StreamGroup::MarketRegular).unwrap();
    assert!(url.starts_with("wss://fstream.binance.com/market/stream?streams="));
    assert!(url.contains("btcusdt@aggTrade"));
    assert!(url.contains("btcusdt@forceOrder"));
    assert!(url.contains("btcusdt@markPrice@1s"));
    assert!(!url.contains("@depth@0ms"));
}
#[test]
fn testnet_uses_separate_real_socket_subscriptions() {
    let t = table(BinanceEnvironment::Testnet);
    assert!(
        build_stream_url(&t, StreamGroup::PublicDepth)
            .unwrap()
            .starts_with("wss://stream.binancefuture.com/stream")
    );
    assert!(
        build_stream_url(&t, StreamGroup::MarketRegular)
            .unwrap()
            .starts_with("wss://stream.binancefuture.com/stream")
    );
}
#[test]
fn depth_preserves_complete_batch_u_and_pu() {
    let f = parse(
        r#"{"stream":"btcusdt@depth@100ms","data":{
      "e":"depthUpdate","E":1700000000000,"s":"BTCUSDT",
      "U":101,"u":105,"pu":100,"b":[["100.0","2"]],"a":[["100.1","3"]]
    }}"#,
        StreamGroup::PublicDepth,
    );
    match f {
        LiveFrame::Depth {
            first,
            final_id,
            previous_final,
            deltas,
            ..
        } => {
            assert_eq!((first, final_id, previous_final), (101, 105, 100));
            assert_eq!(deltas.len(), 2);
        }
        _ => panic!("depth parsing did not preserve batch"),
    }
}
#[test]
fn no_missing_pu_no_wrong_route() {
    let payload = r#"{"stream":"btcusdt@depth@100ms","data":{
      "e":"depthUpdate","E":1700000000000,"s":"BTCUSDT",
      "U":101,"u":105,"b":[["100","1"]],"a":[]
    }}"#;
    assert!(
        parse_combined(
            payload,
            StreamGroup::PublicDepth,
            1,
            0,
            &table(BinanceEnvironment::Mainnet)
        )
        .is_err()
    );
    assert!(
        parse_combined(
            payload,
            StreamGroup::MarketRegular,
            1,
            0,
            &table(BinanceEnvironment::Mainnet)
        )
        .is_err()
    );
}
#[test]
fn forced_liquidation_nested_symbol_and_side() {
    let payload = r#"{"stream":"btcusdt@forceOrder","data":{
      "e":"forceOrder","E":1700000000000,
      "o":{"s":"BTCUSDT","S":"SELL","p":"100.1","q":"1"}
    }}"#;
    assert!(matches!(
        parse(payload, StreamGroup::MarketRegular),
        LiveFrame::ForcedLiquidation(_)
    ));
}
#[test]
fn trade_and_mark_parse_real_wire_fields() {
    let trade = r#"{"stream":"btcusdt@aggTrade","data":{
       "e":"aggTrade","E":1700000000000,"T":1700000000001,"s":"BTCUSDT",
       "a":9123,"p":"100.2","q":"0.35","m":true
    }}"#;
    let mark = r#"{"stream":"btcusdt@markPrice@1s","data":{
       "e":"markPriceUpdate","E":1700000000000,"s":"BTCUSDT",
       "p":"100.20","i":"100.19","r":"0.00010000"
    }}"#;
    assert!(matches!(
        parse(trade, StreamGroup::MarketRegular),
        LiveFrame::AggTrade(_)
    ));
    assert!(matches!(
        parse(mark, StreamGroup::MarketRegular),
        LiveFrame::Mark {
            funding_rate_raw: 10_000,
            ..
        }
    ));
}
#[test]
fn snapshot_generation_prevents_stale_rest_revival() {
    let t = table(BinanceEnvironment::Mainnet);
    let (tx, mut rx) = mpsc::channel(8);
    let mut c = LiveBookConsumer::new(&t, tx).unwrap();
    let depth = parse(
        r#"{"stream":"btcusdt@depth@100ms","data":{
      "e":"depthUpdate","E":1700000000000,"s":"BTCUSDT",
      "U":100,"u":105,"pu":99,"b":[["100","2"]],"a":[["101","2"]]
    }}"#,
        StreamGroup::PublicDepth,
    );
    c.on_frame(&LiveFrame::Reset { generation: 3 }).unwrap();
    let older = with_generation(&depth, 3);
    c.on_frame(&older).unwrap();
    let first = rx.try_recv().unwrap();
    c.on_frame(&LiveFrame::Reset { generation: 4 }).unwrap();
    c.on_frame(&with_generation(&depth, 4)).unwrap();
    let current = rx.try_recv().unwrap();
    let raw = br#"{"lastUpdateId":100,"bids":[["100","1"]],"asks":[["101","1"]]}"#;
    let stale = parse_depth_snapshot(raw, first).unwrap();
    assert_eq!(c.on_frame(&stale).unwrap(), None);
    assert!(!c.book(0).unwrap().is_tradable());
    let good = parse_depth_snapshot(raw, current).unwrap();
    assert_eq!(
        c.on_frame(&good).unwrap(),
        Some(SyncOutcome::Activated)
    );
    assert!(c.book(0).unwrap().is_tradable());
}
#[test]
fn buffers_before_snapshot_and_proves_live_bridge() {
    let t = table(BinanceEnvironment::Mainnet);
    let (tx, mut rx) = mpsc::channel(8);
    let mut c = LiveBookConsumer::new(&t, tx).unwrap();
    c.on_frame(&LiveFrame::Reset { generation: 1 }).unwrap();
    let depth = parse(
        r#"{"stream":"btcusdt@depth@100ms","data":{
      "e":"depthUpdate","E":1700000000000,"s":"BTCUSDT",
      "U":100,"u":105,"pu":99,"b":[["100","2"]],"a":[["101","2"]]
    }}"#,
        StreamGroup::PublicDepth,
    );
    assert_eq!(c.on_frame(&depth).unwrap(), Some(SyncOutcome::Buffered));
    let req = rx.try_recv().unwrap();
    let snap = parse_depth_snapshot(
        br#"{"lastUpdateId":100,"bids":[["100","1"]],"asks":[["101","1"]]}"#,
        req,
    )
    .unwrap();
    assert_eq!(c.on_frame(&snap).unwrap(), Some(SyncOutcome::Activated));
    assert!(c.book(0).unwrap().is_tradable());
}
#[test]
fn corruption_and_unknown_filter_fail_closed() {
    let t = table(BinanceEnvironment::Mainnet);
    let (tx, _rx) = mpsc::channel(1);
    let mut c = LiveBookConsumer::new(&t, tx).unwrap();
    let snap = LiveFrame::Snapshot {
        request: SnapshotRequest {
            symbol_id: 0,
            generation: 7,
            request_id: 1,
        },
        last_update_id: 100,
        bids: vec![],
        asks: vec![],
    };
    assert_eq!(c.on_frame(&snap).unwrap(), None);
}
