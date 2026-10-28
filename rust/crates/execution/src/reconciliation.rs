//! Fail-closed exchange observation state, never a source of trade authorization.
use crate::{AccountSnapshot, PrivateEvent, ExecutionError};
use std::collections::{BTreeMap,BTreeSet};
use trading_core_types::ExecutionStatus;
use trading_market_data::InstrumentSpecTable;

#[derive(Debug,Clone,Copy,PartialEq,Eq)]
pub enum ReconciliationStatus { AwaitingStream, ObservingUnqualified, Quarantined }
#[derive(Debug,Clone,Copy,PartialEq,Eq)]
pub enum ReconcileOutcome { Applied, Duplicate, Quarantined }
#[derive(Debug,Clone,Copy,PartialEq,Eq)]
pub struct ObservedBalance { pub wallet_raw:i64, pub cross_raw:i64 }
#[derive(Debug,Clone,Copy,PartialEq,Eq)]
pub struct ObservedPosition { pub quantity_raw:i64, pub entry_raw:i64 }
#[derive(Debug,Clone,Copy,PartialEq,Eq)]
struct TradeValue { qty:i64, cum:i64, price:i64, fee:i64, pnl:i64 }
pub struct AccountReconciler {
    state:ReconciliationStatus,
    epoch_ms:u64,
    account_tx_ms:Option<u64>,
    balances:BTreeMap<String,ObservedBalance>,
    positions:BTreeMap<String,ObservedPosition>,
    trade_ids:BTreeMap<(u64,u64),TradeValue>,
    cumulative:BTreeMap<u64,i64>,
    pnl_raw:i64,
    outstanding_open_orders:usize,
    table:InstrumentSpecTable,
}
impl AccountReconciler {
    pub fn new(snapshot:AccountSnapshot,table:InstrumentSpecTable)->Result<Self,ExecutionError>{
        if snapshot.observed_at_ms==0 {return Err(ExecutionError::InvalidMessage("missing REST epoch"));}
        let mut positions=BTreeMap::new();
        for p in snapshot.positions {
            if p.quantity_raw==0 || p.entry_price_raw<=0 ||
                table.find(&p.symbol).is_none() ||
                positions.insert(p.symbol,ObservedPosition {
                    quantity_raw:p.quantity_raw,entry_raw:p.entry_price_raw
                }).is_some() {
                return Err(ExecutionError::InvalidMessage("invalid initial position"));
            }
        }
        let mut balances=BTreeMap::new();
        balances.insert("USDT".into(),ObservedBalance {
            wallet_raw:snapshot.wallet_usdt,cross_raw:snapshot.cross_wallet_usdt
        });
        Ok(Self {
            state:ReconciliationStatus::AwaitingStream,epoch_ms:snapshot.observed_at_ms,
            account_tx_ms:None,balances,positions,trade_ids:BTreeMap::new(),
            cumulative:BTreeMap::new(),pnl_raw:0,
            outstanding_open_orders:snapshot.open_orders.len(),table
        })
    }
    pub fn status(&self)->ReconciliationStatus{self.state}
    pub fn trading_permitted(&self)->bool{false}
    pub fn open_orders_at_bootstrap(&self)->usize{self.outstanding_open_orders}
    pub fn observed_balance(&self,asset:&str)->Option<ObservedBalance>{self.balances.get(asset).copied()}
    pub fn observed_position(&self,symbol:&str)->Option<ObservedPosition>{self.positions.get(symbol).copied()}
    pub fn observed_trade_pnl_raw(&self)->i64{self.pnl_raw}
    pub fn observed_unique_trades(&self)->usize{self.trade_ids.len()}
    fn quarantine(&mut self)->ReconcileOutcome{
        self.state=ReconciliationStatus::Quarantined;
        ReconcileOutcome::Quarantined
    }
    pub fn apply(&mut self,event:&PrivateEvent)->ReconcileOutcome{
        if self.state==ReconciliationStatus::Quarantined {return ReconcileOutcome::Quarantined;}
        match event {
            PrivateEvent::Connected => {
                if self.state!=ReconciliationStatus::AwaitingStream {return self.quarantine();}
                self.state=ReconciliationStatus::ObservingUnqualified;
                ReconcileOutcome::Applied
            }
            PrivateEvent::Disconnected | PrivateEvent::ReconciliationRequired(_) => self.quarantine(),
            PrivateEvent::Account(update) => {
                if self.state!=ReconciliationStatus::ObservingUnqualified ||
                    update.transaction_ms<self.epoch_ms ||
                    self.account_tx_ms.is_some_and(|x|update.transaction_ms<=x) {
                    return self.quarantine();
                }
                let mut balances=self.balances.clone();
                let mut positions=self.positions.clone();
                let mut assets_seen=BTreeSet::new();
                for b in &update.balances {
                    if !assets_seen.insert(b.asset.as_str()) ||
                        b.wallet_balance_raw<0 || b.cross_wallet_balance_raw<0 ||
                        (b.asset!="USDT" && b.wallet_balance_raw!=0) {
                        return self.quarantine();
                    }
                    balances.insert(b.asset.clone(),ObservedBalance {
                        wallet_raw:b.wallet_balance_raw,cross_raw:b.cross_wallet_balance_raw
                    });
                }
                let mut positions_seen=BTreeSet::new();
                for p in &update.positions {
                    if !positions_seen.insert(p.symbol.as_str()) ||
                        p.position_side!="BOTH" || self.table.find(&p.symbol).is_none() ||
                        p.entry_price_raw<0 ||
                        (p.position_amount_raw!=0 && p.entry_price_raw==0) {
                        return self.quarantine();
                    }
                    if p.position_amount_raw==0 {positions.remove(&p.symbol);}
                    else {positions.insert(p.symbol.clone(),ObservedPosition {
                        quantity_raw:p.position_amount_raw,entry_raw:p.entry_price_raw
                    });}
                }
                self.balances=balances;
                self.positions=positions;
                self.account_tx_ms=Some(update.transaction_ms);
                ReconcileOutcome::Applied
            }
            PrivateEvent::Order(fill) => {
                let e=fill.event;
                if self.state!=ReconciliationStatus::ObservingUnqualified ||
                    e.exchange_ts_ns/1_000_000<self.epoch_ms ||
                    self.table.by_id(e.symbol_id).is_none() ||
                    fill.exchange_order_id==0 || e.last_filled_qty<0 ||
                    e.cumulative_qty<e.last_filled_qty || e.commission<0 {
                    return self.quarantine();
                }
                let Ok(status)=ExecutionStatus::try_from(e.status) else {return self.quarantine();};
                if e.last_filled_qty==0 {
                    if matches!(status,ExecutionStatus::Filled|ExecutionStatus::PartiallyFilled)
                        && e.cumulative_qty==0 {return self.quarantine();}
                    if self.cumulative.get(&fill.exchange_order_id).copied().unwrap_or(0)
                        !=e.cumulative_qty {return self.quarantine();}
                    return ReconcileOutcome::Applied;
                }
                if fill.trade_id==0 || e.fill_price<=0 ||
                    fill.commission_asset!="USDT" ||
                    fill.realized_pnl_asset!="USDT" || e.flags&1==0 {
                    return self.quarantine();
                }
                let key=(fill.exchange_order_id,fill.trade_id);
                let value=TradeValue {qty:e.last_filled_qty,cum:e.cumulative_qty,
                    price:e.fill_price,fee:e.commission,pnl:e.realized_pnl};
                if let Some(existing)=self.trade_ids.get(&key) {
                    return if *existing==value {ReconcileOutcome::Duplicate}
                    else {self.quarantine()};
                }
                let prior=self.cumulative.get(&fill.exchange_order_id).copied().unwrap_or(0);
                if prior.checked_add(e.last_filled_qty)!=Some(e.cumulative_qty) {
                    return self.quarantine();
                }
                let Some(net)=e.realized_pnl.checked_sub(e.commission) else {return self.quarantine();};
                let Some(total)=self.pnl_raw.checked_add(net) else {return self.quarantine();};
                self.cumulative.insert(fill.exchange_order_id,e.cumulative_qty);
                self.trade_ids.insert(key,value);
                self.pnl_raw=total;
                ReconcileOutcome::Applied
            }
        }
    }
}
