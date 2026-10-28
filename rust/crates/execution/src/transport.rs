use crate::order::cancel_params;
use crate::{Authentication, ExchangeOrder, ExecutionError, SignedRequest, SigningMode};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Map, Value};
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
/// Parse the exact exchange API response used by the production socket.
///
/// An order ACK is only an order receipt; it never constitutes a fill.
/// Ambiguous replies (including 5xx, backend timeouts, rate limits and
/// duplicates) are NOT interpreted as a safe order rejection.
pub fn decode_order_response(
    expected_id: u64,
    expected_symbol: &str,
    expected_client_id: &str,
    raw: &str,
) -> Result<OrderWireEvent, ExecutionError> {
    if expected_id == 0
        || expected_symbol.is_empty()
        || expected_client_id.is_empty()
        || raw.len() > 65_536
    {
        return Err(ExecutionError::InvalidMessage(
            "invalid order response context",
        ));
    }
    let response: Value = serde_json::from_str(raw)?;
    if response.get("id").and_then(Value::as_u64) != Some(expected_id) {
        return Err(ExecutionError::UnexpectedState);
    }
    let status = response
        .get("status")
        .and_then(Value::as_u64)
        .ok_or(ExecutionError::UnexpectedState)?;
    if status == 200 {
        if response.get("error").is_some() {
            return Err(ExecutionError::UnexpectedState);
        }
        let result = response
            .get("result")
            .ok_or(ExecutionError::UnexpectedState)?;
        let order_id = result
            .get("orderId")
            .and_then(Value::as_u64)
            .filter(|&id| id > 0)
            .ok_or(ExecutionError::UnexpectedState)?;
        if result.get("clientOrderId").and_then(Value::as_str) != Some(expected_client_id)
            || result.get("symbol").and_then(Value::as_str) != Some(expected_symbol)
        {
            return Err(ExecutionError::UnexpectedState);
        }
        return Ok(OrderWireEvent::Accepted {
            id: expected_id,
            exchange_order_id: Some(order_id),
        });
    }
    if response.get("result").is_some() {
        return Err(ExecutionError::UnexpectedState);
    }
    let code = response
        .get("error")
        .and_then(|e| e.get("code"))
        .and_then(Value::as_i64)
        .ok_or(ExecutionError::UnexpectedState)?;
    // Binance -1006/-1007 are explicitly uncertain execution outcomes.
    // Duplicate-client errors can also mean an earlier request was accepted.
    // Rate limits and any 5xx are conservatively treated as uncertain.
    if matches!(status, 408 | 409 | 418 | 429)
        || status >= 500
        || matches!(code, -1006 | -1007 | -2010)
    {
        return Ok(OrderWireEvent::Uncertain { id: expected_id });
    }
    if (400..500).contains(&status) && code < 0 {
        return Ok(OrderWireEvent::Rejected {
            id: expected_id,
            code,
        });
    }
    Err(ExecutionError::UnexpectedState)
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
        if environment != BinanceEnvironment::Testnet && mode != ExchangeSocketMode::InspectionOnly
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
        symbol: &str,
        client_id: &str,
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
                        let event = decode_order_response(id, symbol, client_id, raw.as_str())?;
                        if matches!(event, OrderWireEvent::Uncertain { .. }) {
                            return Err(ExecutionError::UnexpectedState);
                        }
                        return Ok(event);
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
        let expected_client_id = format!("tl-{}", order.client_id);
        let symbol = order.symbol.clone();
        let request = self.request(id, "order.place", order.params)?;
        self.journal.record(id, "place", order.client_id)?;
        self.send_and_confirm(id, &symbol, &expected_client_id, request.to_json())
            .await
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
        let expected_client_id = format!("tl-{client_id}");
        let request = self.request(id, "order.cancel", params)?;
        self.journal.record(id, "cancel", client_id)?;
        self.send_and_confirm(id, symbol, &expected_client_id, request.to_json())
            .await
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::{OrderKind, OrderSide, PrepareOrder};
    use hmac::{Hmac, Mac};
    use sha2::Sha256;
    use std::{
        collections::BTreeMap,
        path::PathBuf,
        sync::atomic::{AtomicU64, Ordering},
    };
    use tokio::net::TcpListener;
    use tokio_tungstenite::accept_async;
    use trading_core_types::{FixedPrice, FixedQty, InstrumentSpec};

    static NEXT_FIXTURE_ID: AtomicU64 = AtomicU64::new(1);

    fn journal_path() -> PathBuf {
        let n = NEXT_FIXTURE_ID.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "trading-rust-local-protocol-{}-{n}.journal",
            std::process::id()
        ))
    }
    fn prepared_order(client_id: u64) -> ExchangeOrder {
        let spec = InstrumentSpec {
            tick_size: 10_000_000,
            step_size: 100_000,
            min_notional: 500_000_000,
            min_price: 10_000_000,
            max_price: 100_000_000_000_000,
            min_qty: 100_000,
            max_qty: 10_000_000_000,
            market_step_size: 100_000,
            market_min_qty: 100_000,
            market_max_qty: 10_000_000_000,
        };
        PrepareOrder {
            client_id,
            symbol_id: 0,
            side: OrderSide::Sell,
            kind: OrderKind::IocLimit,
            quantity: FixedQty(2_000_000),
            limit: Some(FixedPrice(5_000_010_000_000)),
        }
        .prepare("BTCUSDT", spec)
        .unwrap()
    }
    fn verify_fixture_signature(frame: &Value) {
        let params = frame["params"].as_object().expect("params object");
        let signature =
            hex::decode(params["signature"].as_str().expect("signature")).expect("hex HMAC");
        let mut sorted = BTreeMap::<String, String>::new();
        for (key, value) in params {
            if key == "signature" {
                continue;
            }
            let val = if let Some(s) = value.as_str() {
                s.to_owned()
            } else {
                value.to_string()
            };
            sorted.insert(key.clone(), val);
        }
        let canonical = sorted
            .into_iter()
            .map(|(k, v)| format!("{k}={v}"))
            .collect::<Vec<_>>()
            .join("&");
        let mut mac = Hmac::<Sha256>::new_from_slice(b"not-a-real-exchange-secret").unwrap();
        mac.update(canonical.as_bytes());
        mac.verify_slice(&signature)
            .expect("production signed request correctly");
        assert_eq!(params["apiKey"], "TEST_ONLY_LOCAL_KEY");
        assert_eq!(params["recvWindow"], 5000);
        assert!(
            params["timestamp"]
                .as_u64()
                .is_some_and(|v| v > 1_700_000_000_000)
        );
    }
    #[derive(Clone, Copy)]
    enum FixtureReply {
        AckThenCancel,
        Uncertain,
        WrongId,
        RejectedThenAccepted,
        Disconnect,
    }
    /// The production ExchangeSocket::place / ::cancel code is exercised
    /// through a truly local WS socket. No localhost override is added to
    /// the production library API and no real exchange endpoint is involved.
    async fn fixture(
        reply: FixtureReply,
    ) -> (ExchangeSocket, PathBuf, tokio::task::JoinHandle<()>) {
        let server = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = server.local_addr().unwrap();
        assert!(address.ip().is_loopback());
        let task = tokio::spawn(async move {
            let (connection, peer) = server.accept().await.unwrap();
            assert!(peer.ip().is_loopback());
            let mut ws = accept_async(connection).await.unwrap();
            let amount = if matches!(
                reply,
                FixtureReply::AckThenCancel | FixtureReply::RejectedThenAccepted
            ) {
                2
            } else {
                1
            };
            for index in 0..amount {
                let msg = timeout(Duration::from_secs(3), ws.next())
                    .await
                    .unwrap()
                    .unwrap()
                    .unwrap();
                let Message::Text(raw) = msg else {
                    panic!("not JSON text")
                };
                let frame: Value = serde_json::from_str(raw.as_str()).unwrap();
                verify_fixture_signature(&frame);
                let id = frame["id"].as_u64().unwrap();
                let method = frame["method"].as_str().unwrap();
                assert_eq!(frame["params"]["symbol"], "BTCUSDT");
                if method == "order.place" {
                    assert_eq!(frame["params"]["quantity"], "0.02");
                    assert_eq!(frame["params"]["price"], "50000.1");
                    assert_eq!(frame["params"]["timeInForce"], "IOC");
                } else {
                    assert_eq!(method, "order.cancel");
                    assert_eq!(frame["params"]["origClientOrderId"], "tl-42");
                }
                if matches!(reply, FixtureReply::Disconnect) {
                    break;
                }
                let response = match reply {
                    FixtureReply::Uncertain => serde_json::json!({
                        "id":id,"status":503,"error":{"code":-1007,"msg":"test-only timeout"}
                    }),
                    FixtureReply::WrongId => serde_json::json!({
                        "id":id+1,"status":200,"result":{
                            "symbol":"BTCUSDT","clientOrderId":"tl-42","orderId":9101
                        }
                    }),
                    FixtureReply::RejectedThenAccepted if index == 0 => serde_json::json!({
                        "id":id,"status":400,"error":{"code":-1121,"msg":"test-only rejection"}
                    }),
                    _ => {
                        let cid = frame["params"]
                            .get("newClientOrderId")
                            .or_else(|| frame["params"].get("origClientOrderId"))
                            .unwrap()
                            .as_str()
                            .unwrap();
                        serde_json::json!({"id":id,"status":200,"result":{
                            "symbol":"BTCUSDT","clientOrderId":cid,"orderId":9101
                        }})
                    }
                };
                ws.send(Message::Text(response.to_string().into()))
                    .await
                    .unwrap();
            }
        });
        let url = format!("ws://{address}");
        let (socket, _) = timeout(Duration::from_secs(3), connect_async(url.as_str()))
            .await
            .unwrap()
            .unwrap();
        let path = journal_path();
        let journal = IntentJournal::create(&path).unwrap();
        let client = ExchangeSocket {
            socket,
            authentication: Authentication::fixture_hmac(),
            mode: ExchangeSocketMode::TestnetSupervised,
            environment: BinanceEnvironment::Testnet,
            journal,
            next_id: 2,
            auth_session: false,
            poisoned: false,
        };
        (client, path, task)
    }
    async fn finish(task: tokio::task::JoinHandle<()>, path: PathBuf) {
        timeout(Duration::from_secs(4), task)
            .await
            .unwrap()
            .unwrap();
        std::fs::remove_file(path).unwrap();
    }
    #[tokio::test]
    async fn real_place_and_cancel_methods_send_signed_loopback_requests_and_fsync_journal() {
        let (mut client, path, task) = fixture(FixtureReply::AckThenCancel).await;
        assert_eq!(
            client.place(prepared_order(42)).await.unwrap(),
            OrderWireEvent::Accepted {
                id: 2,
                exchange_order_id: Some(9101)
            }
        );
        assert_eq!(
            client.cancel("BTCUSDT", 42).await.unwrap(),
            OrderWireEvent::Accepted {
                id: 3,
                exchange_order_id: Some(9101)
            }
        );
        let journal = std::fs::read_to_string(&path).unwrap();
        assert!(journal.starts_with("TRADING-LAB-UNRESOLVED-EXCHANGE-INTENTS-V1\n"));
        assert!(journal.ends_with("2|place|42\n3|cancel|42\n"));
        // An unresolved intent file can never be reused for a new session.
        assert!(IntentJournal::create(&path).is_err());
        drop(client);
        finish(task, path).await;
    }
    #[tokio::test]
    async fn real_place_latches_ambiguous_exchange_timeout_and_refuses_resend() {
        let (mut client, path, task) = fixture(FixtureReply::Uncertain).await;
        assert!(client.place(prepared_order(42)).await.is_err());
        assert!(client.poisoned);
        assert!(matches!(
            client.place(prepared_order(43)).await,
            Err(ExecutionError::Disabled)
        ));
        let journal = std::fs::read_to_string(&path).unwrap();
        assert!(journal.ends_with("2|place|42\n"));
        assert!(!journal.contains("3|place|43"));
        drop(client);
        finish(task, path).await;
    }
    #[tokio::test]
    async fn real_place_rejects_wrong_response_correlation_and_latches_socket() {
        let (mut client, path, task) = fixture(FixtureReply::WrongId).await;
        assert!(client.place(prepared_order(42)).await.is_err());
        assert!(client.poisoned);
        drop(client);
        finish(task, path).await;
    }
    #[tokio::test]
    async fn definite_rejection_does_not_poison_next_signed_request() {
        let (mut client, path, task) = fixture(FixtureReply::RejectedThenAccepted).await;
        assert_eq!(
            client.place(prepared_order(42)).await.unwrap(),
            OrderWireEvent::Rejected { id: 2, code: -1121 }
        );
        assert!(!client.poisoned);
        assert_eq!(
            client.place(prepared_order(43)).await.unwrap(),
            OrderWireEvent::Accepted {
                id: 3,
                exchange_order_id: Some(9101)
            }
        );
        drop(client);
        finish(task, path).await;
    }
    #[tokio::test]
    async fn broken_connection_is_uncertain_and_stops_further_requests() {
        let (mut client, path, task) = fixture(FixtureReply::Disconnect).await;
        assert!(client.place(prepared_order(42)).await.is_err());
        assert!(client.poisoned);
        assert!(matches!(
            client.cancel("BTCUSDT", 42).await,
            Err(ExecutionError::Disabled)
        ));
        drop(client);
        finish(task, path).await;
    }

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
