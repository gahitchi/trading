use std::path::Path;
use trading_paper_gateway::{Journal, PaperError, run_demo};

fn help() -> ! {
    eprintln!("usage: cargo run -p trading-paper-gateway --bin paper-lab -- [demo|inspect] PATH");
    eprintln!("  demo:    create a NEW file and execute a supervised synthetic paper round-trip");
    eprintln!("  inspect: read-only validation of an existing journal (never resends orders)");
    std::process::exit(2);
}
fn main() {
    if let Err(e) = run() {
        eprintln!("paper-lab: {e}");
        std::process::exit(1);
    }
}
fn run() -> Result<(), PaperError> {
    let mut a = std::env::args().skip(1);
    let Some(cmd) = a.next() else { help() };
    let Some(path) = a.next() else { help() };
    if a.next().is_some() { help() }
    let path = Path::new(&path);
    match cmd.as_str() {
        "demo" => {
            let result = run_demo(path)?;
            let recovered = Journal::inspect(path)?;
            let s = recovered.summary();
            if recovered.is_inspect_only() && s.confirmed_short_qty_raw == 0
                && s.halted && s.unresolved_order().is_none()
                && s.records == result.journal_events
            {
                println!("paper demo COMPLETE (offline synthetic fixture only)");
                println!("entry_id={} cover_id={}", result.entry_id, result.cover_id);
                println!("entry_fill_raw={} cover_fill_raw={}",
                    result.entry_filled.0, result.cover_filled.0);
                println!("realized_net_pnl_raw={}", result.realized_net_pnl.0);
                println!("journal_events={} sha_authentication=NOT_PROVIDED",
                    result.journal_events);
                println!("paper_wallet_total_raw={}", result.end_wallet.total_equity.0);
                println!("short_position_raw=0 pending_orders=0 halted=true");
                Ok(())
            } else {
                Err(PaperError::InvalidFlow)
            }
        }
        "inspect" => {
            let journal = Journal::inspect(path)?;
            let s = journal.summary();
            println!("offline journal VERIFIED (checksum only; not authenticated)");
            println!("events={} last_id={} short_raw={} unresolved={:?} halted={}",
                s.records, s.last_order_id, s.confirmed_short_qty_raw,
                s.unresolved_order(), s.halted);
            println!("realized_pnl_raw={} cash_total_raw={}",
                s.realized_net_pnl_raw, s.wallet().total_equity.0);
            println!("inspect_only=true routing_allowed=false");
            Ok(())
        }
        _ => help(),
    }
}
