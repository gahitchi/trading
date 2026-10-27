//! Live Binance USD-M public market-data acquisition in Rust.
//! No synthetic fills, no private account credentials, no order submission.
//! The network runs outside the book/risk strategy core thread.
//! An SPSC ring overflow, snapshot gap or malformed payload invalidates
//! all affected books; it must never be treated as a trading-ready feed.
mod bridge;
mod instruments;
mod runtime;
mod wire;

pub use bridge::{LiveBookConsumer, MarketFrameConsumer, MarketFrameProducer, market_frame_ring};
pub use instruments::{
    BinanceEnvironment, Instrument, InstrumentSpecTable, fetch_exchange_info, rest_client,
};
pub use runtime::{LiveMarketDataEngine, build_stream_url, fetch_depth_snapshot};
pub use wire::{LiveFrame, SnapshotRequest, StreamGroup, parse_combined, parse_depth_snapshot};

use trading_core_types::NumericError;
use trading_orderbook::{BookError, SyncError};

#[derive(Debug)]
pub enum MarketDataError {
    Http(reqwest::Error),
    WebSocket(tokio_tungstenite::tungstenite::Error),
    Json(serde_json::Error),
    Numeric(NumericError),
    Book(BookError),
    Sync(SyncError),
    InvalidResponse(&'static str),
    InvalidConfiguration(&'static str),
    Backpressure,
    ChannelClosed,
}
impl From<reqwest::Error> for MarketDataError {
    fn from(value: reqwest::Error) -> Self {
        Self::Http(value)
    }
}
impl From<serde_json::Error> for MarketDataError {
    fn from(value: serde_json::Error) -> Self {
        Self::Json(value)
    }
}
impl std::fmt::Display for MarketDataError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for MarketDataError {}
