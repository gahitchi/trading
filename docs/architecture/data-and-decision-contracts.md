# Data, sessions, signals and portfolio contracts

**Owner:** Data/Decision Engine. **Status:** target design; not fully implemented.
**Invariant references:** INV-02, INV-03, INV-06, INV-10, INV-12.

## 1. Event envelope (versioned)

Every event is immutable and carries: `schema_version`, `event_id`, `correlation_id`, `deployment_id`, `producer`, `occurred_at_utc`, `received_at_utc`, `exchange_session`, `sequence`, `payload_hash`, `payload`. Use UTC instants for ordering and an **explicit exchange-calendar session ID** for identity. Do **not** infer the exchange session solely from the event timestamp (vendor daily bars can be timestamped in UTC or at start/end of interval).

Event types (separate schema versions):
- `MarketBarFinalized`: instrument ID, MIC, session, interval start/end, O/H/L/C/V, currency, adjusted/raw flags, vendor/source, ingestion checksum, quality status. Completeness must be verified by the vendor contract and exchange calendar.
- `ConstituentStatus`: instrument ID, effective-from/until, source timestamp, active/inactive and reason; preserve delisted/halted names.
- `SignalEvaluated`: strategy hash, sleeve ID, instrument, session, bar-data hashes, target, disposition (`TRADE`, `FLAT`, `NOT_APPLICABLE`, `ERROR`), compute version. An error is **not** automatically interpreted as a zero target.
- `SessionSnapshotSealed`: deterministic snapshot ID, full expected sleeve and instrument manifests, received counts, per-item hashes, data cutoff, session and content root hash.
- `PortfolioProposed`: sealed snapshot ID, full target weights for all managed instruments **including explicit zeros**, cash target, constraints version.
- `RiskDecision`: exact portfolio ID, ledger sequence, risk policy hash, expiry, permit/deny and reason; **permits are single-use**.
- `OrderIntent`: permit ID, stable client order ID, account, side, qty, order type, time-in-force, venue, estimated notional, reservation, submission expiry.
- `BrokerExecutionEvent`: broker account, client ID, broker order ID, exchange execution ID, fill or terminal status, price, qty, fee, time, originating intent.
- `ControlEvent`: kill, disable, resume, operator recovery, reconciliation, configuration activation with actor, reason and audit time.

Do not reuse human-visible `ticker` as a unique security/accounting identity: symbol changes, ticker reuse and class shares exist. Use canonical instrument ID + venue + currency + listing-effective dates.

## 2. Session lifecycle

```text
SCHEDULED -> COLLECTING -> VALIDATING -> SEALED -> PORTFOLIO_PROPOSED
                                              -> RISK_DENIED
                                              -> RISK_PERMITTED -> ORDER_PLANNED -> SUBMITTED
COLLECTING/VALIDATING -> MISSED or INVALID (non-trading terminal for this session)
```

Rules:
1. The exchange calendar determines holidays, half-days, daylight-saving offsets and expected official close. Historical replay and paper use **the same calendar adapter and session ID semantics**.
2. Data must be finalized for **this** session, not merely younger than a four-day threshold. A Monday run cannot silently use Friday's last close for one missing instrument.
3. Expected instruments and sleeves are a **point-in-time frozen roster**. A missing sleeve cannot be silently omitted. Explicitly declared `NOT_APPLICABLE` needs membership/event evidence.
4. Arrival order is irrelevant; duplicate same-ID/same-hash events are harmless, conflicting duplicates are incidents; stale events cannot replace newer sealed data.
5. After cutoff, incomplete coverage marks session `MISSED`, emits an alert and **does not allocate or trade**. Do not silently substitute yesterday, synthesize zeros, or apply partial targets.
6. Exactly one immutable snapshot/decision is emitted per session per deployment. Later corrected bars trigger an audited revision workflow; never silently mutate an already submitted decision.
7. If a strategy needs all constituents (cross-sectional), compute only **after** the validated full panel is available; emit a complete vector of results.
8. Do not assume the message bus is synchronous. A published equity event is **not** a risk permit. Risk authorization is an explicit ID-bound transaction before every order.

## 3. Signal and portfolio specifics

- Validate finite nonnegative prices and OHLC ordering; currency, lot size, minimum tick, stale volume and corporate actions; quarantine inconsistencies with evidence.
- All strategy parameters, lookback, active universe and source hashes are sealed. Strategy exceptions produce `ERROR`; operator decides whether to skip session or disable a previously approved strategy version.
- Target weights are finite long-only fractions in [0,1] unless a *different, separately authorized* risk product is designed. Gross <= configured bound; account borrowing forbidden in a cash-account deployment.
- Portfolio proposal defines all expected instruments; **replace** prior weights atomically, not `dict.update` on partial signals.
- Optimizer accounts for confirmed holdings, unavailable cash, fees, reserved outstanding orders, lot rounding, minimum notional, turnover, corporate actions and venue restrictions.
- Never assume funds from a sell are immediately available for a buy until actual broker cash/settlement permits them.
- A planned order's quantity must be fixed by the risk permit; if broker quote or account value changes outside tolerance, permit expires and must be recomputed.

## 4. Failure cases and required tests

| Scenario | Expected |
| --- | --- |
| Friday data delivered again on Monday | Monday snapshot not sealed |
| One cross-sectional symbol missing | No ranking and no orders |
| A late constituent exits | Membership event verified at effective timestamp |
| One sleeve reports same session twice with conflicting output | Quarantine and abort |
| Risk approval arrives before final updated sleeve | No order; require approval of sealed snapshot |
| Tuesday partial data mixed with Wednesday prices | No valid session snapshot |
| 13:00 NYSE half-day close | Exchange-calendar close, not hardcoded 16:00 |
| One symbol changes its ticker | Instrument ID continuity or explicit corporate-action mapping |
| Strategy returns NaN / throws exception | Error disposition, no implicit sell/buy |
| Two processes attempt the same session | Unique decision key and account lease prevent duplicate orders |

### Adoption sequence

First implement session and event schema plus deterministic calendar fixtures and unit tests; then change producer/allocator messages; then implement risk permit IDs; finally connect live execution. **Do not add a simple boolean barrier only at the allocator or execution layer:** it will not solve mixed timestamps, broker delayed bars, cross-sectional fanout or asynchronous risk approval.
