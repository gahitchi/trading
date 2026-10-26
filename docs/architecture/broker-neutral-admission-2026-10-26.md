# Broker-neutral venue selection and supervised paper-admission evidence

**2026-10-26 decision: retain IBKR as a candidate, not an architectural
dependency; add an evidence-driven acceptance path for alternative venues.**

Trading Lab must not claim institutional grade because it imports an adapter,
offers a paper login, or has a modern trading API. The relevant outcome is
broker-supported market/instrument semantics, auditable fills and costs, current
risk and account reports, reproducible data, and conservative failure recovery.

## Venue candidates and limits

| Candidate | Specific attraction | Blocking question before selection |
|---|---|---|
| IBKR | Broad equities, FX, commodities, futures and exchange support; established paper/TWS integration | Actual paper account order/execution/cash reporting, time/boundary limits, session conflicts, market-data permissions and restart correctness |
| Alpaca | REST/WebSocket/SSE order lifecycle and separate paper API; US equity and crypto workflows | Eligibility for the actual Italian retail Trading API account (EEA passporting primarily describes enterprise/B2B services), asset restrictions, availability of whole-market paid SIP rather than free IEX-only data |
| OKX EEA | EEA-specific demo API, public/private websocket and REST; crypto spot/eligible derivatives | Country/contract permissions, supported demo order and account events, exchange-specific funding/margin, actual server-backed reconnect and sequence semantics |
| OANDA | v20 transaction-ID-based account changes would be strong for FX | European customers are served by OANDA TMS; OANDA's v20 API introduction explicitly excludes OANDA TMS BROKERS S.A. Do not assume Italian v20 availability |

Primary sources, retrieved 2026-10-26:
- https://www.interactivebrokers.com/docs/tws-api/doc/notes-limitations/tws-api-limitations
- https://www.interactivebrokers.com/docs/web-api/authentication/sessions
- https://docs.alpaca.markets/us/docs/alpaca-api-platform
- https://alpaca.markets/blog/alpaca-completes-eea-passporting-to-29-countries-expanding-access-to-regulated-investment-services-across-europe/
- https://alpaca.markets/support/countries-alpaca-is-available
- https://alpaca.markets/support/data-provider-alpaca
- https://my.okx.com/docs-v5/
- https://developer.oanda.com/rest-live-v20/introduction/
- https://www.oanda.com/group/press-release/new-approach-to-operations-in-europe/

## Implementation introduced by this branch

The stdlib-only `lab.broker_trial` module defines:
- A **pinned** paper-only candidate namespace for account, venue, contract
  class/kind, action version, stream epoch, and instrument.
- An asset-appropriate list of **required provider observations**.
- Immutable, SHA-256-identified artifact *claims* for each check.
- A non-authorizing coverage assessment that rejects missing, conflicted,
  stale, future-dated, duplicated, and wrong-account/epoch evidence.
- Explicitly unimplemented external authority: the assessment's
  `authenticated_broker`, `release_reservations`, and `authorize_orders`
  are all hard-coded `False`.

**Security boundary:** a locally created SHA-256 is not an authentication
signature. A complete checklist can be filled with fabricated objects.
The `claimed_coverage_complete` output means only that the untrusted local
checklist has no missing fields. It is **not a pass**, a venue endorsement,
a trustworthy broker observation, or permission to submit an order.
No connection, secrets, order transport or reservation mutation is added.

The acceptance protocol in docs/paper-acceptance.md, including B01–B08 and
independent operator/broker evidence, still governs any real paper session.
The account, venue and instrument must pass independent end-to-end evidence
tests before *supervised* paper routing can even be proposed. Any missing
coverage, foreign order, unknown cancellation or market-data entitlement
must fail closed.

## Further architecture priorities

1. Keep research data providers separate from broker execution providers.
   Historical data provenance, corporate actions, book fidelity and point-in-
   time availability are not validated by a broker's paper endpoint.
2. Select a *primary equities* paper execution venue on observed B01–B08
   results. Keep alternatives able to satisfy the same independent reducer,
   immutable evidence and cash/positions reconciler.
3. For crypto, continue OKX EEA demo qualification in its own instrument
   family rather than pretending the securities accounting and derivatives
   margin/funding models are interchangeable.
4. Require a production-grade per-venue adapter to implement order identity,
   replay-aware event synchronization, reconnect recovery and full actual fee/
   financing accounting. Do not fabricate globally monotonic provider cursors.
5. Retain real-money execution as disallowed while these review and
   independent acceptance gates remain unresolved.

The current project pinned NautilusTrader 2.0.0rc7. It is a pre-release
engine and official installation guidance discourages pre-release wheels
for production trading with real capital. An engine/framework change remains
open if its actual semantics fail testing, just like a broker change.

Acceptance tests for this branch:
`PYTHONPATH=src python -m pytest -q tests/test_broker_trial.py`;
`ruff check src tests tools`; `ruff format --check src tests tools`;
`python tools/collab.py check`.
