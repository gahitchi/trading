//! Deliberately isolated synthetic *test exchange*. Never deployed in the
//! live application and never binds outside the IPv4 loopback interface.
//! It uses Binance-shaped order WS-API ACKs and *separate* private WS events.
use futures_util::{SinkExt, StreamExt};
use hmac::{Hmac, Mac};
use serde_json::{Value, json};
use sha2::Sha256;
use std::{
    collections::{BTreeMap, BTreeSet},
    error::Error,
    fmt,
    str::FromStr,
    time::Duration,
};
use tokio::{net::TcpListener, sync::mpsc, time::timeout};
use tokio_tungstenite::{accept_async, connect_async, tungstenite::Message};
use trading_core_types::{FixedQty, SCALE};
use trading_execution::{PrivateEvent, parse_private_event};
use trading_market_data::{BinanceEnvironment, InstrumentSpecTable};

const TEST_KEY: &str = "TEST_ONLY_LOCAL_KEY";
const TEST_SECRET: &[u8] = b"not-a-real-exchange-secret";
const CLOCK: u64 = 1_700_000_000_000;
type HmacSha256 = Hmac<Sha256>;

#[derive(Debug)]
pub enum LabError {
    Protocol(&'static str),
    IO(std::io::Error),
    Ws(tokio_tungstenite::tungstenite::Error),
    Json(serde_json::Error),
}
impl fmt::Display for LabError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Protocol(p) => write!(f, "local protocol test: {p}"),
            Self::IO(_) => write!(f, "local IO failure"),
            Self::Ws(_) => write!(f, "local WS failure"),
            Self::Json(_) => write!(f, "invalid test JSON"),
        }
    }
}
impl Error for LabError {}
impl From<std::io::Error> for LabError {
    fn from(e: std::io::Error) -> Self {
        Self::IO(e)
    }
}
impl From<tokio_tungstenite::tungstenite::Error> for LabError {
    fn from(e: tokio_tungstenite::tungstenite::Error) -> Self {
        Self::Ws(e)
    }
}
impl From<serde_json::Error> for LabError {
    fn from(e: serde_json::Error) -> Self {
        Self::Json(e)
    }
}
fn signed_message(id: u64, method: &str, extra: &[(&str, &str)]) -> Value {
    let mut params = serde_json::Map::new();
    params.insert("apiKey".into(), json!(TEST_KEY));
    params.insert("timestamp".into(), json!(CLOCK));
    params.insert("recvWindow".into(), json!(5000));
    for &(key, val) in extra {
        params.insert(key.into(), json!(val));
    }
    let payload = canonical(&params).expect("local fixture canonical parameters");
    let mut mac = HmacSha256::new_from_slice(TEST_SECRET).expect("fixture HMAC key");
    mac.update(payload.as_bytes());
    params.insert(
        "signature".into(),
        json!(hex::encode(mac.finalize().into_bytes())),
    );
    json!({"id":id,"method":method,"params":params})
}
fn canonical(params: &serde_json::Map<String, Value>) -> Result<String, LabError> {
    let mut pairs = BTreeMap::new();
    for (key, val) in params {
        if key == "signature" {
            continue;
        }
        let value = if let Some(s) = val.as_str() {
            s.to_owned()
        } else if val.is_number() {
            val.to_string()
        } else {
            return Err(LabError::Protocol("noncanonical parameter type"));
        };
        if !key.bytes().all(|b| b.is_ascii_alphanumeric())
            || !value
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
        {
            return Err(LabError::Protocol("noncanonical signature payload"));
        }
        pairs.insert(key.clone(), value);
    }
    Ok(pairs
        .into_iter()
        .map(|(k, v)| format!("{k}={v}"))
        .collect::<Vec<_>>()
        .join("&"))
}
#[derive(Default)]
pub struct LocalVenue {
    seen: BTreeSet<String>,
}
pub struct Response {
    pub acknowledgement: Value,
    pub private_messages: Vec<Value>,
}
impl LocalVenue {
    /// Local fake order admission, only for network protocol regression
    /// tests. This must not be used by any production trading component.
    pub fn apply(&mut self, frame: &Value) -> Result<Response, LabError> {
        let id = frame
            .get("id")
            .and_then(Value::as_u64)
            .ok_or(LabError::Protocol("no numeric request id"))?;
        let method = frame
            .get("method")
            .and_then(Value::as_str)
            .ok_or(LabError::Protocol("missing method"))?;
        let params = frame
            .get("params")
            .and_then(Value::as_object)
            .ok_or(LabError::Protocol("missing request params"))?;
        let sig = params
            .get("signature")
            .and_then(Value::as_str)
            .ok_or(LabError::Protocol("unsigned request"))?;
        let signature = hex::decode(sig).map_err(|_| LabError::Protocol("malformed signature"))?;
        let payload = canonical(params)?;
        let mut mac = HmacSha256::new_from_slice(TEST_SECRET)
            .map_err(|_| LabError::Protocol("invalid local fixture key"))?;
        mac.update(payload.as_bytes());
        if mac.verify_slice(&signature).is_err()
            || params.get("apiKey").and_then(Value::as_str) != Some(TEST_KEY)
            || params.get("timestamp").and_then(Value::as_u64) != Some(CLOCK)
            || params.get("recvWindow").and_then(Value::as_u64) != Some(5000)
        {
            return Err(LabError::Protocol("invalid request authentication"));
        }
        if method != "order.place" {
            return Err(LabError::Protocol("unsupported method"));
        }
        if params.get("symbol").and_then(Value::as_str) != Some("BTCUSDT")
            || params.get("side").and_then(Value::as_str) != Some("SELL")
            || params.get("type").and_then(Value::as_str) != Some("LIMIT")
            || params.get("timeInForce").and_then(Value::as_str) != Some("IOC")
        {
            return Err(LabError::Protocol("unsupported synthetic order contract"));
        }
        let cid = params
            .get("newClientOrderId")
            .and_then(Value::as_str)
            .ok_or(LabError::Protocol("no client id"))?;
        if !cid.starts_with("tl-") || cid.len() > 36 {
            return Err(LabError::Protocol("unexpected local client id"));
        }
        let quantity = params
            .get("quantity")
            .and_then(Value::as_str)
            .ok_or(LabError::Protocol("no quantity"))?;
        let qty = FixedQty::from_str(quantity)
            .map_err(|_| LabError::Protocol("invalid quantity"))?
            .0;
        // Deterministic IOC fixture: 50% of quantity fills, remainder
        // expires. Numbers and events are SYNTHETIC and test-only.
        if qty <= 0 || qty % 200_000 != 0 {
            return Err(LabError::Protocol("fixture requires even 0.001 lots"));
        }
        let price = params
            .get("price")
            .and_then(Value::as_str)
            .ok_or(LabError::Protocol("no limit"))?;
        let p = trading_core_types::FixedPrice::from_str(price)
            .map_err(|_| LabError::Protocol("invalid price"))?
            .0;
        if p <= 0 {
            return Err(LabError::Protocol("nonpositive price"));
        }
        if !self.seen.insert(cid.into()) {
            return Ok(Response {
                acknowledgement: json!({
                    "id":id,"status":400,"error":{"code":-2010,"msg":"duplicate synthetic client id"}
                }),
                private_messages: vec![],
            });
        }
        let synthetic_order_id = 9001_u64;
        let half = qty / 2;
        let exact_qty = format_fixed(half);
        let total_qty = format_fixed(qty);
        let private_messages = vec![
            json!({"e":"ORDER_TRADE_UPDATE","E":CLOCK+1,"T":CLOCK+1,
                "o":{"s":"BTCUSDT","c":cid,"i":synthetic_order_id,
                    "S":"SELL","X":"NEW","x":"NEW","L":"0","l":"0",
                    "z":"0","n":"0","N":"USDT","rp":"0","t":0}}),
            json!({"e":"ORDER_TRADE_UPDATE","E":CLOCK+2,"T":CLOCK+2,
                "o":{"s":"BTCUSDT","c":cid,"i":synthetic_order_id,
                    "S":"SELL","X":"PARTIALLY_FILLED","x":"TRADE",
                    "L":price,"l":exact_qty,"z":exact_qty,
                    "n":"0.01","N":"USDT","rp":"0.03","t":8001}}),
            json!({"e":"ORDER_TRADE_UPDATE","E":CLOCK+3,"T":CLOCK+3,
                "o":{"s":"BTCUSDT","c":cid,"i":synthetic_order_id,
                    "S":"SELL","X":"EXPIRED","x":"EXPIRED","L":"0",
                    "l":"0","z":exact_qty,
                    "n":"0","N":"USDT","rp":"0","t":0}}),
            json!({"e":"ACCOUNT_UPDATE","E":CLOCK+4,"T":CLOCK+4,
                "a":{"m":"ORDER","B":[{"a":"USDT","wb":"1000","cw":"1000"}],
                    "P":[{"s":"BTCUSDT","pa":format!("-{exact_qty}"),
                        "ep":price,"ps":"BOTH"}]}}),
        ];
        let ack = json!({"id":id,"status":200,"result":{
            "symbol":"BTCUSDT","clientOrderId":cid,"orderId":synthetic_order_id,
            "origQty":total_qty,"status":"NEW"
        }});
        Ok(Response {
            acknowledgement: ack,
            private_messages,
        })
    }
}
fn format_fixed(raw: i64) -> String {
    let whole = raw / SCALE;
    let frac = format!("{:08}", raw % SCALE);
    format!("{whole}.{}", frac.trim_end_matches('0'))
}
fn spec_table() -> Result<InstrumentSpecTable, LabError> {
    let value = json!({"symbols":[{"symbol":"BTCUSDT","status":"TRADING",
        "contractType":"PERPETUAL","quoteAsset":"USDT","marginAsset":"USDT",
        "filters":[
            {"filterType":"PRICE_FILTER","tickSize":"0.10","minPrice":"0.10","maxPrice":"1000000"},
            {"filterType":"LOT_SIZE","stepSize":"0.001","minQty":"0.001","maxQty":"100"},
            {"filterType":"MARKET_LOT_SIZE","stepSize":"0.001","minQty":"0.001","maxQty":"100"},
            {"filterType":"MIN_NOTIONAL","notional":"5.0"}]}]});
    InstrumentSpecTable::from_exchange_info(
        &value,
        &["BTCUSDT".into()],
        BinanceEnvironment::Testnet,
    )
    .map_err(|_| LabError::Protocol("fixture instrument filters"))
}

/// Live network loopback (TLS not required on the local fixture). Separate
/// API and user WS channels reflect the venue's ACK/filled-report separation.
/// Both ports are OS-assigned and bound ONLY to 127.0.0.1.
pub async fn run_selftest() -> Result<(), LabError> {
    let api = TcpListener::bind("127.0.0.1:0").await?;
    let user = TcpListener::bind("127.0.0.1:0").await?;
    let api_url = format!("ws://{}", api.local_addr()?);
    let user_url = format!("ws://{}", user.local_addr()?);
    let (tx, mut rx) = mpsc::channel::<Value>(16);
    let private_task = tokio::spawn(async move {
        let (socket, _) = user.accept().await?;
        let mut ws = accept_async(socket).await?;
        while let Some(event) = rx.recv().await {
            ws.send(Message::Text(event.to_string().into())).await?;
        }
        Ok::<(), LabError>(())
    });
    let api_task = tokio::spawn(async move {
        let (socket, _) = api.accept().await?;
        let mut ws = accept_async(socket).await?;
        let mut engine = LocalVenue::default();
        while let Some(message) = ws.next().await {
            let message = message?;
            if let Message::Text(text) = message {
                let frame: Value = serde_json::from_str(text.as_str())?;
                let response = engine.apply(&frame)?;
                ws.send(Message::Text(response.acknowledgement.to_string().into()))
                    .await?;
                for private in response.private_messages {
                    tx.send(private)
                        .await
                        .map_err(|_| LabError::Protocol("user stream closed"))?;
                }
            }
        }
        Ok::<(), LabError>(())
    });
    let result = timeout(Duration::from_secs(8), async {
        let (mut private_ws, _) = connect_async(user_url.as_str()).await?;
        let (mut api_ws, _) = connect_async(api_url.as_str()).await?;
        let params = [
            ("symbol", "BTCUSDT"),
            ("side", "SELL"),
            ("type", "LIMIT"),
            ("timeInForce", "IOC"),
            ("newClientOrderId", "tl-42"),
            ("quantity", "0.020"),
            ("price", "50000.10"),
        ];
        api_ws
            .send(Message::Text(
                signed_message(77, "order.place", &params)
                    .to_string()
                    .into(),
            ))
            .await?;
        let ack = private_next(&mut api_ws).await?;
        if ack["status"] != 200 || ack["id"] != 77 || ack["result"]["orderId"] != 9001 {
            return Err(LabError::Protocol("order ACK mismatch"));
        }
        // No fill can be inferred from the order ACK.
        let table = spec_table()?;
        let mut fills = 0;
        let mut terminal = 0;
        let mut account_updates = 0;
        for _ in 0..4 {
            let frame = private_next(&mut private_ws).await?;
            let parsed = parse_private_event(&frame.to_string(), &table)
                .map_err(|_| LabError::Protocol("actual private event parser rejected fixture"))?;
            match parsed {
                PrivateEvent::Order(ref event) if event.event.last_filled_qty > 0 => {
                    if event.trade_id != 8001 || event.event.last_filled_qty != 1_000_000 {
                        return Err(LabError::Protocol("incorrect trade attribution"));
                    }
                    fills += 1;
                }
                PrivateEvent::Order(ref event) if event.event.status == 5 => terminal += 1,
                PrivateEvent::Account(_) => account_updates += 1,
                PrivateEvent::Order(_) => {}
                _ => return Err(LabError::Protocol("unexpected private event")),
            }
        }
        if fills != 1 || terminal != 1 || account_updates != 1 {
            return Err(LabError::Protocol("missing private order/account messages"));
        }
        api_ws
            .send(Message::Text(
                signed_message(78, "order.place", &params)
                    .to_string()
                    .into(),
            ))
            .await?;
        let replay = private_next(&mut api_ws).await?;
        if replay["status"] != 400 || replay["error"]["code"] != -2010 {
            return Err(LabError::Protocol("duplicate order not rejected"));
        }
        // A bad signature MUST terminate the local session and surface
        // an error. No positive ACK should be emitted.
        let mut invalid = signed_message(79, "order.place", &params);
        invalid["params"]["signature"] = json!("0000");
        api_ws
            .send(Message::Text(invalid.to_string().into()))
            .await?;
        let result = timeout(Duration::from_millis(200), api_ws.next()).await;
        if matches!(result, Ok(Some(Ok(Message::Text(_))))) {
            return Err(LabError::Protocol("bad signature received an ACK"));
        }
        Ok::<(), LabError>(())
    })
    .await;
    api_task.abort();
    private_task.abort();
    result.map_err(|_| LabError::Protocol("local network test timed out"))?
}
async fn private_next<T>(ws: &mut tokio_tungstenite::WebSocketStream<T>) -> Result<Value, LabError>
where
    T: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin,
{
    let next = timeout(Duration::from_secs(2), ws.next())
        .await
        .map_err(|_| LabError::Protocol("missing WebSocket frame"))?
        .ok_or(LabError::Protocol("WebSocket ended"))??;
    let Message::Text(text) = next else {
        return Err(LabError::Protocol("expected JSON text frame"));
    };
    Ok(serde_json::from_str(text.as_str())?)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn local_signature_tampering_is_rejected() {
        let mut venue = LocalVenue::default();
        let mut req = signed_message(
            1,
            "order.place",
            &[
                ("symbol", "BTCUSDT"),
                ("side", "SELL"),
                ("type", "LIMIT"),
                ("timeInForce", "IOC"),
                ("newClientOrderId", "tl-42"),
                ("quantity", "0.02"),
                ("price", "50000.10"),
            ],
        );
        req["params"]["quantity"] = json!("0.04");
        assert!(venue.apply(&req).is_err());
    }
    #[test]
    fn canonical_signature_independent_of_json_insertion_order() {
        let a = signed_message(
            1,
            "order.place",
            &[("symbol", "BTCUSDT"), ("price", "50000")],
        );
        let b = signed_message(
            1,
            "order.place",
            &[("price", "50000"), ("symbol", "BTCUSDT")],
        );
        assert_eq!(a["params"]["signature"], b["params"]["signature"]);
    }
    #[tokio::test]
    async fn isolated_network_sends_ack_then_real_shape_private_events() {
        run_selftest().await.unwrap();
    }
}
