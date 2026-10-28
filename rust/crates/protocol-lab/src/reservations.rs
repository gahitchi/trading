//! Crash-detecting, append-only PAPER reservation evidence.
//! This is deliberately isolated from the authenticated execution router.
//! No release is supported until an independent settlement/reconciliation
//! protocol exists: an ACK, cancellation request or disconnected socket
//! can NEVER make exposure capacity available.
use sha2::{Digest, Sha256};
use std::{
    fs::{File, OpenOptions},
    io::{Read, Write},
    path::Path,
};
use trading_core_types::{FixedPrice, FixedQty, SCALE};

const PREFIX: &str = "TRADING-LAB-PAPER-RESERVATIONS-V1";
const MAX_BYTES: u64 = 4 * 1024 * 1024;
const MAX_RECORDS: u64 = 10_000;

#[derive(Debug)]
pub enum ReservationError {
    Io(std::io::Error),
    InvalidState,
    OverBudget,
    Arithmetic,
    Corruption,
    InspectOnly,
}
impl From<std::io::Error> for ReservationError {
    fn from(e: std::io::Error) -> Self {
        Self::Io(e)
    }
}
impl std::fmt::Display for ReservationError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(_) => write!(f, "reservation storage IO failed"),
            Self::InvalidState => write!(f, "invalid reservation transition"),
            Self::OverBudget => write!(f, "reservation exceeds paper budget"),
            Self::Arithmetic => write!(f, "reservation arithmetic overflow"),
            Self::Corruption => write!(f, "invalid reservation journal"),
            Self::InspectOnly => write!(f, "journal cannot resume routing"),
        }
    }
}
impl std::error::Error for ReservationError {}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReservationSnapshot {
    pub cap_raw: i64,
    pub gross_reserved_raw: i64,
    pub reservations: u64,
    pub highest_client_id: u64,
    pub inspect_only: bool,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Reservation {
    pub client_id: u64,
    pub symbol_id: u16,
    pub quantity_raw: i64,
    pub limit_price_raw: i64,
    pub notional_raw: i64,
}
pub struct PaperReservationJournal {
    file: Option<File>,
    snapshot: ReservationSnapshot,
    last_hash: [u8; 32],
    poisoned: bool,
}
fn hash(bytes: &[u8]) -> [u8; 32] {
    Sha256::digest(bytes).into()
}
fn hex32(bytes: &[u8; 32]) -> String {
    hex::encode(bytes)
}
fn strict_number<T: std::str::FromStr>(v: &str) -> Result<T, ReservationError> {
    // Reject +1, padded decimals, whitespace and signs not part of the
    // canonical unsigned financial event format.
    if v.is_empty() || !v.bytes().all(|c| c.is_ascii_digit()) || (v.len() > 1 && v.starts_with('0'))
    {
        return Err(ReservationError::Corruption);
    }
    v.parse().map_err(|_| ReservationError::Corruption)
}
fn notional(quantity: i64, price: i64) -> Result<i64, ReservationError> {
    if quantity <= 0 || price <= 0 {
        return Err(ReservationError::InvalidState);
    }
    let product = i128::from(quantity)
        .checked_mul(i128::from(price))
        .ok_or(ReservationError::Arithmetic)?;
    i64::try_from(product / i128::from(SCALE))
        .map_err(|_| ReservationError::Arithmetic)
        .and_then(|v| {
            if v > 0 {
                Ok(v)
            } else {
                Err(ReservationError::InvalidState)
            }
        })
}
impl PaperReservationJournal {
    /// Exclusive create-new with directory fsync. A second session must
    /// never reuse a prior file, including after a clean shutdown.
    pub fn create(path: &Path, cap_raw: i64) -> Result<Self, ReservationError> {
        if cap_raw <= 0 {
            return Err(ReservationError::InvalidState);
        }
        let mut f = OpenOptions::new().write(true).create_new(true).open(path)?;
        let header = format!("{PREFIX}|{cap_raw}\n");
        f.write_all(header.as_bytes())?;
        f.sync_all()?;
        if let Some(parent) = path.parent() {
            File::open(if parent.as_os_str().is_empty() {
                Path::new(".")
            } else {
                parent
            })?
            .sync_all()?;
        }
        Ok(Self {
            file: Some(f),
            snapshot: ReservationSnapshot {
                cap_raw,
                gross_reserved_raw: 0,
                reservations: 0,
                highest_client_id: 0,
                inspect_only: false,
            },
            last_hash: hash(header.as_bytes()),
            poisoned: false,
        })
    }
    pub fn snapshot(&self) -> ReservationSnapshot {
        self.snapshot
    }
    pub fn reserve(
        &mut self,
        client_id: u64,
        symbol_id: u16,
        qty: FixedQty,
        price: FixedPrice,
    ) -> Result<Reservation, ReservationError> {
        if self.snapshot.inspect_only {
            return Err(ReservationError::InspectOnly);
        }
        if self.poisoned {
            return Err(ReservationError::InvalidState);
        }
        if client_id == 0
            || client_id <= self.snapshot.highest_client_id
            || symbol_id >= 9
            || self.snapshot.reservations >= MAX_RECORDS
        {
            return Err(ReservationError::InvalidState);
        }
        let amount = notional(qty.0, price.0)?;
        let total = self
            .snapshot
            .gross_reserved_raw
            .checked_add(amount)
            .ok_or(ReservationError::Arithmetic)?;
        if total > self.snapshot.cap_raw {
            return Err(ReservationError::OverBudget);
        }
        let seq = self.snapshot.reservations + 1;
        let record = format!(
            "{seq}|{client_id}|{symbol_id}|{}|{}|{amount}|{total}|{}",
            qty.0,
            price.0,
            hex32(&self.last_hash)
        );
        let checksum = hash(record.as_bytes());
        let line = format!("{record}|{}\n", hex32(&checksum));
        let f = self.file.as_mut().ok_or(ReservationError::InspectOnly)?;
        // A write or fsync error can leave an ambiguous partial tail.
        // Poison the session unconditionally before touching the file.
        self.poisoned = true;
        f.write_all(line.as_bytes())?;
        f.sync_all()?;
        self.snapshot.gross_reserved_raw = total;
        self.snapshot.highest_client_id = client_id;
        self.snapshot.reservations = seq;
        self.last_hash = checksum;
        self.poisoned = false;
        Ok(Reservation {
            client_id,
            symbol_id,
            quantity_raw: qty.0,
            limit_price_raw: price.0,
            notional_raw: amount,
        })
    }
    /// Full bounded replay. Always read-only, never a restart permission.
    pub fn inspect(path: &Path) -> Result<Self, ReservationError> {
        let mut f = File::open(path)?;
        if f.metadata()?.len() > MAX_BYTES {
            return Err(ReservationError::Corruption);
        }
        let mut data = String::new();
        f.read_to_string(&mut data)?;
        if !data.ends_with('\n') {
            return Err(ReservationError::Corruption);
        }
        let mut lines = data.lines();
        let header = lines.next().ok_or(ReservationError::Corruption)?;
        let fields = header.split('|').collect::<Vec<_>>();
        if fields.len() != 2 || fields[0] != PREFIX {
            return Err(ReservationError::Corruption);
        }
        let cap_raw: i64 = strict_number(fields[1])?;
        if cap_raw <= 0 {
            return Err(ReservationError::Corruption);
        }
        let mut snapshot = ReservationSnapshot {
            cap_raw,
            gross_reserved_raw: 0,
            reservations: 0,
            highest_client_id: 0,
            inspect_only: true,
        };
        let mut last_hash = hash(format!("{header}\n").as_bytes());
        for line in lines {
            let columns = line.split('|').collect::<Vec<_>>();
            if columns.len() != 9 || snapshot.reservations >= MAX_RECORDS {
                return Err(ReservationError::Corruption);
            }
            let sequence: u64 = strict_number(columns[0])?;
            let client_id: u64 = strict_number(columns[1])?;
            let symbol_id: u16 = strict_number(columns[2])?;
            let quantity: i64 = strict_number(columns[3])?;
            let price: i64 = strict_number(columns[4])?;
            let amount: i64 = strict_number(columns[5])?;
            let after: i64 = strict_number(columns[6])?;
            if sequence != snapshot.reservations + 1
                || client_id <= snapshot.highest_client_id
                || symbol_id >= 9
                || columns[7] != hex32(&last_hash)
                || hex32(&hash(columns[..8].join("|").as_bytes())) != columns[8]
            {
                return Err(ReservationError::Corruption);
            }
            let expected = notional(quantity, price).map_err(|_| ReservationError::Corruption)?;
            let next = snapshot
                .gross_reserved_raw
                .checked_add(amount)
                .ok_or(ReservationError::Corruption)?;
            if amount != expected || amount <= 0 || after != next || next > snapshot.cap_raw {
                return Err(ReservationError::Corruption);
            }
            snapshot.gross_reserved_raw = next;
            snapshot.highest_client_id = client_id;
            snapshot.reservations = sequence;
            let decoded = hex::decode(columns[8]).map_err(|_| ReservationError::Corruption)?;
            last_hash = decoded
                .try_into()
                .map_err(|_| ReservationError::Corruption)?;
        }
        Ok(Self {
            file: None,
            snapshot,
            last_hash,
            poisoned: false,
        })
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    static NEXT: AtomicU64 = AtomicU64::new(1);
    fn path() -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "trading-reservation-{}-{}.journal",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ))
    }
    #[test]
    fn reservations_are_durable_monotonic_and_never_resumed() {
        let p = path();
        let mut journal = PaperReservationJournal::create(&p, 100 * SCALE).unwrap();
        assert_eq!(
            journal
                .reserve(42, 0, FixedQty(SCALE), FixedPrice(50 * SCALE))
                .unwrap()
                .notional_raw,
            50 * SCALE
        );
        assert!(matches!(
            journal.reserve(42, 0, FixedQty(SCALE), FixedPrice(50 * SCALE)),
            Err(ReservationError::InvalidState)
        ));
        assert!(matches!(
            journal.reserve(43, 0, FixedQty(SCALE), FixedPrice(51 * SCALE)),
            Err(ReservationError::OverBudget)
        ));
        assert_eq!(
            journal
                .reserve(44, 0, FixedQty(SCALE), FixedPrice(50 * SCALE))
                .unwrap()
                .notional_raw,
            50 * SCALE
        );
        assert!(PaperReservationJournal::create(&p, 100 * SCALE).is_err());
        drop(journal);
        let mut recovered = PaperReservationJournal::inspect(&p).unwrap();
        assert_eq!(recovered.snapshot().gross_reserved_raw, 100 * SCALE);
        assert_eq!(recovered.snapshot().reservations, 2);
        assert!(matches!(
            recovered.reserve(45, 0, FixedQty(SCALE), FixedPrice(SCALE)),
            Err(ReservationError::InspectOnly)
        ));
        std::fs::remove_file(p).unwrap();
    }
    #[test]
    fn corrupt_and_truncated_journals_fail_closed() {
        let p = path();
        let mut journal = PaperReservationJournal::create(&p, 100 * SCALE).unwrap();
        journal
            .reserve(1, 0, FixedQty(SCALE), FixedPrice(50 * SCALE))
            .unwrap();
        drop(journal);
        let data = std::fs::read_to_string(&p).unwrap();
        let tampered = data.replace("|5000000000|", "|4000000000|");
        assert_ne!(tampered, data);
        std::fs::write(&p, tampered).unwrap();
        assert!(PaperReservationJournal::inspect(&p).is_err());
        std::fs::write(&p, &data[..data.len() - 1]).unwrap();
        assert!(PaperReservationJournal::inspect(&p).is_err());
        std::fs::remove_file(p).unwrap();
    }
    #[test]
    fn invalid_values_never_mutate_persisted_capacity() {
        let p = path();
        let mut journal = PaperReservationJournal::create(&p, 100 * SCALE).unwrap();
        assert!(
            journal
                .reserve(0, 0, FixedQty(SCALE), FixedPrice(SCALE))
                .is_err()
        );
        assert!(
            journal
                .reserve(1, 9, FixedQty(SCALE), FixedPrice(SCALE))
                .is_err()
        );
        assert!(
            journal
                .reserve(1, 0, FixedQty(0), FixedPrice(SCALE))
                .is_err()
        );
        assert_eq!(journal.snapshot().reservations, 0);
        drop(journal);
        assert_eq!(
            PaperReservationJournal::inspect(&p)
                .unwrap()
                .snapshot()
                .reservations,
            0
        );
        std::fs::remove_file(p).unwrap();
    }
}
