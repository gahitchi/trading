use crate::order::cancel_params;
use crate::{Authentication, ExchangeOrder, ExecutionError, SignedRequest, SigningMode};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Map, Value, json};
use std::{
    fs::{File, OpenOptions},
    io::Write,
    path::Path,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::net::TcpStream;
use tokio::time::timeout;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream, connect_async, tungstenite::Message};
use trading_market_data::BinanceEnvironment;

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExchangeSocketMode {
    /// Signed private requests and order formatting can be qualified but
    /// no order is sent. This is always the default.
    InspectionOnly,
    /// Explicitly supervised Testnet only; production Mainnet never
    /// becomes enabled through this Phase 2 router.
    TestnetSupervised,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OrderWireEvent {
    Accepted {
        id: u64,
        exchange_order_id: Option<u64>,
    },
    Rejected {
        id: u64,
        code: i64,
    },
    Uncertain {
        id: u64,
    },
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderAck {
    pub id: u64,
    pub exchange_order_id: Option<u64>,
}
struct IntentJournal {
    file: File,
    highest_request: u64,
}
impl IntentJournal {
    fn create(path: &Path) -> Result<Self, ExecutionError> {
        let mut file = OpenOptions::new().write(true).create_new(true).open(path)?;
        file.write_all(b"TRADING-LAB-UNRESOLVED-EXCHANGE-INTENTS-V1\n")?;
        file.sync_all()?;
        if let Some(parent) = path.parent() {
            File::open(if parent.as_os_str().is_empty() {
                Path::new(".")
            } else {
                parent
            })?
            .sync_all()?;
        }
        Ok(Self {
            file,
            highest_request: 0,
        })
    }
    fn record(&mut self, id: u64, method: &str, client_id: u64) -> Result<(), ExecutionError> {
        if id == 0 || id <= self.highest_request || !matches!(method, "place" | "cancel") {
            return Err(ExecutionError::UnexpectedState);
        }
        // No credentials, signatures, raw params or PII in the journal.
        let line = format!("{id}|{method}|{client_id}\n");
        self.file.write_all(line.as_bytes())?;
        self.file.sync_all()?;
        self.highest_request = id;
        Ok(())
    }
}
fn clock_ms() -> Result<u64, ExecutionError> {
    let time = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| ExecutionError::Configuration("system wall clock invalid"))?;
    u64::try_from(time.as_millis()).map_err(|_| ExecutionError::Configuration("timestamp overflow"))
}
pub struct ExchangeSocket {
    socket: Socket,
    authentication: Authentication,
    mode: ExchangeSocketMode,
    environment: BinanceEnvironment,
    journal: IntentJournal,
    next_id: u64,
    auth_session: bool,
    poisoned: bool,
}
impl ExchangeSocket {
    /// Only new journals can be used. A crash, disconnect or timeout
    /// requires REST+stream reconciliation before another session is
    /// allowed. No implicit resume/resend, ever.
    pub async fn connect(
        environment: BinanceEnvironment,
        authentication: Authentication,
        mode: ExchangeSocketMode,
        journal_path: &Path,
    ) -> Result<Self, ExecutionError> {
        if environment == BinanceEnvironment::Mainnet && mode != ExchangeSocketMode::InspectionOnly
        {
            return Err(ExecutionError::Disabled);
        }
        if mode == ExchangeSocketMode::TestnetSupervised {
            // Intentionally requires a second operator-controlled setting;
            // the next milestone must bind it to verified account mode,
            // reconciliation, kill path and reserved risk exposure.
            if std::env::var("TRADING_LAB_TESTNET_ROUTING").ok().as_deref()
                != Some("I_ACCEPT_TESTNET_ORDER_SUBMISSION")
            {
                return Err(ExecutionError::Disabled);
            }
        }
        let journal = IntentJournal::create(journal_path)?;
        let endpoint = crate::endpoint(environment);
        let (mut socket, _) = timeout(Duration::from_secs(10), connect_async(endpoint))
            .await
            .map_err(|_| ExecutionError::Transport)?
            .map_err(|_| ExecutionError::Transport)?;
        let mut session = false;
        if authentication.mode() == SigningMode::Ed25519 {
            let login = authentication.session_logon(1, clock_ms()?)?;
            socket
                .send(Message::Text(login.to_json().into()))
                .await
                .map_err(|_| ExecutionError::Transport)?;
            let result = timeout(Duration::from_secs(8), socket.next())
                .await
                .map_err(|_| ExecutionError::Unauthorized)?;
            let response = match result {
                Some(Ok(Message::Text(raw))) => raw,
                _ => return Err(ExecutionError::Unauthorized),
            };
            let event: Value = serde_json::from_str(response.as_str())?;
            if event.get("id").and_then(Value::as_u64) != Some(1)
                || event.get("status").and_then(Value::as_u64) != Some(200)
            {
                // Some USD-M Futures API deployments do not support
                // session.logon. Fail closed, never downgrade credentials
                // to unsigned order sends.
                return Err(ExecutionError::Unauthorized);
            }
            session = true;
        }
        Ok(Self {
            socket,
            authentication,
            mode,
            environment,
            journal,
            next_id: 2,
            auth_session: session,
            poisoned: false,
        })
    }
    pub fn mode(&self) -> ExchangeSocketMode {
        self.mode
    }
    pub fn environment(&self) -> BinanceEnvironment {
        self.environment
    }
    fn request(
        &self,
        id: u64,
        method: &'static str,
        mut params: Map<String, Value>,
    ) -> Result<SignedRequest, ExecutionError> {
        if !self.auth_session {
            self.authentication.sign_params(&mut params, clock_ms()?)?;
        }
        Ok(SignedRequest { id, method, params })
    }
    async fn send_and_confirm(
        &mut self,
        id: u64,
        body: String,
    ) -> Result<OrderWireEvent, ExecutionError> {
        // A successfully transmitted JSON-RPC request is NOT an order
        // fill. Only ORDER_TRADE_UPDATE in the separate private stream
        // can advance inventory/account PnL.
        if self.socket.send(Message::Text(body.into())).await.is_err() {
            self.poisoned = true;
            return Err(ExecutionError::UnexpectedState);
        }
        let result = timeout(Duration::from_secs(7), async {
            loop {
                match self.socket.next().await {
                    Some(Ok(Message::Text(raw))) => {
                        let v: Value = serde_json::from_str(raw.as_str())?;
                        if v.get("id").and_then(Value::as_u64) != Some(id) {
                            self.poisoned = true;
                            return Err(ExecutionError::UnexpectedState);
                        }
                        let code = v.get("status").and_then(Value::as_u64).unwrap_or(0);
                        if code == 200 {
                            let order_id = v
                                .get("result")
                                .and_then(|o| o.get("orderId"))
                                .and_then(Value::as_u64);
                            return Ok(OrderWireEvent::Accepted {
                                id,
                                exchange_order_id: order_id,
                            });
                        }
                        let error = v
                            .get("error")
                            .and_then(|o| o.get("code"))
                            .and_then(Value::as_i64)
                            .unwrap_or(-1);
                        return Ok(OrderWireEvent::Rejected { id, code: error });
                    }
                    Some(Ok(Message::Ping(data))) => {
                        self.socket
                            .send(Message::Pong(data))
                            .await
                            .map_err(|_| ExecutionError::Transport)?;
                    }
                    Some(Ok(Message::Pong(_))) => {}
                    _ => return Err(ExecutionError::UnexpectedState),
                }
            }
        })
        .await;
        match result {
            Ok(Ok(v)) => Ok(v),
            _ => {
                self.poisoned = true;
                Err(ExecutionError::UnexpectedState)
            }
        }
    }
    /// Real Testnet order transport. A call journal is synced before the
    /// network write. Mainnet and unarmed sessions are permanently blocked
    /// until Part 3 supplies independent approval and account verification.
    pub async fn place(&mut self, order: ExchangeOrder) -> Result<OrderWireEvent, ExecutionError> {
        if self.mode != ExchangeSocketMode::TestnetSupervised || self.poisoned {
            return Err(ExecutionError::Disabled);
        }
        let id = self.next_id;
        self.next_id = id.checked_add(1).ok_or(ExecutionError::UnexpectedState)?;
        let request = self.request(id, "order.place", order.params)?;
        self.journal.record(id, "place", order.client_id)?;
        self.send_and_confirm(id, request.to_json()).await
    }
    pub async fn cancel(
        &mut self,
        symbol: &str,
        client_id: u64,
    ) -> Result<OrderWireEvent, ExecutionError> {
        if self.mode != ExchangeSocketMode::TestnetSupervised || self.poisoned {
            return Err(ExecutionError::Disabled);
        }
        let id = self.next_id;
        self.next_id = id.checked_add(1).ok_or(ExecutionError::UnexpectedState)?;
        let params = cancel_params(symbol, client_id)?;
        let request = self.request(id, "order.cancel", params)?;
        self.journal.record(id, "cancel", client_id)?;
        self.send_and_confirm(id, request.to_json()).await
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn mainnet_mode_is_sealed_and_order_ack_is_not_fill() {
        assert_eq!(
            crate::endpoint(BinanceEnvironment::Mainnet),
            "wss://ws-fapi.binance.com/ws-fapi/v1"
        );
        assert_ne!(
            OrderWireEvent::Accepted {
                id: 1,
                exchange_order_id: Some(2)
            },
            OrderWireEvent::Uncertain { id: 1 }
        );
    }
}
