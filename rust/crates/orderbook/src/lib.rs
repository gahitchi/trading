//! Fixed-capacity L2 book and venue-specific snapshot/delta synchronization.
//!
//! A 1,000-level sorted flat array has O(1) best quotes, bounded O(1,000)
//! insertion shifts, and no allocation after initialization. It is NOT an
//! asymptotically O(1) random-price-update data structure.
//!
//! The adapter must deliver complete, ordered batches, not arbitrary
//! individual levels: Binance `pu` and Bybit `seq` are batch-level metadata
//! that cannot be reconstructed from the 64-byte MarketEvent alone.

use std::collections::VecDeque;
use trading_core_types::{FixedBpsPerUsd, FixedPrice, FixedQty, FixedUsd, NumericError, SCALE};

pub const DEFAULT_MAX_LEVELS: usize = 1_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BookError {
    InvalidCapacity, InvalidPrice, InvalidQuantity, InvalidSnapshot,
    CrossedBook, NumericOverflow, TruncatedDepth,
}
impl From<NumericError> for BookError {
    fn from(_: NumericError) -> Self { Self::NumericOverflow }
}

#[repr(C)]
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct PriceLevel {
    pub price: FixedPrice,
    pub qty: FixedQty,
}
impl PriceLevel {
    pub fn new(price: FixedPrice, qty: FixedQty) -> Result<Self, BookError> {
        if price.0 <= 0 { return Err(BookError::InvalidPrice); }
        if qty.0 < 0 { return Err(BookError::InvalidQuantity); }
        Ok(Self { price, qty })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side { Bid, Ask }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LevelDelta {
    pub side: Side,
    pub level: PriceLevel,
}
impl LevelDelta {
    pub fn new(side: Side, price: FixedPrice, qty: FixedQty) -> Result<Self, BookError> {
        Ok(Self { side, level: PriceLevel::new(price, qty)? })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DepthWithinBps {
    pub bid_qty: FixedQty,
    pub ask_qty: FixedQty,
    pub bid_notional: FixedUsd,
    pub ask_notional: FixedUsd,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LiquidityWall {
    pub side: Side,
    pub price: FixedPrice,
    pub qty: FixedQty,
    pub index: usize,
}

/// Price levels are unique, nonnegative, and sorted (bids descending, asks
/// ascending). All capacity is allocated in `new`.
#[derive(Debug)]
pub struct FlatOrderBook {
    bids: Vec<PriceLevel>,
    asks: Vec<PriceLevel>,
    max_levels: usize,
}
impl FlatOrderBook {
    pub fn new(max_levels: usize) -> Result<Self, BookError> {
        if max_levels == 0 { return Err(BookError::InvalidCapacity); }
        Ok(Self { bids: Vec::with_capacity(max_levels),
            asks: Vec::with_capacity(max_levels), max_levels })
    }
    pub fn bids(&self) -> &[PriceLevel] { &self.bids }
    pub fn asks(&self) -> &[PriceLevel] { &self.asks }
    pub fn capacity_per_side(&self) -> usize { self.max_levels }
    pub fn best_bid(&self) -> Option<PriceLevel> { self.bids.first().copied() }
    pub fn best_ask(&self) -> Option<PriceLevel> { self.asks.first().copied() }
    pub fn clear(&mut self) { self.bids.clear(); self.asks.clear(); }
    pub fn has_uncrossed_top(&self) -> bool {
        match (self.best_bid(), self.best_ask()) {
            (Some(b), Some(a)) => b.price.0 < a.price.0,
            _ => false,
        }
    }
    fn is_crossed(&self) -> bool {
        matches!((self.best_bid(), self.best_ask()), (Some(b), Some(a)) if b.price.0 >= a.price.0)
    }
    fn validate_sorted(levels: &[PriceLevel], side: Side) -> Result<(), BookError> {
        for (index, level) in levels.iter().enumerate() {
            if level.price.0 <= 0 || level.qty.0 <= 0 { return Err(BookError::InvalidSnapshot); }
            if index > 0 {
                let previous = levels[index - 1].price.0;
                let sorted = match side { Side::Bid => previous > level.price.0,
                    Side::Ask => previous < level.price.0 };
                if !sorted { return Err(BookError::InvalidSnapshot); }
            }
        }
        Ok(())
    }
    /// Snapshot must already be sorted by the market-data adapter. Reject
    /// malformed snapshots instead of sorting them into a plausible book.
    pub fn load_snapshot(&mut self, bids: &[PriceLevel], asks: &[PriceLevel]) -> Result<(), BookError> {
        self.clear();
        if bids.len() > self.max_levels || asks.len() > self.max_levels {
            return Err(BookError::InvalidSnapshot);
        }
        Self::validate_sorted(bids, Side::Bid)?;
        Self::validate_sorted(asks, Side::Ask)?;
        if let (Some(b), Some(a)) = (bids.first(), asks.first()) {
            if b.price.0 >= a.price.0 { return Err(BookError::CrossedBook); }
        }
        // Guaranteed not to allocate: len <= preallocated Vec capacities.
        self.bids.extend_from_slice(bids);
        self.asks.extend_from_slice(asks);
        Ok(())
    }
    fn update_side(levels: &mut Vec<PriceLevel>, side: Side, delta: PriceLevel,
                   max_levels: usize)
    {
        let position = levels.binary_search_by(|probe| match side {
            Side::Bid => delta.price.0.cmp(&probe.price.0),
            Side::Ask => probe.price.0.cmp(&delta.price.0),
        });
        match position {
            Ok(index) => {
                if delta.qty.0 == 0 { levels.remove(index); }
                else { levels[index] = delta; }
            }
            Err(index) => {
                if delta.qty.0 == 0 || index >= max_levels { return; }
                if levels.len() == max_levels {
                    levels.pop(); // Make room before `insert`: never reallocate.
                }
                levels.insert(index, delta);
            }
        }
    }
    pub fn apply_delta(&mut self, delta: LevelDelta) -> Result<(), BookError> {
        if delta.level.price.0 <= 0 { return Err(BookError::InvalidPrice); }
        if delta.level.qty.0 < 0 { return Err(BookError::InvalidQuantity); }
        match delta.side {
            Side::Bid => Self::update_side(&mut self.bids, Side::Bid, delta.level, self.max_levels),
            Side::Ask => Self::update_side(&mut self.asks, Side::Ask, delta.level, self.max_levels),
        }
        Ok(())
    }
    /// Apply a complete update transaction. Do not call the strategy until a
    /// completed batch has been checked for crossed quotes.
    pub fn apply_batch(&mut self, deltas: &[LevelDelta]) -> Result<(), BookError> {
        for delta in deltas {
            if delta.level.price.0 <= 0 { return Err(BookError::InvalidPrice); }
            if delta.level.qty.0 < 0 { return Err(BookError::InvalidQuantity); }
        }
        for &delta in deltas { self.apply_delta(delta)?; }
        self.validate_top()
    }
    pub fn validate_top(&self) -> Result<(), BookError> {
        if self.is_crossed() { Err(BookError::CrossedBook) } else { Ok(()) }
    }
    /// Sum quantities and notionals within 20bp of each side's best quote.
    /// Assumes the quote currency is USD; other currencies need FX conversion.
    pub fn depth_20bps(&self) -> Result<Option<DepthWithinBps>, BookError> {
        let (Some(bid), Some(ask)) = (self.best_bid(), self.best_ask()) else { return Ok(None); };
        // If the retained worst level is still within 20bps, a 1,000-level
        // truncated book cannot prove the true aggregate depth: fail closed.
        if (self.bids.len() == self.max_levels
                && i128::from(self.bids[self.bids.len() - 1].price.0) * 10_000
                    >= i128::from(bid.price.0) * 9_980)
            || (self.asks.len() == self.max_levels
                && i128::from(self.asks[self.asks.len() - 1].price.0) * 10_000
                    <= i128::from(ask.price.0) * 10_020)
        { return Err(BookError::TruncatedDepth); }
        let mut bq: i128 = 0;
        let mut aq: i128 = 0;
        let mut bn: i128 = 0;
        let mut an: i128 = 0;
        for level in &self.bids {
            if i128::from(level.price.0) * 10_000 < i128::from(bid.price.0) * 9_980 { break; }
            bq += i128::from(level.qty.0);
            bn += i128::from(level.price.0) * i128::from(level.qty.0) / i128::from(SCALE);
        }
        for level in &self.asks {
            if i128::from(level.price.0) * 10_000 > i128::from(ask.price.0) * 10_020 { break; }
            aq += i128::from(level.qty.0);
            an += i128::from(level.price.0) * i128::from(level.qty.0) / i128::from(SCALE);
        }
        let checked = |x: i128| i64::try_from(x).map_err(|_| BookError::NumericOverflow);
        Ok(Some(DepthWithinBps {
            bid_qty: FixedQty(checked(bq)?), ask_qty: FixedQty(checked(aq)?),
            bid_notional: FixedUsd(checked(bn)?), ask_notional: FixedUsd(checked(an)?),
        }))
    }
    /// Formula in the prompt is a static ten-level book-depth slope, not a
    /// statistically estimated Kyle lambda. Result is fixed-point bps/USD.
    /// Returns None if fewer than ten bids or total displayed notional is 0.
    pub fn bid_book_slope_10(&self) -> Result<Option<FixedBpsPerUsd>, BookError> {
        if self.bids.len() < 10 { return Ok(None); }
        let best = i128::from(self.bids[0].price.0);
        let tenth = i128::from(self.bids[9].price.0);
        let mut top10_notional: i128 = 0;
        for level in self.bids.iter().take(10) {
            top10_notional += i128::from(level.price.0) * i128::from(level.qty.0)
                / i128::from(SCALE);
        }
        if top10_notional == 0 { return Ok(None); }
        let drop_bps_scaled = (best - tenth) * 10_000 * i128::from(SCALE) / best;
        let lambda_scaled = drop_bps_scaled * i128::from(SCALE) / top10_notional;
        Ok(Some(FixedBpsPerUsd(i64::try_from(lambda_scaled).map_err(|_| BookError::NumericOverflow)?)))
    }
    /// Search a bounded top-30 window; threshold is strict >4.5 times mean.
    /// Integer comparison avoids floating-point and premature rounding.
    pub fn find_nearest_liquidity_wall(&self, side: Side) -> Option<LiquidityWall> {
        let levels = match side { Side::Bid => &self.bids, Side::Ask => &self.asks };
        let count = levels.len().min(30);
        if count == 0 { return None; }
        let total: i128 = levels.iter().take(count).map(|x| i128::from(x.qty.0)).sum();
        if total == 0 { return None; }
        for (index, level) in levels.iter().take(count).enumerate() {
            if i128::from(level.qty.0) * 2 * count as i128 > 9 * total {
                return Some(LiquidityWall { side, price: level.price, qty: level.qty, index });
            }
        }
        None
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SequencePolicy { BinanceUsdM, BybitLinear, BybitFullDepth }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SyncState { Disconnected, FetchingSnapshot, Validating, Live }

/// The feed adapter must retain Binance futures `pu` from the original message.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DepthSequence {
    Binance { first: u64, final_id: u64, previous_final: Option<u64> },
    Bybit { update_id: u64, cross_seq: u64 },
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SnapshotSequence {
    Binance { last_update_id: u64 },
    Bybit { update_id: u64, cross_seq: u64 },
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SyncOutcome { Buffered, AwaitingBridge, SkippedStale, Activated,
    Applied, NeedsSnapshot }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SyncError { InvalidState, WrongVenue, InvalidSequence, InvalidCapacity, InvalidData(BookError) }

#[derive(Debug, Clone, Copy)]
struct BufferedBatch { sequence: DepthSequence, count: usize }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Continuity { Skip, Apply, Gap }

/// All queues reserve their maximum capacity in `new`; no heap growth occurs
/// while applying or buffering deltas. If any queue fills or a feed gaps,
/// the book is immediately invalidated and must be re-snapshotted.
pub struct DepthSynchronizer {
    book: FlatOrderBook,
    policy: SequencePolicy,
    state: SyncState,
    last_id: u64,
    last_cross_seq: u64,
    buffered_deltas: VecDeque<LevelDelta>,
    buffered_batches: VecDeque<BufferedBatch>,
    max_buffered_deltas: usize,
    max_buffered_batches: usize,
}
impl DepthSynchronizer {
    pub fn new(policy: SequencePolicy, max_levels: usize,
               max_buffered_deltas: usize, max_buffered_batches: usize) -> Result<Self, SyncError>
    {
        if max_buffered_deltas == 0 || max_buffered_batches == 0 {
            return Err(SyncError::InvalidCapacity);
        }
        Ok(Self { book: FlatOrderBook::new(max_levels).map_err(SyncError::InvalidData)?,
            policy, state: SyncState::Disconnected, last_id: 0, last_cross_seq: 0,
            buffered_deltas: VecDeque::with_capacity(max_buffered_deltas),
            buffered_batches: VecDeque::with_capacity(max_buffered_batches),
            max_buffered_deltas, max_buffered_batches })
    }
    pub fn state(&self) -> SyncState { self.state }
    pub fn book(&self) -> &FlatOrderBook { &self.book }
    pub fn last_update_id(&self) -> u64 { self.last_id }
    pub fn is_tradable(&self) -> bool {
        self.state == SyncState::Live && self.book.has_uncrossed_top()
    }
    pub fn disconnect(&mut self) {
        self.clear_everything();
        self.state = SyncState::Disconnected;
    }
    pub fn start_snapshot_fetch(&mut self) {
        self.clear_everything();
        self.state = SyncState::FetchingSnapshot;
    }
    fn clear_everything(&mut self) {
        self.book.clear();
        self.last_id = 0;
        self.last_cross_seq = 0;
        self.buffered_deltas.clear();
        self.buffered_batches.clear();
    }
    fn restart(&mut self) -> SyncOutcome {
        self.start_snapshot_fetch();
        SyncOutcome::NeedsSnapshot
    }
    fn compatible(&self, sequence: DepthSequence) -> bool {
        matches!((self.policy, sequence),
            (SequencePolicy::BinanceUsdM, DepthSequence::Binance { .. })
            | (SequencePolicy::BybitLinear | SequencePolicy::BybitFullDepth, DepthSequence::Bybit { .. }))
    }
    fn valid_sequence(sequence: DepthSequence) -> bool {
        match sequence {
            DepthSequence::Binance { first, final_id, .. } => first > 0 && final_id >= first,
            DepthSequence::Bybit { update_id, cross_seq } => update_id > 0 && cross_seq > 0,
        }
    }
    fn check_sequence(&self, sequence: DepthSequence) -> Continuity {
        match sequence {
            DepthSequence::Binance { first, final_id, previous_final } => {
                if self.state == SyncState::Validating {
                    // USD-M Futures differs from spot: first processed batch
                    // must satisfy U <= snapshot.lastUpdateId <= u; batches
                    // with u == snapshot.lastUpdateId may be valid anchors.
                    if final_id < self.last_id { return Continuity::Skip; }
                    if first > self.last_id { return Continuity::Gap; }
                    return Continuity::Apply;
                }
                if final_id <= self.last_id { return Continuity::Skip; }
                let Some(next) = self.last_id.checked_add(1) else { return Continuity::Gap; };
                if previous_final != Some(self.last_id) || first > next {
                    return Continuity::Gap;
                }
                Continuity::Apply
            }
            DepthSequence::Bybit { update_id, cross_seq } => {
                if update_id == 1 && self.last_id > 1 { return Continuity::Gap; }
                if update_id <= self.last_id { return Continuity::Skip; }
                if cross_seq <= self.last_cross_seq { return Continuity::Gap; }
                // Bybit FULL depth publishes consecutive u; ordinary 50/200/1000
                // depth feeds do not promise that a gap in u proves packet loss.
                if self.policy == SequencePolicy::BybitFullDepth
                    && update_id != self.last_id.saturating_add(1) {
                    return Continuity::Gap;
                }
                Continuity::Apply
            }
        }
    }
    fn record_sequence(&mut self, sequence: DepthSequence) {
        match sequence {
            DepthSequence::Binance { final_id, .. } => self.last_id = final_id,
            DepthSequence::Bybit { update_id, cross_seq } => {
                self.last_id = update_id;
                self.last_cross_seq = cross_seq;
            }
        }
    }
    fn validate_deltas(deltas: &[LevelDelta]) -> Result<(), SyncError> {
        for delta in deltas {
            PriceLevel::new(delta.level.price, delta.level.qty).map_err(SyncError::InvalidData)?;
        }
        Ok(())
    }
    /// Accept complete batches. Network adapters must not split a batch into
    /// several calls. A Binance missing `pu` fails closed once Live.
    pub fn on_depth_batch(&mut self, sequence: DepthSequence,
                          deltas: &[LevelDelta]) -> Result<SyncOutcome, SyncError>
    {
        if self.state == SyncState::Disconnected { return Err(SyncError::InvalidState); }
        if !self.compatible(sequence) { self.restart(); return Err(SyncError::WrongVenue); }
        if !Self::valid_sequence(sequence) { self.restart(); return Err(SyncError::InvalidSequence); }
        if let Err(error) = Self::validate_deltas(deltas) {
            self.restart(); return Err(error);
        }
        if self.state == SyncState::FetchingSnapshot {
            if self.buffered_batches.len() >= self.max_buffered_batches
                || deltas.len() > self.max_buffered_deltas - self.buffered_deltas.len() {
                return Ok(self.restart());
            }
            self.buffered_batches.push_back(BufferedBatch { sequence, count: deltas.len() });
            self.buffered_deltas.extend(deltas.iter().copied());
            return Ok(SyncOutcome::Buffered);
        }
        self.apply_verified_batch(sequence, deltas)
    }
    fn apply_verified_batch(&mut self, sequence: DepthSequence,
                            deltas: &[LevelDelta]) -> Result<SyncOutcome, SyncError>
    {
        match self.check_sequence(sequence) {
            Continuity::Skip => return Ok(if self.state == SyncState::Validating {
                SyncOutcome::AwaitingBridge } else { SyncOutcome::SkippedStale }),
            Continuity::Gap => return Ok(self.restart()),
            Continuity::Apply => (),
        }
        if let Err(error) = self.book.apply_batch(deltas) {
            self.restart(); return Err(SyncError::InvalidData(error));
        }
        self.record_sequence(sequence);
        if self.state == SyncState::Validating {
            self.state = SyncState::Live;
            Ok(SyncOutcome::Activated)
        } else {
            Ok(SyncOutcome::Applied)
        }
    }
    /// Install a Binance REST snapshot or a Bybit WS snapshot (or full-depth
    /// REST snapshot), then drain all buffered feed batches in sequence.
    pub fn install_snapshot(&mut self, sequence: SnapshotSequence, bids: &[PriceLevel],
                            asks: &[PriceLevel]) -> Result<SyncOutcome, SyncError>
    {
        if self.state != SyncState::FetchingSnapshot { return Err(SyncError::InvalidState); }
        let matching = matches!((self.policy, sequence),
            (SequencePolicy::BinanceUsdM, SnapshotSequence::Binance { .. })
            | (SequencePolicy::BybitLinear | SequencePolicy::BybitFullDepth,
               SnapshotSequence::Bybit { .. }));
        if !matching { self.restart(); return Err(SyncError::WrongVenue); }
        if self.policy == SequencePolicy::BybitFullDepth {
            // Bybit's full-depth delta-only feed requires a REST snapshot
            // whose (seq,u) matches a buffered delta exactly. Without that
            // match the bootstrap is not proven, even if u appears adjacent.
            let SnapshotSequence::Bybit { update_id, cross_seq } = sequence else {
                return Ok(self.restart());
            };
            if !self.buffered_batches.iter().any(|batch| matches!(batch.sequence,
                DepthSequence::Bybit { update_id: id, cross_seq: cross }
                    if id == update_id && cross == cross_seq)) {
                return Ok(self.restart());
            }
        }
        if let Err(error) = self.book.load_snapshot(bids, asks) {
            self.restart(); return Err(SyncError::InvalidData(error));
        }
        match sequence {
            SnapshotSequence::Binance { last_update_id } => self.last_id = last_update_id,
            SnapshotSequence::Bybit { update_id, cross_seq } => {
                self.last_id = update_id;
                self.last_cross_seq = cross_seq;
            }
        }
        if self.last_id == 0 || (self.policy != SequencePolicy::BinanceUsdM && self.last_cross_seq == 0) {
            self.restart(); return Err(SyncError::InvalidSequence);
        }
        self.state = SyncState::Validating;
        while let Some(batch) = self.buffered_batches.pop_front() {
            // Check sequence BEFORE touching any level, then consume exactly
            // the batch's number of elements from the pre-allocated ring.
            let continuity = self.check_sequence(batch.sequence);
            if continuity == Continuity::Gap { return Ok(self.restart()); }
            if continuity == Continuity::Skip {
                for _ in 0..batch.count { self.buffered_deltas.pop_front(); }
                continue;
            }
            for _ in 0..batch.count {
                let Some(delta) = self.buffered_deltas.pop_front() else {
                    return Ok(self.restart());
                };
                if let Err(error) = self.book.apply_delta(delta) {
                    self.restart(); return Err(SyncError::InvalidData(error));
                }
            }
            if let Err(error) = self.book.validate_top() {
                self.restart(); return Err(SyncError::InvalidData(error));
            }
            self.record_sequence(batch.sequence);
            self.state = SyncState::Live;
        }
        Ok(if self.state == SyncState::Live { SyncOutcome::Activated }
           else { SyncOutcome::AwaitingBridge })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(price: &str, qty: &str) -> PriceLevel {
        PriceLevel::new(price.parse().unwrap(), qty.parse().unwrap()).unwrap()
    }
    fn d(side: Side, price: &str, qty: &str) -> LevelDelta {
        LevelDelta::new(side, price.parse().unwrap(), qty.parse().unwrap()).unwrap()
    }
    fn simple_book() -> FlatOrderBook {
        let mut book = FlatOrderBook::new(1_000).unwrap();
        book.load_snapshot(&[p("100", "2"), p("99", "1")],
                           &[p("101", "4"), p("102", "3")]).unwrap();
        book
    }
    #[test]
    fn snapshot_validates_sorting_duplicates_and_crossed_quotes() {
        let mut b = FlatOrderBook::new(10).unwrap();
        assert_eq!(b.load_snapshot(&[p("99", "2"), p("100", "1")], &[p("101", "1")]),
            Err(BookError::InvalidSnapshot));
        assert_eq!(b.load_snapshot(&[p("100", "2"), p("100", "1")], &[p("101", "1")]),
            Err(BookError::InvalidSnapshot));
        assert_eq!(b.load_snapshot(&[p("102", "2")], &[p("101", "1")]),
            Err(BookError::CrossedBook));
        assert!(b.bids().is_empty());
    }
    #[test]
    fn update_inserts_updates_deletes_and_caps_best_levels() {
        let mut b = FlatOrderBook::new(2).unwrap();
        b.load_snapshot(&[p("100", "1"), p("99", "2")], &[p("101", "3")]).unwrap();
        b.apply_delta(d(Side::Bid, "98", "1")).unwrap();
        assert_eq!(b.bids().len(), 2);
        b.apply_delta(d(Side::Bid, "101", "4")).unwrap();
        assert_eq!(b.bids()[0], p("101", "4"));
        assert_eq!(b.bids()[1], p("100", "1"));
        b.apply_delta(d(Side::Bid, "101", "0")).unwrap();
        assert_eq!(b.bids().len(), 1);
        b.apply_delta(d(Side::Ask, "102", "1")).unwrap();
        assert_eq!(b.asks().len(), 2);
    }
    #[test]
    fn twenty_bps_depth_is_bounded_and_exact() {
        let mut b = FlatOrderBook::new(10).unwrap();
        b.load_snapshot(&[p("100", "1"), p("99.90", "2"), p("99.79", "5")],
                        &[p("101", "3"), p("101.15", "4"), p("101.30", "8")]).unwrap();
        let depth = b.depth_20bps().unwrap().unwrap();
        assert_eq!(depth.bid_qty, "3".parse().unwrap());
        assert_eq!(depth.ask_qty, "7".parse().unwrap());
        assert_eq!(depth.bid_notional, "299.8".parse().unwrap());
        assert_eq!(depth.ask_notional, "707.6".parse().unwrap());
    }
    #[test]
    fn top_ten_slope_and_wall_detection() {
        let mut b = FlatOrderBook::new(30).unwrap();
        let bids: Vec<_> = (0..30).map(|i| PriceLevel {
            price: FixedPrice((10_000 - i * 10) * SCALE),
            qty: FixedQty(if i == 5 { 60 * SCALE } else { SCALE }),
        }).collect();
        b.load_snapshot(&bids, &[p("101", "1")]).unwrap_err(); // crossed
        let asks = [p("10100", "1")];
        b.load_snapshot(&bids, &asks).unwrap();
        let slope = b.bid_book_slope_10().unwrap().unwrap();
        assert!(slope.raw() > 0);
        let wall = b.find_nearest_liquidity_wall(Side::Bid).unwrap();
        assert_eq!(wall.index, 5);
    }
    #[test]
    fn binance_buffers_then_bridges_snapshot_and_checks_pu() {
        let mut sync = DepthSynchronizer::new(SequencePolicy::BinanceUsdM, 10, 12, 4).unwrap();
        sync.start_snapshot_fetch();
        assert_eq!(sync.on_depth_batch(DepthSequence::Binance {
            first: 99, final_id: 100, previous_final: None }, &[d(Side::Bid, "100", "4")]).unwrap(),
            SyncOutcome::Buffered);
        assert_eq!(sync.on_depth_batch(DepthSequence::Binance {
            first: 101, final_id: 102, previous_final: Some(100) },
            &[d(Side::Ask, "101", "5")]).unwrap(), SyncOutcome::Buffered);
        assert_eq!(sync.install_snapshot(SnapshotSequence::Binance { last_update_id: 100 },
            &[p("100", "1")], &[p("101", "2")]).unwrap(), SyncOutcome::Activated);
        assert!(sync.is_tradable());
        assert_eq!(sync.book().best_bid().unwrap().qty, "4".parse().unwrap());
        assert_eq!(sync.book().best_ask().unwrap().qty, "5".parse().unwrap());
        assert_eq!(sync.last_update_id(), 102);
        assert_eq!(sync.on_depth_batch(DepthSequence::Binance {
            first: 103, final_id: 104, previous_final: Some(101) }, &[]).unwrap(),
            SyncOutcome::NeedsSnapshot);
        assert!(!sync.is_tradable());
    }
    #[test]
    fn binance_futures_rejects_spot_style_snapshot_bridge() {
        let mut sync = DepthSynchronizer::new(SequencePolicy::BinanceUsdM, 10, 12, 4).unwrap();
        sync.start_snapshot_fetch();
        sync.install_snapshot(SnapshotSequence::Binance { last_update_id: 100 },
            &[p("100", "1")], &[p("101", "1")]).unwrap();
        // U=lastUpdateId+1 is not a proven USD-M Futures first bridge.
        assert_eq!(sync.on_depth_batch(DepthSequence::Binance {
            first: 101, final_id: 102, previous_final: Some(100) }, &[]).unwrap(),
            SyncOutcome::NeedsSnapshot);
        assert!(!sync.is_tradable());
    }
    #[test]
    fn binance_gap_on_bridge_resets_book() {
        let mut sync = DepthSynchronizer::new(SequencePolicy::BinanceUsdM, 10, 12, 4).unwrap();
        sync.start_snapshot_fetch();
        sync.on_depth_batch(DepthSequence::Binance {
            first: 105, final_id: 106, previous_final: Some(104) }, &[]).unwrap();
        assert_eq!(sync.install_snapshot(SnapshotSequence::Binance { last_update_id: 100 },
            &[p("100", "1")], &[p("101", "1")]).unwrap(), SyncOutcome::NeedsSnapshot);
        assert!(sync.book().bids().is_empty());
    }
    #[test]
    fn bybit_normal_depth_uses_monotonic_ids_not_fake_consecutiveness() {
        let mut sync = DepthSynchronizer::new(SequencePolicy::BybitLinear, 10, 12, 4).unwrap();
        sync.start_snapshot_fetch();
        sync.install_snapshot(SnapshotSequence::Bybit { update_id: 50, cross_seq: 100 },
            &[p("100", "1")], &[p("101", "1")]).unwrap();
        assert_eq!(sync.state(), SyncState::Validating);
        assert_eq!(sync.on_depth_batch(DepthSequence::Bybit { update_id: 55, cross_seq: 101 },
            &[d(Side::Bid, "100", "2")]).unwrap(), SyncOutcome::Activated);
        assert!(sync.is_tradable());
        assert_eq!(sync.on_depth_batch(DepthSequence::Bybit { update_id: 1, cross_seq: 102 }, &[]).unwrap(),
            SyncOutcome::NeedsSnapshot);
    }
    #[test]
    fn bybit_full_depth_requires_consecutive_update_ids() {
        let mut sync = DepthSynchronizer::new(SequencePolicy::BybitFullDepth, 10, 12, 4).unwrap();
        sync.start_snapshot_fetch();
        sync.on_depth_batch(DepthSequence::Bybit { update_id: 50, cross_seq: 100 }, &[]).unwrap();
        sync.install_snapshot(SnapshotSequence::Bybit { update_id: 50, cross_seq: 100 },
            &[p("100", "1")], &[p("101", "1")]).unwrap();
        assert_eq!(sync.on_depth_batch(DepthSequence::Bybit { update_id: 52, cross_seq: 102 }, &[]).unwrap(),
            SyncOutcome::NeedsSnapshot);
        assert!(!sync.is_tradable());
    }
    #[test]
    fn full_depth_snapshot_requires_exact_buffered_anchor() {
        let mut sync = DepthSynchronizer::new(SequencePolicy::BybitFullDepth, 10, 12, 4).unwrap();
        sync.start_snapshot_fetch();
        sync.on_depth_batch(DepthSequence::Bybit { update_id: 50, cross_seq: 100 }, &[]).unwrap();
        assert_eq!(sync.install_snapshot(SnapshotSequence::Bybit { update_id: 50, cross_seq: 101 },
            &[p("100", "1")], &[p("101", "1")]).unwrap(), SyncOutcome::NeedsSnapshot);
        assert!(!sync.is_tradable());
    }
    #[test]
    fn unknown_20bps_depth_when_top_levels_fill_retention_limit() {
        let mut book = FlatOrderBook::new(2).unwrap();
        book.load_snapshot(&[p("100", "1"), p("99.99", "1")], &[p("101", "1")]).unwrap();
        assert_eq!(book.depth_20bps(), Err(BookError::TruncatedDepth));
    }
    #[test]
    fn a_full_book_keeps_sorted_best_levels() {
        let mut book = FlatOrderBook::new(3).unwrap();
        book.load_snapshot(&[p("100", "1"), p("99", "1"), p("98", "1")],
            &[p("101", "1")]).unwrap();
        book.apply_batch(&[d(Side::Bid, "99.5", "4"),
            d(Side::Bid, "97", "4"), d(Side::Bid, "100", "2")]).unwrap();
        assert_eq!(book.bids(), &[p("100", "2"), p("99.5", "4"), p("99", "1")]);
    }
    #[test]
    fn buffering_overflow_and_disconnect_fail_closed() {
        let mut sync = DepthSynchronizer::new(SequencePolicy::BinanceUsdM, 10, 1, 1).unwrap();
        sync.start_snapshot_fetch();
        sync.on_depth_batch(DepthSequence::Binance { first: 1, final_id: 2, previous_final: None },
            &[d(Side::Bid, "100", "1")]).unwrap();
        assert_eq!(sync.on_depth_batch(DepthSequence::Binance {
            first: 3, final_id: 4, previous_final: Some(2) },
            &[d(Side::Ask, "101", "1")]).unwrap(), SyncOutcome::NeedsSnapshot);
        sync.disconnect();
        assert_eq!(sync.state(), SyncState::Disconnected);
        assert_eq!(sync.on_depth_batch(DepthSequence::Binance {
            first: 5, final_id: 6, previous_final: Some(4) }, &[]), Err(SyncError::InvalidState));
    }
    #[test]
    fn crossed_delta_batch_invalidates_sync() {
        let mut sync = DepthSynchronizer::new(SequencePolicy::BybitLinear, 10, 12, 4).unwrap();
        sync.start_snapshot_fetch();
        sync.install_snapshot(SnapshotSequence::Bybit { update_id: 5, cross_seq: 10 },
            &[p("100", "1")], &[p("101", "1")]).unwrap();
        assert_eq!(sync.on_depth_batch(DepthSequence::Bybit { update_id: 6, cross_seq: 11 },
            &[d(Side::Bid, "102", "1")]), Err(SyncError::InvalidData(BookError::CrossedBook)));
        assert!(!sync.is_tradable());
    }
    #[test]
    fn valid_snapshot_does_not_imply_tradable_until_bridged() {
        let mut sync = DepthSynchronizer::new(SequencePolicy::BinanceUsdM, 10, 12, 4).unwrap();
        sync.start_snapshot_fetch();
        assert_eq!(sync.install_snapshot(SnapshotSequence::Binance { last_update_id: 10 },
            &[p("100", "1")], &[p("101", "1")]).unwrap(), SyncOutcome::AwaitingBridge);
        assert!(!sync.is_tradable());
        assert_eq!(sync.on_depth_batch(DepthSequence::Binance {
            first: 10, final_id: 11, previous_final: Some(10) }, &[]).unwrap(), SyncOutcome::Activated);
        assert!(sync.is_tradable());
    }
    #[test]
    fn simple_book_sanity() {
        let b = simple_book();
        assert!(b.has_uncrossed_top());
        assert!(b.bid_book_slope_10().unwrap().is_none());
    }
}
