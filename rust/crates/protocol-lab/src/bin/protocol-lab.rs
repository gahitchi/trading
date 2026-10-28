//! Zero-cost localhost demonstration; never contacts an exchange.
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    trading_protocol_lab::run_selftest().await?;
    println!("LOCAL PROTOCOL LAB PASS: signed request, ACK, private fills, IOC expiry, replay rejection");
    println!("This is an isolated synthetic test, not proof of Binance authentication.");
    Ok(())
}
