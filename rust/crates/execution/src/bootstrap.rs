//! Read-only, fail-closed account bootstrap from signed Binance USD-M REST.
//! A response is a point-in-time observation, not execution authorization.
use std::{collections::BTreeMap, str::FromStr, time::{Duration, SystemTime, UNIX_EPOCH}};
use serde_json::Value;
use trading_core_types::FixedPrice;
use trading_market_data::{BinanceEnvironment, InstrumentSpecTable};
use crate::{Authentication, ExecutionError};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccountSnapshot {
    pub wallet_usdt: i64,
    pub cross_wallet_usdt: i64,
    pub available_usdt: i64,
    pub unrealized_usdt: i64,
    pub total_margin_usdt: i64,
    pub maintenance_margin_usdt: i64,
    pub open_order_margin_usdt: i64,
    pub positions: Vec<RestPosition>,
    pub open_orders: Vec<RestOpenOrder>,
    pub observed_at_ms: u64,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RestPosition {
    pub symbol: String,
    pub quantity_raw: i64,
    pub entry_price_raw: i64,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RestOpenOrder {
    pub symbol: String,
    pub client_id: String,
    pub order_id: u64,
    pub remaining_qty_raw: i64,
}
fn as_str<'a>(value: &'a Value, field: &str) -> Result<&'a str, ExecutionError> {
    value.get(field).and_then(Value::as_str)
        .ok_or(ExecutionError::InvalidMessage("required account string missing"))
}
fn decimal(value: &Value, field: &str) -> Result<i64, ExecutionError> {
    Ok(FixedPrice::from_str(as_str(value, field)?)?.0)
}
fn nonnegative(value: &Value, field: &str) -> Result<i64, ExecutionError> {
    let v=decimal(value,field)?;
    if v<0 {return Err(ExecutionError::InvalidMessage("negative account collateral"));}
    Ok(v)
}
pub fn parse_bootstrap(
    account: &Value, open_orders: &Value, position_mode: &Value,
    instruments: &InstrumentSpecTable, observed_at_ms: u64,
) -> Result<AccountSnapshot, ExecutionError> {
    if observed_at_ms==0 || position_mode.get("dualSidePosition").and_then(Value::as_bool)!=Some(false) {
        return Err(ExecutionError::InvalidMessage("one-way position mode not verified"));
    }
    if account.get("multiAssetsMargin").and_then(Value::as_bool)!=Some(false) {
        return Err(ExecutionError::InvalidMessage("multi-assets margin unsupported"));
    }
    let wallet=nonnegative(account,"totalWalletBalance")?;
    let cross=nonnegative(account,"totalCrossWalletBalance")?;
    let available=nonnegative(account,"availableBalance")?;
    let margin=nonnegative(account,"totalMarginBalance")?;
    let maintenance=nonnegative(account,"totalMaintMargin")?;
    let order_margin=nonnegative(account,"totalOpenOrderInitialMargin")?;
    let unrealized=decimal(account,"totalUnrealizedProfit")?;
    // Account totals are quote/USDT-only in single-asset mode. Never infer
    // available collateral from wallet alone; exchange reports its margin.
    if i128::from(wallet)+i128::from(unrealized)!=i128::from(margin)
        || maintenance>margin || available>margin || cross>margin {
        return Err(ExecutionError::InvalidMessage("inconsistent exchange account totals"));
    }
    let assets=account.get("assets").and_then(Value::as_array)
        .ok_or(ExecutionError::InvalidMessage("missing account assets"))?;
    if assets.len()>200 {return Err(ExecutionError::InvalidMessage("oversized asset array"));}
    let mut usdt_count=0;
    for asset in assets {
        let code=as_str(asset,"asset")?;
        let balance=decimal(asset,"walletBalance")?;
        if code=="USDT" {
            usdt_count+=1;
            if balance<0 {return Err(ExecutionError::InvalidMessage("negative USDT balance"));}
        } else if balance!=0 {
            // This engine cannot value other collateral at a known haircut.
            return Err(ExecutionError::InvalidMessage("non-USDT assets require collateral adapter"));
        }
    }
    if usdt_count!=1 {return Err(ExecutionError::InvalidMessage("missing/duplicate USDT asset"));}
    let rows=account.get("positions").and_then(Value::as_array)
        .ok_or(ExecutionError::InvalidMessage("missing account positions"))?;
    if rows.len()>300 {return Err(ExecutionError::InvalidMessage("oversized position array"));}
    let mut positions=Vec::new();
    let mut seen=std::collections::BTreeSet::new();
    for p in rows {
        let symbol=as_str(p,"symbol")?;
        let qty=decimal(p,"positionAmt")?;
        if qty==0 {continue;}
        if as_str(p,"positionSide")?!="BOTH"
            || instruments.find(symbol).is_none() || !seen.insert(symbol.to_owned()) {
            return Err(ExecutionError::InvalidMessage("unsupported or repeated position"));
        }
        let entry_price=nonnegative(p,"entryPrice")?;
        if entry_price==0 {return Err(ExecutionError::InvalidMessage("open position has zero basis"));}
        positions.push(RestPosition {
            symbol:symbol.into(), quantity_raw:qty, entry_price_raw:entry_price,
        });
    }
    let orders=open_orders.as_array()
        .ok_or(ExecutionError::InvalidMessage("invalid openOrders response"))?;
    if orders.len()>500 {return Err(ExecutionError::InvalidMessage("oversized open orders"));}
    let mut observed_orders=Vec::with_capacity(orders.len());
    let mut ids=std::collections::BTreeSet::new();
    for o in orders {
        let symbol=as_str(o,"symbol")?;
        let client_id=as_str(o,"clientOrderId")?;
        let order_id=o.get("orderId").and_then(Value::as_u64)
            .ok_or(ExecutionError::InvalidMessage("open order missing ID"))?;
        if instruments.find(symbol).is_none() || order_id==0
            || !ids.insert(order_id)
            || client_id.is_empty() {
            return Err(ExecutionError::InvalidMessage("unknown or repeated open order"));
        }
        let orig=nonnegative(o,"origQty")?;
        let executed=nonnegative(o,"executedQty")?;
        let remaining=orig.checked_sub(executed)
            .ok_or(ExecutionError::InvalidMessage("overfilled open order"))?;
        if remaining==0 {return Err(ExecutionError::InvalidMessage("open order without remainder"));}
        observed_orders.push(RestOpenOrder{
            symbol:symbol.into(),client_id:client_id.into(),order_id,remaining_qty_raw:remaining,
        });
    }
    Ok(AccountSnapshot {
        wallet_usdt:wallet,cross_wallet_usdt:cross,available_usdt:available,
        unrealized_usdt:unrealized,total_margin_usdt:margin,
        maintenance_margin_usdt:maintenance,open_order_margin_usdt:order_margin,
        positions,open_orders:observed_orders,observed_at_ms,
    })
}

pub struct SignedAccountRest {
    client: reqwest::Client,
    auth: Authentication,
    environment: BinanceEnvironment,
}
impl SignedAccountRest {
    pub fn new(auth:Authentication,environment:BinanceEnvironment)
        -> Result<Self,ExecutionError> {
        let client=reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(10))
            .build()?;
        Ok(Self {client,auth,environment})
    }
    fn signed_query(&self, now_ms:u64) -> Result<String,ExecutionError> {
        let fields=BTreeMap::from([
            ("recvWindow".to_owned(),"5000".to_owned()),
            ("timestamp".to_owned(),now_ms.to_string()),
        ]);
        let query=fields.iter().map(|(k,v)|format!("{k}={v}"))
            .collect::<Vec<_>>().join("&");
        let signature=self.auth.signature(&fields)?;
        // Ed25519 produces base64 including '+', '/' and '='. These MUST be
        // percent-encoded in a REST query. HMAC hex needs no encoding.
        let escaped=signature.bytes().map(|b|match b {
            b'+'|b'/'|b'='=>format!("%{b:02X}"),
            _=>char::from(b).to_string(),
        }).collect::<String>();
        Ok(format!("{query}&signature={escaped}"))
    }
    async fn get(&self,path:&str)->Result<Value,ExecutionError>{
        let now_ms=SystemTime::now().duration_since(UNIX_EPOCH)
            .map_err(|_|ExecutionError::Configuration("wall clock not synchronized"))?
            .as_millis();
        let now=u64::try_from(now_ms)
            .map_err(|_|ExecutionError::Configuration("timestamp overflow"))?;
        let url=format!("{}{}?{}",self.environment.rest(),path,self.signed_query(now)?);
        let response=self.client.get(url).header("X-MBX-APIKEY",self.auth.api_key())
            .send().await?.error_for_status()?;
        if response.content_length().is_some_and(|len|len>4_194_304) {
            return Err(ExecutionError::InvalidMessage("oversized account response"));
        }
        let bytes=response.bytes().await?;
        if bytes.len()>4_194_304 {
            return Err(ExecutionError::InvalidMessage("oversized account response"));
        }
        Ok(serde_json::from_slice(&bytes)?)
    }
    /// READ ONLY. A signed snapshot is NOT sufficient to resume trading;
    /// private-stream continuity, order history and clock bounds still need
    /// independent proof.
    pub async fn fetch_snapshot(&self,instruments:&InstrumentSpecTable)
        -> Result<AccountSnapshot,ExecutionError>
    {
        if instruments.environment()!=self.environment {
            return Err(ExecutionError::Configuration("account/market environment mismatch"));
        }
        let account=self.get("/fapi/v2/account").await?;
        let orders=self.get("/fapi/v1/openOrders").await?;
        let mode=self.get("/fapi/v1/positionSide/dual").await?;
        let observed_at_ms=u64::try_from(SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_|ExecutionError::Configuration("clock invalid"))?
            .as_millis()).map_err(|_|ExecutionError::Configuration("clock overflow"))?;
        parse_bootstrap(&account,&orders,&mode,instruments,observed_at_ms)
    }
}
