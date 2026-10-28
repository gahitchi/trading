use crate::ExecutionError;
use futures_util::{SinkExt, StreamExt};
use rtrb::{Consumer, Producer, RingBuffer};
use serde_json::Value;
use std::{str::FromStr, time::Duration};
use tokio::{sync::mpsc, time::timeout};
use tokio_tungstenite::{connect_async, tungstenite::Message};
use trading_core_types::{ExecutionReportEvent, ExecutionStatus, FixedPrice, VenueId};
use trading_market_data::{BinanceEnvironment, InstrumentSpecTable};

pub type ExecutionReportProducer = Producer<ExecutionReportEvent>;
pub type ExecutionReportConsumer = Consumer<ExecutionReportEvent>;
pub fn execution_report_ring(
    capacity: usize,
) -> Result<(ExecutionReportProducer, ExecutionReportConsumer), ExecutionError> {
    if !(2..=65_536).contains(&capacity) {
        return Err(ExecutionError::Configuration(
            "invalid execution ring capacity",
        ));
    }
    Ok(RingBuffer::new(capacity))
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccountBalance {
    pub asset: String,
    pub wallet_balance_raw: i64,
    pub cross_wallet_balance_raw: i64,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccountPosition {
    pub symbol: String,
    pub position_side: String,
    pub position_amount_raw: i64,
    pub entry_price_raw: i64,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccountUpdate {
    pub event_ms: u64,
    pub transaction_ms: u64,
    pub reason: String,
    pub balances: Vec<AccountBalance>,
    pub positions: Vec<AccountPosition>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExchangeFill {
    pub event: ExecutionReportEvent,
    pub client_order_id: String,
    pub exchange_order_id: u64,
    pub trade_id: u64,
    pub commission_asset: String,
    pub realized_pnl_asset: String,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PrivateEvent {
    Order(ExchangeFill),
    Account(AccountUpdate),
    ReconciliationRequired(&'static str),
    Connected,
    Disconnected,
}
fn value<'a>(v: &'a Value, k: &str) -> Result<&'a Value, ExecutionError> {
    v.get(k).ok_or(ExecutionError::InvalidMessage(
        "missing required exchange field",
    ))
}
fn string<'a>(v: &'a Value, k: &str) -> Result<&'a str, ExecutionError> {
    value(v, k)?
        .as_str()
        .ok_or(ExecutionError::InvalidMessage("expected string field"))
}
fn uint(v: &Value, k: &str) -> Result<u64, ExecutionError> {
    value(v, k)?
        .as_u64()
        .ok_or(ExecutionError::InvalidMessage("expected integer field"))
}
fn exact(raw: &str) -> Result<i64, ExecutionError> {
    Ok(FixedPrice::from_str(raw)?.0)
}
fn millis_ns(value: u64) -> Result<u64, ExecutionError> {
    value
        .checked_mul(1_000_000)
        .ok_or(ExecutionError::InvalidMessage(
            "exchange timestamp overflow",
        ))
}
pub fn parse_private_event(
    raw: &str,
    table: &InstrumentSpecTable,
) -> Result<PrivateEvent, ExecutionError> {
    if raw.len() > 1_048_576 {
        return Err(ExecutionError::InvalidMessage("oversized private event"));
    }
    let root: Value = serde_json::from_str(raw)?;
    let event = if root.get("data").is_some() {
        value(&root, "data")?
    } else if root.get("event").is_some() {
        value(&root, "event")?
    } else {
        &root
    };
    match string(event, "e")? {
        "ORDER_TRADE_UPDATE" => {
            let o = value(event, "o")?;
            let sym = string(o, "s")?;
            let instrument = table
                .find(sym)
                .ok_or(ExecutionError::InvalidMessage("unknown order symbol"))?;
            let status = match string(o, "X")? {
                "NEW" => ExecutionStatus::New,
                "PARTIALLY_FILLED" => ExecutionStatus::PartiallyFilled,
                "FILLED" => ExecutionStatus::Filled,
                "CANCELED" => ExecutionStatus::Canceled,
                "EXPIRED" | "EXPIRED_IN_MATCH" => ExecutionStatus::Expired,
                "REJECTED" => ExecutionStatus::Rejected,
                _ => return Ok(PrivateEvent::ReconciliationRequired("unknown order status")),
            };
            let side = match string(o, "S")? {
                "BUY" => 1,
                "SELL" => 2,
                _ => return Err(ExecutionError::InvalidMessage("invalid order side")),
            };
            let c = string(o, "c")?.to_owned();
            // Binance can generate autoclose and ADL IDs that are not
            // numeric. Never drop these liquidation events; escalate.
            let client_order_id = match c.strip_prefix("tl-").and_then(|s| s.parse::<u64>().ok()) {
                Some(v) if v != 0 => v,
                _ => {
                    return Ok(PrivateEvent::ReconciliationRequired(
                        "unknown, external or liquidation order ID",
                    ));
                }
            };
            let last = exact(string(o, "l")?)?;
            let cumulative = exact(string(o, "z")?)?;
            let fill_price = exact(string(o, "L")?)?;
            let commission = exact(string(o, "n")?)?;
            let realized = exact(string(o, "rp")?)?;
            let asset = o.get("N").and_then(Value::as_str).unwrap_or("");
            let price_asset = instrument
                .symbol
                .strip_suffix("USDT")
                .map(|_| "USDT")
                .ok_or(ExecutionError::InvalidMessage("invalid quote asset"))?;
            if last < 0
                || cumulative < last
                || commission < 0
                || (last > 0 && fill_price <= 0)
                || (status == ExecutionStatus::Filled && cumulative <= 0)
            {
                return Err(ExecutionError::InvalidMessage("inconsistent fill values"));
            }
            // An event with an unknown fee asset cannot safely settle to
            // USDT. It is still delivered, marked, and requires reconciliation.
            let currency_verified = last == 0 || asset == price_asset;
            let e = ExecutionReportEvent {
                exchange_ts_ns: millis_ns(uint(event, "E")?)?,
                client_order_id,
                fill_price,
                last_filled_qty: last,
                cumulative_qty: cumulative,
                realized_pnl: realized,
                commission,
                symbol_id: instrument.symbol_id,
                status: status as u8,
                side,
                venue_id: VenueId::BinanceUsdM as u8,
                flags: if currency_verified { 1 } else { 0 },
                _padding: [0; 2],
            };
            Ok(PrivateEvent::Order(ExchangeFill {
                event: e,
                client_order_id: c,
                exchange_order_id: uint(o, "i")?,
                trade_id: uint(o, "t")?,
                commission_asset: asset.into(),
                realized_pnl_asset: price_asset.into(),
            }))
        }
        "ACCOUNT_UPDATE" => {
            let data = value(event, "a")?;
            let balances = value(data, "B")?
                .as_array()
                .ok_or(ExecutionError::InvalidMessage("invalid balance array"))?;
            let positions = value(data, "P")?
                .as_array()
                .ok_or(ExecutionError::InvalidMessage("invalid positions array"))?;
            if balances.len() > 100 || positions.len() > 512 {
                return Err(ExecutionError::InvalidMessage("oversized account update"));
            }
            let mut b = Vec::with_capacity(balances.len());
            for row in balances {
                b.push(AccountBalance {
                    asset: string(row, "a")?.to_owned(),
                    wallet_balance_raw: exact(string(row, "wb")?)?,
                    cross_wallet_balance_raw: exact(string(row, "cw")?)?,
                });
            }
            let mut p = Vec::with_capacity(positions.len());
            for row in positions {
                p.push(AccountPosition {
                    symbol: string(row, "s")?.to_owned(),
                    position_side: string(row, "ps")?.to_owned(),
                    position_amount_raw: exact(string(row, "pa")?)?,
                    entry_price_raw: exact(string(row, "ep")?)?,
                });
            }
            Ok(PrivateEvent::Account(AccountUpdate {
                event_ms: uint(event, "E")?,
                transaction_ms: uint(event, "T")?,
                reason: string(data, "m")?.to_owned(),
                balances: b,
                positions: p,
            }))
        }
        "listenKeyExpired" => Ok(PrivateEvent::ReconciliationRequired("listenKey expired")),
        "MARGIN_CALL" => Ok(PrivateEvent::ReconciliationRequired("margin call")),
        "ACCOUNT_CONFIG_UPDATE" => Ok(PrivateEvent::ReconciliationRequired(
            "account settings changed",
        )),
        _ => Ok(PrivateEvent::ReconciliationRequired(
            "unknown private event type",
        )),
    }
}
pub struct UserDataStreamManager {
    client: reqwest::Client,
    api_key: String,
    environment: BinanceEnvironment,
    table: InstrumentSpecTable,
}
impl UserDataStreamManager {
    pub fn new(api_key: String, table: InstrumentSpecTable) -> Result<Self, ExecutionError> {
        if api_key.is_empty()
            || api_key.len() > 512
            || !api_key.bytes().all(|c| c.is_ascii_graphic())
        {
            return Err(ExecutionError::Configuration("invalid API key"));
        }
        let client = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(10))
            .build()?;
        Ok(Self {
            client,
            api_key,
            environment: table.environment(),
            table,
        })
    }
    fn base(&self) -> &'static str {
        self.environment.rest()
    }
    fn ws_base(&self) -> &'static str {
        match self.environment {
            BinanceEnvironment::Mainnet => "wss://fstream.binance.com/private/ws/",
            BinanceEnvironment::Testnet => "wss://stream.binancefuture.com/ws/",
        }
    }
    async fn start(&self) -> Result<String, ExecutionError> {
        let response = self
            .client
            .post(format!("{}/fapi/v1/listenKey", self.base()))
            .header("X-MBX-APIKEY", &self.api_key)
            .send()
            .await?
            .error_for_status()?;
        let body: Value = response.json().await?;
        let key = string(&body, "listenKey")?;
        if key.len() < 16 || key.len() > 512 || !key.bytes().all(|c| c.is_ascii_alphanumeric()) {
            return Err(ExecutionError::InvalidMessage("invalid listenKey"));
        }
        Ok(key.to_owned())
    }
    async fn keepalive(&self) -> Result<(), ExecutionError> {
        self.client
            .put(format!("{}/fapi/v1/listenKey", self.base()))
            .header("X-MBX-APIKEY", &self.api_key)
            .send()
            .await?
            .error_for_status()?;
        Ok(())
    }
    /// Actual authenticated private-user feed. It NEVER fabricates fills.
    /// Any socket loss emits Disconnected, obliging Core 2 to halt and
    /// perform a full REST/account reconciliation before using new events.
    pub async fn run(&self, tx: mpsc::Sender<PrivateEvent>) -> Result<(), ExecutionError> {
        let mut retry_seconds = 1u64;
        loop {
            let key = match self.start().await {
                Ok(key) => key,
                Err(_) => {
                    tx.send(PrivateEvent::Disconnected)
                        .await
                        .map_err(|_| ExecutionError::UnexpectedState)?;
                    tokio::time::sleep(Duration::from_secs(retry_seconds)).await;
                    retry_seconds = (retry_seconds * 2).min(30);
                    continue;
                }
            };
            let url = format!("{}{}", self.ws_base(), key);
            // Never log URL: its path is a bearer token (listenKey).
            let connected = timeout(Duration::from_secs(10), connect_async(url.as_str())).await;
            let mut socket = match connected {
                Ok(Ok((ws, _))) => ws,
                _ => {
                    tx.send(PrivateEvent::Disconnected)
                        .await
                        .map_err(|_| ExecutionError::UnexpectedState)?;
                    tokio::time::sleep(Duration::from_secs(retry_seconds)).await;
                    retry_seconds = (retry_seconds * 2).min(30);
                    continue;
                }
            };
            retry_seconds = 1;
            tx.send(PrivateEvent::Connected)
                .await
                .map_err(|_| ExecutionError::UnexpectedState)?;
            let mut heartbeat = tokio::time::interval(Duration::from_secs(25 * 60));
            heartbeat.tick().await;
            loop {
                tokio::select! {
                    _=heartbeat.tick()=>{
                        if self.keepalive().await.is_err() {break;}
                    }
                    maybe=socket.next()=>{
                        match maybe {
                            Some(Ok(Message::Text(payload)))=>{
                                match parse_private_event(payload.as_str(),&self.table){
                                    Ok(event)=>{
                                        let critical=matches!(event,
                                            PrivateEvent::ReconciliationRequired(_));
                                        tx.send(event).await
                                            .map_err(|_|ExecutionError::UnexpectedState)?;
                                        if critical {break;}
                                    }
                                    Err(_)=>{
                                        tx.send(PrivateEvent::ReconciliationRequired(
                                            "malformed private payload")).await
                                            .map_err(|_|ExecutionError::UnexpectedState)?;
                                        break;
                                    }
                                }
                            }
                            Some(Ok(Message::Ping(bytes)))=>{
                                if socket.send(Message::Pong(bytes)).await.is_err(){break;}
                            }
                            Some(Ok(Message::Pong(_)))=>{},
                            Some(Ok(Message::Close(_)))|None|Some(Err(_))=>break,
                            Some(Ok(_))=>{
                                tx.send(PrivateEvent::ReconciliationRequired(
                                    "unexpected private websocket frame")).await
                                    .map_err(|_|ExecutionError::UnexpectedState)?;
                                break;
                            },
                        }
                    }
                }
            }
            tx.send(PrivateEvent::Disconnected)
                .await
                .map_err(|_| ExecutionError::UnexpectedState)?;
            tokio::time::sleep(Duration::from_secs(retry_seconds)).await;
            retry_seconds = (retry_seconds * 2).min(30);
        }
    }
}

/// Dedicated Core 3 dispatcher: only this task produces into the 64-byte
/// SPSC execution-report ring. Account snapshots and trade identity/asset
/// provenance are emitted on separate bounded channels. No dropped fills:
/// saturation returns an error and must force Core 2 into reconciliation.
impl UserDataStreamManager {
    pub async fn run_into_ring(
        self,
        mut producer: ExecutionReportProducer,
        account_tx: mpsc::Sender<AccountUpdate>,
        audit_tx: mpsc::Sender<PrivateEvent>,
    ) -> Result<(), ExecutionError> {
        let (tx, mut rx) = mpsc::channel(256);
        let mut worker = tokio::spawn(async move { self.run(tx).await });
        loop {
            tokio::select! {
                item=rx.recv()=>{
                    match item {
                        Some(PrivateEvent::Order(report))=>{
                            producer.push(report.event).map_err(|_|ExecutionError::UnexpectedState)?;
                            audit_tx.send(PrivateEvent::Order(report)).await
                                .map_err(|_|ExecutionError::UnexpectedState)?;
                        }
                        Some(PrivateEvent::Account(account))=>{
                            account_tx.send(account).await
                                .map_err(|_|ExecutionError::UnexpectedState)?;
                        }
                        Some(other)=>{
                            audit_tx.send(other).await
                                .map_err(|_|ExecutionError::UnexpectedState)?;
                        }
                        None=>return Err(ExecutionError::UnexpectedState),
                    }
                }
                result=&mut worker=>{
                    let _=result;
                    return Err(ExecutionError::UnexpectedState);
                }
            }
        }
    }
}
