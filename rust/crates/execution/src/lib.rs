//! Real Binance USD-M *transport* and exchange-confirmed private report parser.
//!
//! This library contains no fake fill path. No wallet or strategy quantity is
//! mutated by an order ACK. Mainnet order submission is deliberately sealed:
//! qualified live reconciliation, persistent reservations and kill services
//! are prerequisites for promotion in Live Upgrade Part 3.
mod auth;
mod order;
mod private;
mod transport;

pub use auth::{Authentication, SignedRequest, SigningMode};
pub use order::{ExchangeOrder, OrderKind, OrderSide, PrepareOrder};
pub use private::{
    AccountBalance, AccountPosition, PrivateEvent, UserDataStreamManager, execution_report_ring,
    parse_private_event,
};
pub use transport::{ExchangeSocket, ExchangeSocketMode, OrderAck, OrderWireEvent};

use trading_market_data::BinanceEnvironment;
#[derive(Debug)]
pub enum ExecutionError {
    Configuration(&'static str),
    InvalidMessage(&'static str),
    Unauthorized,
    Disabled,
    UnexpectedState,
    Transport,
    Storage(std::io::Error),
    Http(reqwest::Error),
    Json(serde_json::Error),
    Numeric(trading_core_types::NumericError),
}
impl From<std::io::Error> for ExecutionError {
    fn from(e: std::io::Error) -> Self {
        Self::Storage(e)
    }
}
impl From<reqwest::Error> for ExecutionError {
    fn from(e: reqwest::Error) -> Self {
        Self::Http(e)
    }
}
impl From<serde_json::Error> for ExecutionError {
    fn from(e: serde_json::Error) -> Self {
        Self::Json(e)
    }
}
impl From<trading_core_types::NumericError> for ExecutionError {
    fn from(e: trading_core_types::NumericError) -> Self {
        Self::Numeric(e)
    }
}
impl std::fmt::Display for ExecutionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Never print credential-bearing reqwest URLs/headers.
        match self {
            Self::Storage(e) => write!(f, "storage error: {e}"),
            Self::Configuration(e) | Self::InvalidMessage(e) => write!(f, "{e}"),
            Self::Numeric(_) => write!(f, "invalid fixed-point number"),
            _ => write!(
                f,
                "{}",
                match self {
                    Self::Unauthorized => "unauthorized",
                    Self::Disabled => "routing disabled",
                    Self::UnexpectedState => "uncertain exchange order state",
                    Self::Transport => "websocket transport failure",
                    Self::Http(_) => "HTTP exchange request failure",
                    Self::Json(_) => "invalid exchange JSON",
                    _ => "execution failure",
                }
            ),
        }
    }
}
impl std::error::Error for ExecutionError {}
pub fn endpoint(env: BinanceEnvironment) -> &'static str {
    match env {
        BinanceEnvironment::Mainnet => "wss://ws-fapi.binance.com/ws-fapi/v1",
        BinanceEnvironment::Testnet => "wss://testnet.binancefuture.com/ws-fapi/v1",
    }
}
