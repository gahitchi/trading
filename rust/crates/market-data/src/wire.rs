use trading_core_types::{
    FixedPrice, FixedQty, MarketEvent, MarketEventType, VenueId,
    FLAG_BID_OR_BUYER_MAKER,
};
use trading_orderbook::{LevelDelta, PriceLevel, Side};
use serde_json::Value;
use crate::{InstrumentSpecTable, MarketDataError};
use crate::instruments::{ms_to_ns,parse_price};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StreamGroup { PublicDepth, MarketRegular }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SnapshotRequest {
    pub symbol_id: u16,
    pub generation: u64,
    pub request_id: u64,
}
#[derive(Debug, Clone)]
pub enum LiveFrame {
    /// Invalidate the book immediately when a public-depth socket connects,
    /// disconnects, or reconnects. No prior generation may remain tradable.
    Reset { generation: u64 },
    Snapshot {
        request: SnapshotRequest,
        last_update_id: u64,
        bids: Vec<PriceLevel>,
        asks: Vec<PriceLevel>,
    },
    Depth {
        symbol_id: u16, generation: u64,
        first: u64, final_id: u64, previous_final: u64,
        event_ns: u64, received_ns: u64,
        /// Complete, atomic Binance absolute-quantity batch, never a
        /// partial batch of independent 64-byte events.
        deltas: Vec<LevelDelta>,
    },
    AggTrade(MarketEvent),
    ForcedLiquidation(MarketEvent),
    Mark {
        symbol_id: u16, event_ns: u64, received_ns: u64,
        mark_price: FixedPrice, index_price: FixedPrice,
        /// Exact 1e8 scaled *fraction*; NOT bps.
        funding_rate_raw: i64,
    },
}
fn field_str<'a>(v:&'a Value,key:&str)->Result<&'a str,MarketDataError>{
    v.get(key).and_then(Value::as_str)
        .ok_or(MarketDataError::InvalidResponse("missing string field"))
}
fn field_u64(v:&Value,key:&str)->Result<u64,MarketDataError>{
    v.get(key).and_then(Value::as_u64)
        .ok_or(MarketDataError::InvalidResponse("missing unsigned field"))
}
fn levels(v: &Value, side:Side)->Result<Vec<LevelDelta>,MarketDataError>{
    let arr=v.as_array().ok_or(MarketDataError::InvalidResponse("invalid depth array"))?;
    if arr.len()>2_000 {return Err(MarketDataError::InvalidResponse("oversized depth batch"));}
    let mut out=Vec::with_capacity(arr.len());
    for entry in arr {
        let pair=entry.as_array().ok_or(MarketDataError::InvalidResponse("bad level tuple"))?;
        if pair.len()!=2{return Err(MarketDataError::InvalidResponse("bad level length"));}
        let price=parse_price(pair[0].as_str()
            .ok_or(MarketDataError::InvalidResponse("bad price"))?)?;
        let quantity=parse_price(pair[1].as_str()
            .ok_or(MarketDataError::InvalidResponse("bad quantity"))?)?;
        let delta=LevelDelta::new(side,FixedPrice(price),FixedQty(quantity))
            .map_err(MarketDataError::Book)?;
        out.push(delta);
    }
    Ok(out)
}
fn snapshot_levels(v:&Value)->Result<Vec<PriceLevel>,MarketDataError>{
    let arr=v.as_array().ok_or(MarketDataError::InvalidResponse("bad snapshot array"))?;
    if arr.len()>1000{return Err(MarketDataError::InvalidResponse("snapshot too deep"));}
    let mut out=Vec::with_capacity(arr.len());
    for e in arr {
        let pair=e.as_array().ok_or(MarketDataError::InvalidResponse("bad tuple"))?;
        if pair.len()!=2{return Err(MarketDataError::InvalidResponse("bad tuple length"));}
        let p=FixedPrice(parse_price(pair[0].as_str()
            .ok_or(MarketDataError::InvalidResponse("bad snapshot price"))?)?);
        let q=FixedQty(parse_price(pair[1].as_str()
            .ok_or(MarketDataError::InvalidResponse("bad snapshot quantity"))?)?);
        out.push(PriceLevel::new(p,q).map_err(MarketDataError::Book)?);
    }
    Ok(out)
}
pub fn parse_depth_snapshot(body:&[u8],request:SnapshotRequest)
    ->Result<LiveFrame,MarketDataError>
{
    if body.len()>1_048_576{return Err(MarketDataError::InvalidResponse("snapshot body too large"));}
    let v:Value=serde_json::from_slice(body)?;
    let last_update_id=field_u64(&v,"lastUpdateId")?;
    if last_update_id==0{return Err(MarketDataError::InvalidResponse("zero snapshot sequence"));}
    let bids=snapshot_levels(v.get("bids")
        .ok_or(MarketDataError::InvalidResponse("snapshot lacks bids"))?)?;
    let asks=snapshot_levels(v.get("asks")
        .ok_or(MarketDataError::InvalidResponse("snapshot lacks asks"))?)?;
    if bids.is_empty()||asks.is_empty(){return Err(MarketDataError::InvalidResponse("empty book"));}
    Ok(LiveFrame::Snapshot {request,last_update_id,bids,asks})
}
/// One live combined-stream message. No dummy or synthetic price generators.
/// The strict route/group check prevents accidental subscriptions on Binance's
/// newly separated public and market gateways (effective 2026-04-23).
pub fn parse_combined(
    input:&str,group:StreamGroup,generation:u64,received_ns:u64,
    table:&InstrumentSpecTable,
)->Result<LiveFrame,MarketDataError>{
    if input.len()>1_048_576{return Err(MarketDataError::InvalidResponse("websocket frame too large"));}
    let root:Value=serde_json::from_str(input)?;
    let stream=field_str(&root,"stream")?;
    let data=root.get("data").ok_or(MarketDataError::InvalidResponse("missing data"))?;
    let event=field_str(data,"e")?;
    // Forced-liquidation events locate the symbol in the nested order
    // object (o.s), unlike depth/trade/mark updates.
    let symbol=if event=="forceOrder" {
        field_str(data.get("o")
            .ok_or(MarketDataError::InvalidResponse("no forceOrder data"))?,"s")?
    } else { field_str(data,"s")? };
    let instrument=table.find(symbol).ok_or(MarketDataError::InvalidResponse("unknown feed symbol"))?;
    let id=instrument.symbol_id;
    let lower=symbol.to_ascii_lowercase();
    let event_ns=ms_to_ns(field_u64(data,"E")?)?;
    match (group,event) {
        (StreamGroup::PublicDepth,"depthUpdate") if stream==format!("{lower}@depth@100ms") => {
            let first=field_u64(data,"U")?;
            let final_id=field_u64(data,"u")?;
            let previous_final=field_u64(data,"pu")?;
            if first==0||final_id<first {return Err(MarketDataError::InvalidResponse("invalid depth sequence"));}
            let mut deltas=levels(data.get("b").ok_or(MarketDataError::InvalidResponse("missing bids"))?,Side::Bid)?;
            deltas.extend(levels(data.get("a").ok_or(MarketDataError::InvalidResponse("missing asks"))?,Side::Ask)?);
            Ok(LiveFrame::Depth {symbol_id:id,generation,first,final_id,previous_final,
                event_ns,received_ns,deltas})
        }
        (StreamGroup::MarketRegular,"aggTrade") if stream==format!("{lower}@aggTrade") => {
            let tx_ns=ms_to_ns(field_u64(data,"T")?)?;
            let price=FixedPrice(parse_price(field_str(data,"p")?)?);
            let qty=FixedQty(parse_price(field_str(data,"q")?)?);
            if price.0<=0||qty.0<=0{return Err(MarketDataError::InvalidResponse("invalid aggregate trade"));}
            let maker=data.get("m").and_then(Value::as_bool)
                .ok_or(MarketDataError::InvalidResponse("no maker direction"))?;
            let event=MarketEvent::new(tx_ns,received_ns,price,qty,
                field_u64(data,"a")?,field_u64(data,"a")?,id,
                VenueId::BinanceUsdM,MarketEventType::AggTrade,
                if maker {FLAG_BID_OR_BUYER_MAKER}else{0});
            Ok(LiveFrame::AggTrade(event))
        }
        (StreamGroup::MarketRegular,"forceOrder") if stream==format!("{lower}@forceOrder") => {
            let order=data.get("o").ok_or(MarketDataError::InvalidResponse("no forceOrder data"))?;
            if field_str(order,"s")?!=symbol {return Err(MarketDataError::InvalidResponse("liquidation symbol mismatch"));}
            let price=FixedPrice(parse_price(field_str(order,"p")?)?);
            let qty=FixedQty(parse_price(field_str(order,"q")?)?);
            if price.0<=0||qty.0<=0{return Err(MarketDataError::InvalidResponse("invalid liquidation"));}
            let side=field_str(order,"S")?;
            let flags=match side {
                "BUY"=>0,
                "SELL"=>FLAG_BID_OR_BUYER_MAKER,
                _=>return Err(MarketDataError::InvalidResponse("bad liquidation side")),
            };
            Ok(LiveFrame::ForcedLiquidation(MarketEvent::new(
                event_ns,received_ns,price,qty,0,0,id,VenueId::BinanceUsdM,
                MarketEventType::ForceOrderLiq,flags,
            )))
        }
        (StreamGroup::MarketRegular,"markPriceUpdate") if stream==format!("{lower}@markPrice@1s") => {
            Ok(LiveFrame::Mark {
                symbol_id:id,event_ns,received_ns,
                mark_price:FixedPrice(parse_price(field_str(data,"p")?)?),
                index_price:FixedPrice(parse_price(field_str(data,"i")?)?),
                funding_rate_raw:parse_price(field_str(data,"r")?)?,
            })
        }
        _=>Err(MarketDataError::InvalidResponse("wrong stream routing or unknown event")),
    }
}
