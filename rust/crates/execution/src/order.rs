use crate::ExecutionError;
use serde_json::{Map, Value, json};
use trading_core_types::{FixedPrice, FixedQty, InstrumentSpec, SCALE, TickRounding};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderSide {
    Buy,
    Sell,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderKind {
    IocLimit,
    PostOnlyLimit,
    MarketReduceOnly,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PrepareOrder {
    pub client_id: u64,
    pub symbol_id: u16,
    pub side: OrderSide,
    pub kind: OrderKind,
    pub quantity: FixedQty,
    pub limit: Option<FixedPrice>,
}
#[derive(Debug, Clone)]
pub struct ExchangeOrder {
    pub client_id: u64,
    pub symbol: String,
    pub params: Map<String, Value>,
}
fn decimal(n: i64) -> String {
    let whole = n / SCALE;
    let fraction = (n % SCALE).unsigned_abs();
    if fraction == 0 {
        return whole.to_string();
    }
    let frac = format!("{fraction:08}");
    format!("{whole}.{}", frac.trim_end_matches('0'))
}
impl PrepareOrder {
    /// Explicit exchange filter checks, not a promise of an executable fill.
    /// Only ONE-WAY position mode supported until Part 3 proves account mode.
    /// No broker-sent order is ever created from an unqualified instrument.
    pub fn prepare(
        self,
        symbol: &str,
        spec: InstrumentSpec,
    ) -> Result<ExchangeOrder, ExecutionError> {
        if self.client_id == 0
            || self.quantity.0 <= 0
            || symbol.is_empty()
            || symbol.len() > 32
            || !symbol
                .bytes()
                .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit())
        {
            return Err(ExecutionError::InvalidMessage(
                "invalid order identity or quantity",
            ));
        }
        let market = self.kind == OrderKind::MarketReduceOnly;
        let aligned = spec
            .round_qty(self.quantity, market)
            .map_err(|_| ExecutionError::InvalidMessage("invalid exchange LOT_SIZE quantity"))?;
        // Never silently reduce risk-approved size: the gate must reassess
        // the actual aligned size before publication and reservation.
        if aligned != self.quantity {
            return Err(ExecutionError::InvalidMessage(
                "quantity is not step-aligned",
            ));
        }
        if matches!(self.kind, OrderKind::MarketReduceOnly) && self.side != OrderSide::Buy {
            return Err(ExecutionError::InvalidMessage(
                "only short reduce-only covers supported",
            ));
        }
        let mut p = Map::new();
        p.insert("symbol".into(), json!(symbol));
        p.insert(
            "side".into(),
            json!(match self.side {
                OrderSide::Buy => "BUY",
                OrderSide::Sell => "SELL",
            }),
        );
        p.insert("positionSide".into(), json!("BOTH"));
        p.insert(
            "newClientOrderId".into(),
            json!(format!("tl-{}", self.client_id)),
        );
        p.insert("newOrderRespType".into(), json!("ACK"));
        match self.kind {
            OrderKind::MarketReduceOnly => {
                if self.limit.is_some() {
                    return Err(ExecutionError::InvalidMessage(
                        "MARKET order may not contain price",
                    ));
                }
                p.insert("type".into(), json!("MARKET"));
                p.insert("reduceOnly".into(), json!("true"));
            }
            OrderKind::IocLimit | OrderKind::PostOnlyLimit => {
                let price = self
                    .limit
                    .ok_or(ExecutionError::InvalidMessage("limit price required"))?;
                if price.0 <= 0 {
                    return Err(ExecutionError::InvalidMessage("nonpositive limit"));
                }
                spec.check_price(price)
                    .map_err(|_| ExecutionError::InvalidMessage("price not aligned to tickSize"))?;
                // Caller can find correct side-dependent candidate with
                // round_price(...,TickRounding::Down|Up), but any adjustment
                // requires renewed external risk approval.
                p.insert("type".into(), json!("LIMIT"));
                p.insert("price".into(), json!(decimal(price.0)));
                p.insert(
                    "timeInForce".into(),
                    json!(if self.kind == OrderKind::IocLimit {
                        "IOC"
                    } else {
                        "GTX"
                    }),
                );
            }
        }
        p.insert("quantity".into(), json!(decimal(aligned.0)));
        if !market {
            let price = self
                .limit
                .ok_or(ExecutionError::InvalidMessage("missing limit"))?;
            spec.check_notional(price, aligned)
                .map_err(|_| ExecutionError::InvalidMessage("below min notional or oversized"))?;
        }
        Ok(ExchangeOrder {
            client_id: self.client_id,
            symbol: symbol.into(),
            params: p,
        })
    }
    pub fn aligned_price_hint(self, spec: InstrumentSpec) -> Result<FixedPrice, ExecutionError> {
        let price = self
            .limit
            .ok_or(ExecutionError::InvalidMessage("limit missing"))?;
        let direction = match self.side {
            OrderSide::Sell => {
                if self.kind == OrderKind::PostOnlyLimit {
                    TickRounding::Up
                } else {
                    TickRounding::Down
                }
            }
            OrderSide::Buy => TickRounding::Up,
        };
        spec.round_price(price, direction)
            .map_err(|_| ExecutionError::InvalidMessage("invalid price filter"))
    }
}
pub fn cancel_params(symbol: &str, client_id: u64) -> Result<Map<String, Value>, ExecutionError> {
    if client_id == 0
        || symbol.is_empty()
        || symbol.len() > 32
        || !symbol
            .bytes()
            .all(|x| x.is_ascii_uppercase() || x.is_ascii_digit())
    {
        return Err(ExecutionError::InvalidMessage("invalid cancel identity"));
    }
    let mut params = Map::new();
    params.insert("symbol".into(), json!(symbol));
    params.insert("origClientOrderId".into(), json!(format!("tl-{client_id}")));
    Ok(params)
}
#[cfg(test)]
mod tests {
    use super::*;
    fn spec() -> InstrumentSpec {
        InstrumentSpec {
            tick_size: 1_000_000,
            step_size: 100_000,
            min_notional: 500_000_000,
            min_price: 1_000_000,
            max_price: 1_000_000_000_000,
            min_qty: 100_000,
            max_qty: 10_000_000_000,
            market_step_size: 100_000,
            market_min_qty: 100_000,
            market_max_qty: 10_000_000_000,
        }
    }
    #[test]
    fn limit_order_is_exchange_exact_and_has_correct_ioc() {
        let o = PrepareOrder {
            client_id: 42,
            symbol_id: 0,
            side: OrderSide::Sell,
            kind: OrderKind::IocLimit,
            quantity: FixedQty(SCALE),
            limit: Some(FixedPrice(100 * SCALE)),
        }
        .prepare("BTCUSDT", spec())
        .unwrap();
        assert_eq!(o.params["timeInForce"], "IOC");
        assert_eq!(o.params["quantity"], "1");
        assert_eq!(o.params["price"], "100");
        assert_eq!(o.params["newClientOrderId"], "tl-42");
    }
    #[test]
    fn never_round_risk_approved_order_silently() {
        let o = PrepareOrder {
            client_id: 1,
            symbol_id: 0,
            side: OrderSide::Sell,
            kind: OrderKind::PostOnlyLimit,
            quantity: FixedQty(SCALE + 1),
            limit: Some(FixedPrice(100 * SCALE)),
        };
        assert!(o.prepare("BTCUSDT", spec()).is_err());
        let o = PrepareOrder {
            quantity: FixedQty(SCALE),
            limit: Some(FixedPrice(100 * SCALE + 1)),
            ..o
        };
        assert!(o.prepare("BTCUSDT", spec()).is_err());
    }
    #[test]
    fn reduce_only_market_uses_no_price_no_time_in_force() {
        let p = PrepareOrder {
            client_id: 2,
            symbol_id: 0,
            side: OrderSide::Buy,
            kind: OrderKind::MarketReduceOnly,
            quantity: FixedQty(SCALE),
            limit: None,
        }
        .prepare("BTCUSDT", spec())
        .unwrap();
        assert_eq!(p.params["type"], "MARKET");
        assert_eq!(p.params["reduceOnly"], "true");
        assert!(p.params.get("price").is_none());
        assert!(p.params.get("timeInForce").is_none());
    }
}
