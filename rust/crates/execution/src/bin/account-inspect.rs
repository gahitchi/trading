//! Read-only signed REST inspection for Binance USD-M Testnet.
//! No private WebSocket orders, no position changes, no credential output.
use std::error::Error;
use trading_execution::{Authentication,SignedAccountRest};
use trading_market_data::{BinanceEnvironment,fetch_exchange_info,rest_client};

#[tokio::main]
async fn main()->Result<(),Box<dyn Error>> {
    let mut args=std::env::args().skip(1);
    let mut symbols=vec!["BTCUSDT".to_owned()];
    while let Some(arg)=args.next() {
        match arg.as_str(){
            "--symbols"=>{
                symbols=args.next().ok_or("missing symbols")?
                    .split(',').map(str::to_owned).collect();
            }
            _=>return Err("usage: account-inspect [--symbols BTCUSDT,ETHUSDT,SOLUSDT]".into()),
        }
    }
    // Explicit Testnet only. The signed account bootstrap is a prerequisite
    // to reconciliation; this binary has no order submission ability.
    let environment=BinanceEnvironment::Testnet;
    let table=fetch_exchange_info(&rest_client()?,environment,&symbols).await?;
    let credentials=Authentication::from_environment()?;
    let client=SignedAccountRest::new(credentials,environment)?;
    let snapshot=client.fetch_snapshot(&table).await?;
    println!("SIGNED BINANCE TESTNET ACCOUNT SNAPSHOT VERIFIED (READ ONLY)");
    println!("configured_symbols={} active_positions={} open_orders={}",
        table.instruments().len(),snapshot.positions.len(),snapshot.open_orders.len());
    println!("account_observation_unqualified=true order_submission_enabled=false");
    println!("No account balances, API secrets or order IDs are printed.");
    Ok(())
}
