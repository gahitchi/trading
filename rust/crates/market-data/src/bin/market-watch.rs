//! Real exchange public-feed reader. Never sends or signs any order.
use std::{
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::sync::mpsc;
use trading_market_data::{
    BinanceEnvironment, LiveBookConsumer, LiveFrame, LiveMarketDataEngine, fetch_exchange_info,
    market_frame_ring, rest_client,
};

fn parse_args() -> Result<(BinanceEnvironment, Vec<String>), String> {
    let mut env = BinanceEnvironment::Testnet;
    let mut symbols: Vec<String> = vec!["BTCUSDT".into()];
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--environment" => {
                env = match args.next().as_deref() {
                    Some("mainnet") => BinanceEnvironment::Mainnet,
                    Some("testnet") => BinanceEnvironment::Testnet,
                    _ => return Err("expected mainnet or testnet".into()),
                };
            }
            "--symbols" => {
                symbols = args
                    .next()
                    .ok_or("missing --symbols list")?
                    .split(',')
                    .map(str::to_owned)
                    .collect();
            }
            _ => return Err(format!("unknown argument: {a}")),
        }
    }
    Ok((env, symbols))
}
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let (env, symbols) = parse_args()?;
    eprintln!("Trading Lab real market feed: {env:?}, {symbols:?}");
    eprintln!("READ-ONLY: no execution, no account authentication, no live orders");
    let client = rest_client()?;
    let table = fetch_exchange_info(&client, env, &symbols).await?;
    for entry in table.instruments() {
        eprintln!(
            "{} id={} tick={} step={} min_notional_quote={}",
            entry.symbol,
            entry.symbol_id,
            entry.spec.tick_size,
            entry.spec.step_size,
            entry.spec.min_notional
        );
    }
    let (producer, mut consumer) = market_frame_ring(2048)?;
    let (snapshot_tx, requests) = mpsc::channel(64);
    let stop = Arc::new(AtomicBool::new(false));
    let stop2 = stop.clone();
    let table2 = table.clone();
    let hot=std::thread::Builder::new().name("trading-core1-book".into())
        .spawn(move || -> Result<(),String> {
            let mut books=LiveBookConsumer::new(&table2,snapshot_tx)
                .map_err(|e|format!("{e}"))?;
            let mut messages = 0_u64;
            let mut trades = 0_u64;
            let mut liqs = 0_u64;
            let mut marks = 0_u64;
            let mut depth = 0_u64;
            let mut snapshots = 0_u64;
            let mut resets = 0_u64;
            let mut resync_count = 0_u64;
            let mut last_report = Instant::now();
            let mut idle = 0_u16;
            while !stop2.load(Ordering::Acquire) {
                match consumer.pop() {
                    Ok(frame) => {
                        idle = 0;
                        match &frame {
                            LiveFrame::AggTrade(_) => trades += 1,
                            LiveFrame::ForcedLiquidation(_) => liqs += 1,
                            LiveFrame::Mark { .. } => marks += 1,
                            LiveFrame::Depth { .. } => depth += 1,
                            LiveFrame::Snapshot { .. } => snapshots += 1,
                            LiveFrame::Reset { .. } => resets += 1,
                        }
                        // Capture sequence state *before* the synchronizer
                        // mutates it. A failed bridge resets its last ID to
                        // zero; logging only afterwards hides the diagnosis.
                        let prior = match &frame {
                            LiveFrame::Depth { symbol_id, first, final_id, previous_final, .. } => {
                                books.book(*symbol_id).map(|b| (
                                    *symbol_id,
                                    "depth",
                                    b.state(),
                                    b.last_update_id(),
                                    *first, *final_id, *previous_final,
                                ))
                            }
                            LiveFrame::Snapshot { request, last_update_id, .. } => {
                                books.book(request.symbol_id).map(|b| (
                                    request.symbol_id,
                                    "snapshot",
                                    b.state(),
                                    b.last_update_id(),
                                    *last_update_id, 0, 0,
                                ))
                            }
                            _ => None,
                        };
                        let outcome = books.on_frame(&frame).map_err(|e| {
                            books.disconnect_all();
                            format!("book invalidated: {e}")
                        })?;
                        messages += 1;
                        if messages <= 3 {
                            eprintln!("market-watch first_frame={messages} depth={depth} snapshots={snapshots} resets={resets} trades={trades} marks={marks}");
                        }
                        if matches!(outcome, Some(trading_orderbook::SyncOutcome::NeedsSnapshot)) {
                            resync_count += 1;
                            // Log the first failures and then sample,
                            // avoiding a log flood while preserving proof.
                            if resync_count <= 24 || resync_count % 50 == 0 {
                                if let Some((symbol, kind, state, old_id, first, final_id, pu)) = prior {
                                    eprintln!(
                                        "market-watch resync={resync_count} symbol_id={symbol} kind={kind} prior_state={state:?} local_last={old_id} U_or_snapshot={first} u={final_id} pu={pu}"
                                    );
                                }
                            }
                        }
                        if matches!(
                            outcome,
                            Some(trading_orderbook::SyncOutcome::Activated)
                                | Some(trading_orderbook::SyncOutcome::NeedsSnapshot)
                        ) {
                            if resync_count <= 24 || matches!(outcome, Some(trading_orderbook::SyncOutcome::Activated)) {
                                eprintln!("market-watch depth_sync={outcome:?} live_books={}", books.active_books());
                            }
                        }
                    }
                    Err(rtrb::PopError::Empty) => {
                        idle = idle.saturating_add(1);
                        if idle >= 1000 {
                            std::thread::yield_now();
                            idle = 0;
                        } else {
                            std::hint::spin_loop();
                        }
                    }
                }
                if last_report.elapsed() >= Duration::from_secs(5) {
                    eprintln!(
                        "market-watch status events={messages} live_books={} depth={depth} snapshots={snapshots} resets={resets} resyncs={resync_count} trades={trades} liquidations={liqs} marks={marks}",
                        books.active_books()
                    );
                    last_report = Instant::now();
                }
            }
            books.disconnect_all();
            Ok(())
        })?;
    let net = LiveMarketDataEngine::new(table)?;
    let result = tokio::select! {
        x=net.run(producer,requests)=>x,
        _=tokio::signal::ctrl_c()=>Ok(()),
    };
    stop.store(true, Ordering::Release);
    // Give the Core 1 loop one scheduler tick to observe shutdown; join
    // happens after service is no longer acquiring external market data.
    tokio::time::sleep(Duration::from_millis(5)).await;
    let hot_result = hot.join().map_err(|_| "book thread panic")?;
    if let Err(e) = hot_result {
        return Err(e.into());
    }
    result?;
    Ok(())
}
