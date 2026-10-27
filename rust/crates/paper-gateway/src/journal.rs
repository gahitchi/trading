//! Crash-detecting append-only paper event journal. FNV-1a is an accidental
//! corruption detector, NOT an authenticated anti-tampering MAC or signature.
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::Path;
use trading_core_types::{FixedUsd, SCALE};
use trading_wallet_manager::{WalletBuckets, WalletSnapshot};

const HEADER: &str = "TRADING-LAB-OFFLINE-PAPER-1\n";
const MAX_JOURNAL_BYTES: u64 = 16 * 1024 * 1024;
const MAX_EVENTS: u64 = 100_000;
const INITIAL_CASH: i64 = 10_000 * SCALE;
const FEE_BPS_PER_SIDE: i128 = 2;

#[derive(Debug)]
pub enum JournalError {
    Io(std::io::Error),
    Integrity,
    InvalidTransition,
    Arithmetic,
    InspectOnly,
    Poisoned,
}
impl From<std::io::Error> for JournalError {
    fn from(e: std::io::Error) -> Self { Self::Io(e) }
}
impl std::fmt::Display for JournalError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for JournalError {}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum RecordKind {
    EntryReserve = 1,
    EntryFill = 2,
    EntryCancel = 3,
    CoverReserve = 4,
    CoverFill = 5,
    CoverCancel = 6,
    EmergencyHalt = 7,
}
impl TryFrom<u8> for RecordKind {
    type Error = JournalError;
    fn try_from(v: u8) -> Result<Self, Self::Error> {
        Ok(match v {
            1 => Self::EntryReserve, 2 => Self::EntryFill,
            3 => Self::EntryCancel, 4 => Self::CoverReserve,
            5 => Self::CoverFill, 6 => Self::CoverCancel,
            7 => Self::EmergencyHalt, _ => return Err(JournalError::Integrity),
        })
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Record {
    pub sequence: u64,
    pub kind: RecordKind,
    pub order_id: u64,
    /// Requested quantity for Reserve, fill delta for Fill, zero otherwise.
    pub quantity_raw: i64,
    /// Limit price for Reserve, actual simulated execution price for Fill.
    pub price_raw: i64,
    pub net_pnl_raw: i64,
    pub position_after_raw: i64,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Pending {
    id: u64,
    entry: bool,
    requested: i64,
    filled: i64,
    limit: i64,
}
#[derive(Debug, Clone)]
pub struct ReplaySummary {
    pub records: u64,
    pub last_order_id: u64,
    pub confirmed_short_qty_raw: i64,
    pub average_entry_price_raw: i64,
    pub realized_net_pnl_raw: i64,
    pub halted: bool,
    pending: Option<Pending>,
    wallet: WalletBuckets,
}
impl ReplaySummary {
    fn new() -> Self {
        Self {
            records: 0, last_order_id: 0, confirmed_short_qty_raw: 0,
            average_entry_price_raw: 0, realized_net_pnl_raw: 0,
            halted: false, pending: None,
            wallet: WalletBuckets::new(FixedUsd(INITIAL_CASH))
                .expect("positive paper initial cash"),
        }
    }
    pub fn wallet(&self) -> WalletSnapshot {
        self.wallet.snapshot().expect("journaled cash totals within i64")
    }
    pub fn unresolved_order(&self) -> Option<u64> { self.pending.map(|p| p.id) }
    pub fn pending_quantity_raw(&self) -> i64 {
        self.pending.map_or(0, |p| p.requested - p.filled)
    }
    pub fn pending_is_cover(&self) -> bool { self.pending.is_some_and(|p| !p.entry) }

    fn calculate_cover_pnl(&self, quantity: i64, execution_price: i64)
        -> Result<i64, JournalError>
    {
        let scale = i128::from(SCALE);
        let q = i128::from(quantity);
        let gross = (i128::from(self.average_entry_price_raw) -
            i128::from(execution_price)).checked_mul(q)
            .ok_or(JournalError::Arithmetic)? / scale;
        // Symmetric 2bp fee each side, charged at fill settlement as a
        // modelling simplification; no borrow interest / carry.
        let fees = (i128::from(self.average_entry_price_raw) +
            i128::from(execution_price)).checked_mul(q)
            .ok_or(JournalError::Arithmetic)? * FEE_BPS_PER_SIDE / (scale * 10_000);
        i64::try_from(gross - fees).map_err(|_| JournalError::Arithmetic)
    }

    fn apply(&mut self, r: Record) -> Result<(), JournalError> {
        if r.sequence != self.records + 1 || self.records >= MAX_EVENTS {
            return Err(JournalError::InvalidTransition);
        }
        if self.halted && r.kind != RecordKind::EmergencyHalt {
            return Err(JournalError::InvalidTransition);
        }
        let invalid = || JournalError::InvalidTransition;
        match r.kind {
            RecordKind::EntryReserve | RecordKind::CoverReserve => {
                let entry = r.kind == RecordKind::EntryReserve;
                if self.pending.is_some() || r.order_id <= self.last_order_id
                    || r.order_id == 0 || r.quantity_raw <= 0 || r.price_raw <= 0
                    || r.net_pnl_raw != 0
                    || (entry && self.confirmed_short_qty_raw != 0)
                    || (!entry && (self.confirmed_short_qty_raw <= 0 ||
                        r.quantity_raw > self.confirmed_short_qty_raw))
                { return Err(invalid()); }
                self.pending = Some(Pending {
                    id: r.order_id, entry,
                    requested: r.quantity_raw, filled: 0,
                    limit: r.price_raw,
                });
                self.last_order_id = r.order_id;
            }
            RecordKind::EntryFill | RecordKind::CoverFill => {
                let Some(mut pending) = self.pending else { return Err(invalid()); };
                let entry = r.kind == RecordKind::EntryFill;
                if pending.entry != entry || pending.id != r.order_id
                    || r.quantity_raw <= 0 || r.price_raw <= 0
                    || r.quantity_raw > pending.requested - pending.filled
                    || (entry && r.price_raw < pending.limit)
                    || (!entry && r.price_raw > pending.limit)
                { return Err(invalid()); }
                if entry {
                    // The single-price basis is only valid for one fill price
                    // across the entry. Differing prints require an average
                    // cost model and are rejected, not silently mispriced.
                    if self.confirmed_short_qty_raw > 0 &&
                        self.average_entry_price_raw != r.price_raw
                    { return Err(invalid()); }
                    self.average_entry_price_raw = r.price_raw;
                    self.confirmed_short_qty_raw = self.confirmed_short_qty_raw
                        .checked_add(r.quantity_raw).ok_or(JournalError::Arithmetic)?;
                    if r.net_pnl_raw != 0 { return Err(invalid()); }
                } else {
                    if r.quantity_raw > self.confirmed_short_qty_raw {
                        return Err(invalid());
                    }
                    let pnl = self.calculate_cover_pnl(r.quantity_raw, r.price_raw)?;
                    if pnl != r.net_pnl_raw { return Err(invalid()); }
                    self.wallet.settle_close(r.sequence, FixedUsd(pnl), 35)
                        .map_err(|_| JournalError::InvalidTransition)?;
                    self.confirmed_short_qty_raw -= r.quantity_raw;
                    self.realized_net_pnl_raw = self.realized_net_pnl_raw
                        .checked_add(pnl).ok_or(JournalError::Arithmetic)?;
                    if self.confirmed_short_qty_raw == 0 {
                        self.average_entry_price_raw = 0;
                    }
                }
                pending.filled += r.quantity_raw;
                self.pending = if pending.filled == pending.requested {
                    None
                } else { Some(pending) };
            }
            RecordKind::EntryCancel | RecordKind::CoverCancel => {
                let Some(pending) = self.pending else { return Err(invalid()); };
                if pending.id != r.order_id
                    || pending.entry != (r.kind == RecordKind::EntryCancel)
                    || r.quantity_raw != 0 || r.price_raw != 0 || r.net_pnl_raw != 0
                { return Err(invalid()); }
                self.pending = None;
            }
            RecordKind::EmergencyHalt => {
                if r.order_id != 0 || r.quantity_raw != 0 || r.price_raw != 0
                    || r.net_pnl_raw != 0 { return Err(invalid()); }
                self.halted = true;
            }
        }
        if self.confirmed_short_qty_raw != r.position_after_raw {
            return Err(invalid());
        }
        self.records += 1;
        Ok(())
    }
}
fn fnv(bytes: &[u8]) -> u64 {
    bytes.iter().fold(0xcbf29ce484222325_u64, |hash, b|
        (hash ^ u64::from(*b)).wrapping_mul(0x100000001b3))
}
fn record_line(r: Record, previous_hash: u64) -> String {
    let data = format!(
        "{}|{}|{}|{}|{}|{}|{}|{:016x}",
        r.sequence, r.kind as u8, r.order_id, r.quantity_raw,
        r.price_raw, r.net_pnl_raw, r.position_after_raw, previous_hash,
    );
    format!("{data}|{:016x}\n", fnv(data.as_bytes()))
}
pub struct Journal {
    file: Option<File>,
    summary: ReplaySummary,
    hash: u64,
    poisoned: bool,
}
impl Journal {
    /// A new file per supervised paper session. Existing paths fail instead
    /// of accidentally restarting outstanding orders or reusing identities.
    pub fn create(path: &Path) -> Result<Self, JournalError> {
        let mut file = OpenOptions::new().write(true).read(true)
            .create_new(true).open(path)?;
        file.write_all(HEADER.as_bytes())?;
        file.sync_all()?;
        if let Some(parent) = path.parent() {
            File::open(if parent.as_os_str().is_empty() {
                Path::new(".")
            } else { parent })?.sync_all()?;
        }
        Ok(Self {
            file: Some(file), summary: ReplaySummary::new(),
            hash: fnv(HEADER.as_bytes()), poisoned: false,
        })
    }

    /// Inspects and fully validates a journal; NEVER restores routing
    /// capability. Unexpected tail, tampering or a missing event fails closed.
    pub fn inspect(path: &Path) -> Result<Self, JournalError> {
        let mut file = File::open(path)?;
        if file.metadata()?.len() > MAX_JOURNAL_BYTES {
            return Err(JournalError::Integrity);
        }
        let mut text = String::new();
        file.read_to_string(&mut text)?;
        if !text.starts_with(HEADER) || !text.ends_with('\n') {
            return Err(JournalError::Integrity);
        }
        let mut summary = ReplaySummary::new();
        let mut hash = fnv(HEADER.as_bytes());
        for line in text[HEADER.len()..].split_terminator('\n') {
            let fields: Vec<_> = line.split('|').collect();
            if fields.len() != 9 { return Err(JournalError::Integrity); }
            let n = |i: usize| fields[i].parse::<i64>()
                .map_err(|_| JournalError::Integrity);
            let seq = fields[0].parse::<u64>().map_err(|_| JournalError::Integrity)?;
            let kind = RecordKind::try_from(
                fields[1].parse::<u8>().map_err(|_| JournalError::Integrity)?)?;
            let id = fields[2].parse::<u64>().map_err(|_| JournalError::Integrity)?;
            let r = Record {
                sequence: seq, kind, order_id: id,
                quantity_raw: n(3)?, price_raw: n(4)?,
                net_pnl_raw: n(5)?, position_after_raw: n(6)?,
            };
            if record_line(r, hash) != format!("{line}\n") {
                return Err(JournalError::Integrity);
            }
            summary.apply(r).map_err(|_| JournalError::Integrity)?;
            hash = u64::from_str_radix(fields[8], 16)
                .map_err(|_| JournalError::Integrity)?;
        }
        Ok(Self { file: None, summary, hash, poisoned: false })
    }
    pub fn summary(&self) -> &ReplaySummary { &self.summary }
    pub fn is_inspect_only(&self) -> bool { self.file.is_none() }
    pub fn append(&mut self, kind: RecordKind, id: u64, quantity: i64,
        price: i64, pnl: i64, post_position: i64,
    ) -> Result<Record, JournalError> {
        if self.poisoned { return Err(JournalError::Poisoned); }
        if self.file.is_none() { return Err(JournalError::InspectOnly); }
        let r = Record {
            sequence: self.summary.records + 1, kind,
            order_id: id, quantity_raw: quantity, price_raw: price,
            net_pnl_raw: pnl, position_after_raw: post_position,
        };
        let mut staged = self.summary.clone();
        staged.apply(r)?;
        let line = record_line(r, self.hash);
        let file = self.file.as_mut().ok_or(JournalError::InspectOnly)?;
        if file.write_all(line.as_bytes()).and_then(|()| file.sync_all()).is_err() {
            // The bytes may have been partially written. Never retry the
            // same event in-process. Only inspect-only recovery is allowed.
            self.poisoned = true;
            return Err(JournalError::Poisoned);
        }
        self.summary = staged;
        self.hash = fnv(line[..line.rfind('|').expect("journal hash")].as_bytes());
        // Hash chain uses the exact decimal-prefix bytes through prev_hash.
        Ok(r)
    }
    pub fn cover_pnl(&self, qty: i64, px: i64) -> Result<i64, JournalError> {
        self.summary.calculate_cover_pnl(qty, px)
    }
}
