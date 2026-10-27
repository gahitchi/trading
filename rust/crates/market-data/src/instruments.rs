use std::{sync::Arc, str::FromStr, time::Duration};
use trading_core_types::{FixedPrice, InstrumentSpec, NumericError, SCALE};
use serde_json::Value;
use crate::MarketDataError;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BinanceEnvironment { Mainnet, Testnet }
impl BinanceEnvironment {
    pub const fn rest(self) -> &'static str {
        match self {
            Self::Mainnet => "https://fapi.binance.com",
            Self::Testnet => "https://testnet.binancefuture.com",
        }
    }
    pub const fn ws_public(self) -> &'static str {
        match self {
            Self::Mainnet => "wss://fstream.binance.com/public",
            Self::Testnet => "wss://stream.binancefuture.com",
        }
    }
    pub const fn ws_market(self) -> &'static str {
        match self {
            Self::Mainnet => "wss://fstream.binance.com/market",
            Self::Testnet => "wss://stream.binancefuture.com",
        }
    }
}
#[derive(Debug, Clone)]
pub struct Instrument {
    pub symbol_id: u16,
    pub symbol: String,
    pub spec: InstrumentSpec,
}
#[derive(Debug, Clone)]
pub struct InstrumentSpecTable {
    instruments: Arc<[Instrument]>,
    environment: BinanceEnvironment,
}
impl InstrumentSpecTable {
    pub fn environment(&self) -> BinanceEnvironment { self.environment }
    pub fn instruments(&self) -> &[Instrument] { &self.instruments }
    pub fn by_id(&self, id: u16) -> Option<&Instrument> {
        self.instruments.get(usize::from(id))
    }
    pub fn find(&self, symbol: &str) -> Option<&Instrument> {
        self.instruments.iter().find(|x| x.symbol.eq_ignore_ascii_case(symbol))
    }
    pub fn from_exchange_info(value: &Value, requested: &[String],
        environment: BinanceEnvironment) -> Result<Self, MarketDataError>
    {
        if requested.is_empty() || requested.len() > 9 {
            return Err(MarketDataError::InvalidConfiguration("1..9 configured symbols required"));
        }
        let symbols = value.get("symbols").and_then(Value::as_array)
            .ok_or(MarketDataError::InvalidResponse("missing exchangeInfo symbols"))?;
        let mut seen = std::collections::HashSet::new();
        let mut entries = Vec::with_capacity(requested.len());
        for (id, raw) in requested.iter().enumerate() {
            if raw.is_empty() || raw.len() > 32 ||
                !raw.bytes().all(|x| x.is_ascii_uppercase() || x.is_ascii_digit())
                || !seen.insert(raw)
            {
                return Err(MarketDataError::InvalidConfiguration("invalid/duplicate symbol"));
            }
            let obj = symbols.iter().find(|v| v.get("symbol").and_then(Value::as_str)
                == Some(raw.as_str()))
                .ok_or(MarketDataError::InvalidResponse("requested symbol unavailable"))?;
            if obj.get("status").and_then(Value::as_str) != Some("TRADING")
                || obj.get("contractType").and_then(Value::as_str) != Some("PERPETUAL")
                || obj.get("quoteAsset").and_then(Value::as_str) != Some("USDT")
                || obj.get("marginAsset").and_then(Value::as_str) != Some("USDT")
            {
                return Err(MarketDataError::InvalidResponse(
                    "symbol must be trading USDT-quoted and USDT-margined perpetual"));
            }
            let filters = obj.get("filters").and_then(Value::as_array)
                .ok_or(MarketDataError::InvalidResponse("symbol filters missing"))?;
            let filter = |name: &str| -> Result<&Value, MarketDataError> {
                filters.iter().find(|v| v.get("filterType").and_then(Value::as_str)==Some(name))
                    .ok_or(MarketDataError::InvalidResponse("required symbol filter absent"))
            };
            let v = |o: &Value, key: &str| -> Result<i64, MarketDataError> {
                let src=o.get(key).and_then(Value::as_str)
                    .ok_or(MarketDataError::InvalidResponse("invalid filter decimal"))?;
                FixedPrice::from_str(src).map(|p|p.0)
                    .map_err(MarketDataError::Numeric)
            };
            let price = filter("PRICE_FILTER")?;
            let lot = filter("LOT_SIZE")?;
            let market = filter("MARKET_LOT_SIZE")?;
            let notional = filters.iter().find(|v| {
                matches!(v.get("filterType").and_then(Value::as_str),
                    Some("MIN_NOTIONAL" | "NOTIONAL"))
            }).ok_or(MarketDataError::InvalidResponse("no notional filter"))?;
            let min_notional = v(notional,"notional")
                .or_else(|_| v(notional,"minNotional"))?;
            let spec=InstrumentSpec {
                tick_size:v(price,"tickSize")?,
                min_price:v(price,"minPrice")?,max_price:v(price,"maxPrice")?,
                step_size:v(lot,"stepSize")?,
                min_qty:v(lot,"minQty")?,max_qty:v(lot,"maxQty")?,
                market_step_size:v(market,"stepSize")?,
                market_min_qty:v(market,"minQty")?,
                market_max_qty:v(market,"maxQty")?,
                min_notional,
            };
            spec.validate().map_err(|_|MarketDataError::InvalidResponse("invalid symbol spec"))?;
            // Binance sometimes disables MARKET_LOT_SIZE by reporting zero
            // increment. Never guess a replacement and silently route orders.
            entries.push(Instrument { symbol_id:id as u16,symbol:raw.clone(),spec });
        }
        Ok(Self {instruments: Arc::from(entries),environment})
    }
}
pub fn rest_client() -> Result<reqwest::Client, MarketDataError> {
    Ok(reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(10))
        .user_agent("trading-lab-rust-market-data/0.1")
        .build()?)
}
/// Real REST bootstrap, no cached/mock fallback and no credentials.
pub async fn fetch_exchange_info(
    client: &reqwest::Client, environment: BinanceEnvironment,
    symbols: &[String],
) -> Result<InstrumentSpecTable, MarketDataError> {
    let r=client.get(format!("{}/fapi/v1/exchangeInfo",environment.rest())).send().await?
        .error_for_status()?;
    if r.content_length().is_some_and(|len|len>4_194_304){
        return Err(MarketDataError::InvalidResponse("oversize exchangeInfo"));
    }
    let body=r.bytes().await?;
    if body.len()>4_194_304 {
        return Err(MarketDataError::InvalidResponse("oversize exchangeInfo"));
    }
    let value:Value=serde_json::from_slice(&body)?;
    InstrumentSpecTable::from_exchange_info(&value,symbols,environment)
}
pub(crate) fn ms_to_ns(ms:u64) -> Result<u64,MarketDataError>{
    ms.checked_mul(1_000_000).ok_or(MarketDataError::InvalidResponse("timestamp overflow"))
}
pub(crate) fn parse_price(s: &str)->Result<i64,MarketDataError>{
    FixedPrice::from_str(s).map(|v|v.0).map_err(|err:NumericError|MarketDataError::Numeric(err))
}
pub(crate) fn checked_notional(p:i64,q:i64)->Result<i64,MarketDataError>{
    i64::try_from(i128::from(p)*i128::from(q)/i128::from(SCALE))
        .map_err(|_|MarketDataError::InvalidResponse("notional overflow"))
}
