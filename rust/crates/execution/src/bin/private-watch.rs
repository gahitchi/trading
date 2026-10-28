//! Authenticated, receive-only Binance Futures account stream monitor.
//! No order routing, no API secrets printed, and no wallet mutation.
use std::{error::Error, time::Duration};
use tokio::sync::mpsc;
use trading_execution::{PrivateEvent, UserDataStreamManager};
use trading_market_data::{BinanceEnvironment, fetch_exchange_info, rest_client};
#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    let mut environment = BinanceEnvironment::Testnet;
    let mut symbols = vec!["BTCUSDT".to_string()];
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--environment" => {
                environment = match args.next().as_deref() {
                    Some("testnet") => BinanceEnvironment::Testnet,
                    Some("mainnet") => BinanceEnvironment::Mainnet,
                    _ => return Err("invalid environment".into()),
                };
            }
            "--symbols" => {
                symbols = args
                    .next()
                    .ok_or("symbols missing")?
                    .split(',')
                    .map(str::to_owned)
                    .collect();
            }
            _ => return Err("usage: private-watch --environment testnet --symbols BTCUSDT".into()),
        }
    }
    let api_key = std::env::var("BINANCE_API_KEY")
        .map_err(|_| "BINANCE_API_KEY environment variable required")?;
    let client = rest_client()?;
    let table = fetch_exchange_info(&client, environment, &symbols).await?;
    let manager = UserDataStreamManager::new(api_key, table)?;
    let (tx, mut rx) = mpsc::channel(256);
    println!("authenticated private Futures stream: {environment:?} (READ ONLY)");
    println!("All order routes disabled. No credentials or listenKey logged.");
    let mut task = tokio::spawn(async move { manager.run(tx).await });
    let mut orders = 0u64;
    let mut accounts = 0u64;
    let mut ticker = tokio::time::interval(Duration::from_secs(10));
    loop {
        tokio::select! {
            _=tokio::signal::ctrl_c()=>break,
            _=ticker.tick()=>{
                println!("private stream: order_events={orders} account_events={accounts}");
            }
            next=rx.recv()=>{
                match next {
                    Some(PrivateEvent::Order(_))=>orders+=1,
                    Some(PrivateEvent::Account(_))=>accounts+=1,
                    Some(PrivateEvent::ReconciliationRequired(reason))=>{
                        eprintln!("PRIVATE STREAM RECONCILIATION REQUIRED: {reason}");
                    }
                    Some(PrivateEvent::Disconnected)=>{
                        eprintln!("PRIVATE STREAM DISCONNECTED: execution eligibility revoked");
                    }
                    None=>break,
                }
            }
            result=&mut task=>{
                eprintln!("private stream task ended: {}",result.is_ok());
                break;
            }
        }
    }
    task.abort();
    Ok(())
}
