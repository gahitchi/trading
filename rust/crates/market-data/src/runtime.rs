use crate::instruments::rest_client;
use crate::wire::{parse_combined, parse_depth_snapshot};
use crate::{
    BinanceEnvironment, InstrumentSpecTable, LiveFrame, MarketDataError, MarketFrameProducer,
    SnapshotRequest, StreamGroup,
};
use futures_util::{SinkExt, StreamExt};
use rtrb::PushError;
use std::{
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{
    sync::{Semaphore, mpsc},
    time::{Instant, sleep_until, timeout},
};
use tokio_tungstenite::{connect_async, tungstenite::Message};

const MAX_STREAMS_PER_SOCKET: usize = 512;
const MAX_BACKOFF_SECONDS: u64 = 30;
const MAX_DEPTH_BODY: usize = 1_048_576;
const SNAPSHOT_MIN_SPACING: Duration = Duration::from_secs(1);
const MAX_SIMULTANEOUS_SNAPSHOTS: usize = 2;

/// A bounded, per-symbol latest-request queue. Obsolete generations and
/// request IDs are replaced, not issued as extra REST calls. This is only
/// the cold REST recovery path, never a strategy/core hot-path structure.
struct SnapshotQueue {
    pending: Vec<Option<SnapshotRequest>>,
    next_symbol: usize,
}
impl SnapshotQueue {
    fn new(symbols: usize) -> Self {
        Self {
            pending: vec![None; symbols],
            next_symbol: 0,
        }
    }
    fn push(&mut self, req: SnapshotRequest) -> Result<(), MarketDataError> {
        let slot = self
            .pending
            .get_mut(usize::from(req.symbol_id))
            .ok_or(MarketDataError::InvalidResponse("unknown snapshot symbol"))?;
        if slot.is_none_or(|previous| {
            (req.generation, req.request_id) > (previous.generation, previous.request_id)
        }) {
            *slot = Some(req);
        }
        Ok(())
    }
    fn has_pending(&self) -> bool {
        self.pending.iter().any(Option::is_some)
    }
    fn pop_next(&mut self) -> Option<SnapshotRequest> {
        if self.pending.is_empty() {
            return None;
        }
        for n in 0..self.pending.len() {
            let index = (self.next_symbol + n) % self.pending.len();
            if let Some(req) = self.pending[index].take() {
                self.next_symbol = (index + 1) % self.pending.len();
                return Some(req);
            }
        }
        None
    }
}

fn receive_unix_ns() -> Result<u64, MarketDataError> {
    let n = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| MarketDataError::InvalidResponse("local clock before epoch"))?
        .as_nanos();
    u64::try_from(n).map_err(|_| MarketDataError::InvalidResponse("local time overflow"))
}
/// Use separate Public and Market routes on Binance mainnet (mandatory
/// following April 2026 migration). Testnet retains the published legacy
/// base URL; sockets are still segregated by channel type.
pub fn build_stream_url(
    table: &InstrumentSpecTable,
    group: StreamGroup,
) -> Result<String, MarketDataError> {
    let mut streams = Vec::with_capacity(
        table.instruments().len()
            * if group == StreamGroup::PublicDepth {
                1
            } else {
                3
            },
    );
    for instrument in table.instruments() {
        let lower = instrument.symbol.to_ascii_lowercase();
        match group {
            StreamGroup::PublicDepth => streams.push(format!("{lower}@depth@100ms")),
            StreamGroup::MarketRegular => {
                streams.push(format!("{lower}@aggTrade"));
                streams.push(format!("{lower}@forceOrder"));
                streams.push(format!("{lower}@markPrice@1s"));
            }
        }
    }
    if streams.is_empty() || streams.len() > MAX_STREAMS_PER_SOCKET {
        return Err(MarketDataError::InvalidConfiguration(
            "invalid stream count",
        ));
    }
    let host = match group {
        StreamGroup::PublicDepth => table.environment().ws_public(),
        StreamGroup::MarketRegular => table.environment().ws_market(),
    };
    Ok(format!("{host}/stream?streams={}", streams.join("/")))
}

async fn connect_loop(
    group: StreamGroup,
    table: InstrumentSpecTable,
    tx: mpsc::Sender<Result<LiveFrame, MarketDataError>>,
) -> Result<(), MarketDataError> {
    let url = build_stream_url(&table, group)?;
    let mut generation = 0_u64;
    let mut backoff = 1_u64;
    loop {
        generation = generation
            .checked_add(1)
            .ok_or(MarketDataError::InvalidResponse(
                "stream generation overflow",
            ))?;
        // Invalidate stale book before connection attempts and after every
        // loss. The Core 1 consumer never trades on a disconnected book.
        if group == StreamGroup::PublicDepth {
            tx.send(Ok(LiveFrame::Reset { generation }))
                .await
                .map_err(|_| MarketDataError::ChannelClosed)?;
        }
        eprintln!("market-data {group:?} connecting; generation={generation}");
        match timeout(Duration::from_secs(10), connect_async(url.as_str())).await {
            Ok(Ok((mut ws, _response))) => {
                eprintln!("market-data {group:?} websocket_connected; generation={generation}");
                backoff = 1;
                let connected_at = tokio::time::Instant::now();
                loop {
                    // Rotate before 24-hour forced Binance disconnection.
                    if connected_at.elapsed() >= Duration::from_secs(23 * 3600) {
                        break;
                    }
                    let result = timeout(Duration::from_secs(60), ws.next()).await;
                    match result {
                        Ok(Some(Ok(Message::Text(text)))) => {
                            let received_ns = receive_unix_ns()?;
                            match parse_combined(
                                text.as_str(),
                                group,
                                generation,
                                received_ns,
                                &table,
                            ) {
                                Ok(frame) => tx
                                    .send(Ok(frame))
                                    .await
                                    .map_err(|_| MarketDataError::ChannelClosed)?,
                                Err(err) => {
                                    tx.send(Err(err))
                                        .await
                                        .map_err(|_| MarketDataError::ChannelClosed)?;
                                    break;
                                }
                            }
                        }
                        Ok(Some(Ok(Message::Binary(_)))) => {
                            // Public market streams are documented as JSON
                            // text; unexpected binary is not silently ignored.
                            tx.send(Err(MarketDataError::InvalidResponse(
                                "unexpected binary frame",
                            )))
                            .await
                            .map_err(|_| MarketDataError::ChannelClosed)?;
                            break;
                        }
                        Ok(Some(Ok(Message::Ping(payload)))) => {
                            ws.send(Message::Pong(payload))
                                .await
                                .map_err(MarketDataError::WebSocket)?;
                        }
                        Ok(Some(Ok(Message::Pong(_)))) => {}
                        Ok(Some(Ok(Message::Close(_)))) | Ok(None) => {
                            eprintln!("market-data {group:?} websocket_closed; reconnecting");
                            break;
                        }
                        Ok(Some(Ok(_))) => {}
                        Ok(Some(Err(err))) => {
                            tx.send(Err(MarketDataError::WebSocket(err)))
                                .await
                                .map_err(|_| MarketDataError::ChannelClosed)?;
                            break;
                        }
                        Err(_) => {
                            // No incoming traffic even on a 1s mark stream:
                            // force reconnection, do not continue stale.
                            tx.send(Err(MarketDataError::InvalidResponse(
                                "websocket read timeout",
                            )))
                            .await
                            .map_err(|_| MarketDataError::ChannelClosed)?;
                            break;
                        }
                    }
                }
            }
            Ok(Err(err)) => {
                tx.send(Err(MarketDataError::WebSocket(err)))
                    .await
                    .map_err(|_| MarketDataError::ChannelClosed)?;
            }
            Err(_) => {
                tx.send(Err(MarketDataError::InvalidResponse(
                    "websocket connect timeout",
                )))
                .await
                .map_err(|_| MarketDataError::ChannelClosed)?;
            }
        }
        // Fail-closed reset reaches Core 1 before reconnect/backoff.
        if group == StreamGroup::PublicDepth {
            tx.send(Ok(LiveFrame::Reset { generation }))
                .await
                .map_err(|_| MarketDataError::ChannelClosed)?;
        }
        tokio::time::sleep(Duration::from_secs(backoff)).await;
        backoff = (backoff * 2).min(MAX_BACKOFF_SECONDS);
    }
}

pub async fn fetch_depth_snapshot(
    client: &reqwest::Client,
    environment: BinanceEnvironment,
    symbol: &str,
    request: SnapshotRequest,
) -> Result<LiveFrame, MarketDataError> {
    let response = client
        .get(format!("{}/fapi/v1/depth", environment.rest()))
        .query(&[("symbol", symbol), ("limit", "1000")])
        .send()
        .await?
        .error_for_status()?;
    if response
        .content_length()
        .is_some_and(|n| n > MAX_DEPTH_BODY as u64)
    {
        return Err(MarketDataError::InvalidResponse(
            "oversize snapshot response",
        ));
    }
    let body = response.bytes().await?;
    parse_depth_snapshot(&body, request)
}

pub struct LiveMarketDataEngine {
    table: InstrumentSpecTable,
    rest: reqwest::Client,
}
impl LiveMarketDataEngine {
    pub fn new(table: InstrumentSpecTable) -> Result<Self, MarketDataError> {
        Ok(Self {
            table,
            rest: rest_client()?,
        })
    }
    /// Core 0 network task. This is the only SPSC producer; all asynchronous
    /// WebSocket and REST workers merge via its bounded MPSC input queue.
    /// When the SPSC is full the function *fails closed* rather than dropping
    /// a single depth delta or pretending the local book is synchronized.
    pub async fn run(
        self,
        mut producer: MarketFrameProducer,
        mut requests: mpsc::Receiver<SnapshotRequest>,
    ) -> Result<(), MarketDataError> {
        let (tx, mut incoming) = mpsc::channel::<Result<LiveFrame, MarketDataError>>(512);
        let public = tokio::spawn(connect_loop(
            StreamGroup::PublicDepth,
            self.table.clone(),
            tx.clone(),
        ));
        let market = tokio::spawn(connect_loop(
            StreamGroup::MarketRegular,
            self.table.clone(),
            tx.clone(),
        ));
        let workers = [public, market];
        let snapshot_slots = Arc::new(Semaphore::new(MAX_SIMULTANEOUS_SNAPSHOTS));
        let mut pending_snapshots = SnapshotQueue::new(self.table.instruments().len());
        let mut next_snapshot_at = Instant::now();
        loop {
            tokio::select! {
                maybe=incoming.recv()=>{
                    let frame=maybe.ok_or(MarketDataError::ChannelClosed)?;
                    match frame {
                        Ok(frame)=>producer.push(frame)
                            .map_err(|PushError::Full(_)|MarketDataError::Backpressure)?,
                        Err(err)=>{
                            // Transport failures are retried by workers.
                            // Structural/REST/parser errors are fatal, never
                            // quietly substituted with a previous snapshot.
                            match err {
                                MarketDataError::WebSocket(e) => {
                                    eprintln!("market-data websocket transport_error={e}");
                                },
                                MarketDataError::InvalidResponse(
                                    "websocket read timeout" | "websocket connect timeout"
                                ) => {
                                    eprintln!("market-data websocket timed out; retrying");
                                },
                                other => return Err(other),
                            }
                        }
                    }
                }
                maybe=requests.recv()=>{
                    let req=maybe.ok_or(MarketDataError::ChannelClosed)?;
                    pending_snapshots.push(req)?;
                }
                _=sleep_until(next_snapshot_at), if pending_snapshots.has_pending()=>{
                    let permit = snapshot_slots.clone().try_acquire_owned();
                    if let Ok(permit) = permit {
                        let req=pending_snapshots.pop_next()
                            .ok_or(MarketDataError::InvalidResponse("empty snapshot queue"))?;
                        let symbol=self.table.by_id(req.symbol_id)
                            .ok_or(MarketDataError::InvalidResponse("unknown snapshot request"))?
                            .symbol.clone();
                        let rest=self.rest.clone();
                        let env=self.table.environment();
                        let tx2=tx.clone();
                        // No more than one new 1000-level snapshot per second,
                        // with at most two requests outstanding. This protects
                        // the shared Binance IP request-weight budget when the
                        // feed repeatedly loses sequence synchronization.
                        next_snapshot_at=Instant::now()+SNAPSHOT_MIN_SPACING;
                        tokio::spawn(async move {
                            let _slot=permit;
                            let result=fetch_depth_snapshot(&rest,env,&symbol,req).await;
                            let _=tx2.send(result).await;
                        });
                    } else {
                        // Both REST permits are in use; retry later instead of
                        // issuing an unbounded burst or losing the request.
                        next_snapshot_at=Instant::now()+SNAPSHOT_MIN_SPACING;
                    }
                }
            }
            if workers.iter().all(tokio::task::JoinHandle::is_finished) {
                return Err(MarketDataError::ChannelClosed);
            }
        }
    }
}

#[cfg(test)]
mod snapshot_queue_tests {
    use super::{SnapshotQueue, SnapshotRequest};
    fn req(symbol_id: u16, generation: u64, request_id: u64) -> SnapshotRequest {
        SnapshotRequest {
            symbol_id,
            generation,
            request_id,
        }
    }
    #[test]
    fn newest_per_symbol_supersedes_stale_snapshot_requests() {
        let mut q = SnapshotQueue::new(3);
        q.push(req(0, 1, 1)).unwrap();
        q.push(req(0, 1, 4)).unwrap();
        q.push(req(0, 1, 3)).unwrap();
        q.push(req(1, 2, 1)).unwrap();
        assert_eq!(q.pop_next(), Some(req(0, 1, 4)));
        assert_eq!(q.pop_next(), Some(req(1, 2, 1)));
        assert!(!q.has_pending());
    }
    #[test]
    fn queue_is_fair_and_rejects_unknown_symbol_ids() {
        let mut q = SnapshotQueue::new(2);
        assert!(q.push(req(2, 1, 1)).is_err());
        q.push(req(0, 1, 1)).unwrap();
        q.push(req(1, 1, 1)).unwrap();
        assert_eq!(q.pop_next(), Some(req(0, 1, 1)));
        q.push(req(0, 1, 2)).unwrap();
        assert_eq!(q.pop_next(), Some(req(1, 1, 1)));
        assert_eq!(q.pop_next(), Some(req(0, 1, 2)));
    }
}
