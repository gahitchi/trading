use crate::{InstrumentSpecTable, LiveFrame, MarketDataError, SnapshotRequest};
use rtrb::{Consumer, Producer, RingBuffer};
use tokio::sync::mpsc;
use trading_orderbook::{
    DepthSequence, DepthSynchronizer, SequencePolicy, SnapshotSequence, SyncError, SyncOutcome,
    SyncState,
};

pub type MarketFrameProducer = Producer<LiveFrame>;
pub type MarketFrameConsumer = Consumer<LiveFrame>;
pub fn market_frame_ring(
    capacity: usize,
) -> Result<(MarketFrameProducer, MarketFrameConsumer), MarketDataError> {
    if capacity < 2 || capacity > 131_072 {
        return Err(MarketDataError::InvalidConfiguration(
            "ring capacity outside bounds",
        ));
    }
    Ok(RingBuffer::new(capacity))
}
struct BookSlot {
    sync: DepthSynchronizer,
    generation: u64,
    request_id: u64,
    active_request: Option<u64>,
}
pub struct LiveBookConsumer {
    books: Vec<BookSlot>,
    requests: mpsc::Sender<SnapshotRequest>,
}
impl LiveBookConsumer {
    /// One consumer owns the state machine and all order books. It should
    /// run on a dedicated pinned Core 1 thread, outside Tokio's runtime.
    pub fn new(
        table: &InstrumentSpecTable,
        requests: mpsc::Sender<SnapshotRequest>,
    ) -> Result<Self, MarketDataError> {
        let mut books = Vec::with_capacity(table.instruments().len());
        for _ in table.instruments() {
            books.push(BookSlot {
                sync: DepthSynchronizer::new(SequencePolicy::BinanceUsdM, 1000, 8192, 1024)
                    .map_err(MarketDataError::Sync)?,
                generation: 0,
                request_id: 0,
                active_request: None,
            });
        }
        Ok(Self { books, requests })
    }
    pub fn book(&self, id: u16) -> Option<&DepthSynchronizer> {
        self.books.get(usize::from(id)).map(|slot| &slot.sync)
    }
    fn request(
        slot: &mut BookSlot,
        id: u16,
        tx: &mpsc::Sender<SnapshotRequest>,
    ) -> Result<(), MarketDataError> {
        slot.request_id =
            slot.request_id
                .checked_add(1)
                .ok_or(MarketDataError::InvalidResponse(
                    "request sequence overflow",
                ))?;
        let r = SnapshotRequest {
            symbol_id: id,
            generation: slot.generation,
            request_id: slot.request_id,
        };
        slot.active_request = Some(r.request_id);
        tx.try_send(r).map_err(|_| MarketDataError::Backpressure)
    }
    /// Process *complete* batches and snapshots, preserving exchange
    /// sequence and snapshot generations. Duplicate/stale REST responses
    /// never revive a dead or disconnected book.
    pub fn on_frame(&mut self, frame: &LiveFrame) -> Result<Option<SyncOutcome>, MarketDataError> {
        match frame {
            LiveFrame::Reset { generation } => {
                for slot in &mut self.books {
                    if *generation < slot.generation {
                        continue;
                    }
                    slot.sync.start_snapshot_fetch();
                    slot.generation = *generation;
                }
                Ok(None)
            }
            LiveFrame::Depth {
                symbol_id,
                generation,
                first,
                final_id,
                previous_final,
                deltas,
                ..
            } => {
                let slot = self
                    .books
                    .get_mut(usize::from(*symbol_id))
                    .ok_or(MarketDataError::InvalidResponse("unknown depth symbol"))?;
                if *generation != slot.generation || slot.generation == 0 {
                    return Ok(None);
                }
                let result = slot
                    .sync
                    .on_depth_batch(
                        DepthSequence::Binance {
                            first: *first,
                            final_id: *final_id,
                            previous_final: Some(*previous_final),
                        },
                        deltas,
                    )
                    .map_err(MarketDataError::Sync)?;
                if result == SyncOutcome::NeedsSnapshot
                    || (result == SyncOutcome::Buffered && slot.active_request.is_none())
                {
                    // WS deltas must be buffered *before* taking the REST
                    // snapshot, per Binance's local-book bootstrap protocol.
                    Self::request(slot, *symbol_id, &self.requests)?;
                }
                Ok(Some(result))
            }
            LiveFrame::Snapshot {
                request,
                last_update_id,
                bids,
                asks,
            } => {
                let slot = self
                    .books
                    .get_mut(usize::from(request.symbol_id))
                    .ok_or(MarketDataError::InvalidResponse("unknown snapshot symbol"))?;
                if slot.generation != request.generation
                    || slot.active_request != Some(request.request_id)
                    || slot.sync.state() != SyncState::FetchingSnapshot
                {
                    return Ok(None);
                }
                slot.active_request = None;
                let result = slot
                    .sync
                    .install_snapshot(
                        SnapshotSequence::Binance {
                            last_update_id: *last_update_id,
                        },
                        bids,
                        asks,
                    )
                    .map_err(MarketDataError::Sync)?;
                if result == SyncOutcome::NeedsSnapshot {
                    Self::request(slot, request.symbol_id, &self.requests)?;
                }
                Ok(Some(result))
            }
            _ => Ok(None),
        }
    }
    pub fn active_books(&self) -> usize {
        self.books.iter().filter(|v| v.sync.is_tradable()).count()
    }
    pub fn disconnect_all(&mut self) {
        for b in &mut self.books {
            b.sync.disconnect();
            b.active_request = None;
        }
    }
}
impl From<SyncError> for MarketDataError {
    fn from(e: SyncError) -> Self {
        Self::Sync(e)
    }
}
