//! Real exchange public-feed reader. Never sends or signs any order.
use std::{
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
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
            let mut messages=0_u64;
            let mut trades=0_u64;
            let mut liqs=0_u64;
            let mut marks=0_u64;
            let mut idle=0_u16;
            while !stop2.load(Ordering::Acquire) {
                match consumer.pop(){
                    Ok(frame)=>{
                        idle=0;
                        match &frame {
                            LiveFrame::AggTrade(_)=>trades+=1,
                            LiveFrame::ForcedLiquidation(_)=>liqs+=1,
                            LiveFrame::Mark{..}=>marks+=1,
                            _=>{}
                        }
                        books.on_frame(&frame).map_err(|e|{
                            books.disconnect_all();
                            format!("book invalidated: {e}")
                        })?;
                        messages+=1;
                        if messages%1000==0{
                            eprintln!("events={messages} live_books={} trades={trades} liqs={liqs} mark={marks}",
                                books.active_books());
                        }
                    }
                    Err(rtrb::PopError::Empty)=>{
                        idle=idle.saturating_add(1);
                        if idle>=1000 {
                            std::thread::yield_now();
                            idle=0;
                        } else {std::hint::spin_loop();}
                    }
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
