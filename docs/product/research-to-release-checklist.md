# Human approval checklist — research to qualified paper release

**This is a future qualification gate, not a certificate of the current system.**
No single green item authorizes trading. Retain evidence artifact IDs, reviewer, UTC time and reason.

## Stage 1: research evidence
- [ ] Dataset and membership/corporate-action history are point-in-time with immutable hashes.
- [ ] Every exploratory candidate/failed variant is in the trial log.
- [ ] Experiment uses realistic cash, costs, spreads and fill assumptions, matched baseline and regime stress.
- [ ] Independent walk-forward evidence, multiplicity correction and confidence intervals are documented.
- [ ] Sealed holdout was accessed only under a recorded scoped authorization.
- [ ] Strategy code passed isolation/security checks; all material Critic objections resolved.
- [ ] Exact code, parameters, runtime dependencies, dataset and universe are recorded.

## Stage 2: approval for shadow
- [ ] Reviewer explicitly approves the artifact fingerprint, instruments, exposure/cash limits and expiry.
- [ ] Shadow release uses **no order-sending broker credentials**.
- [ ] Same session snapshot and decision can be reproduced from stored inputs.
- [ ] Stale bars, invalid targets, missing constituents or corrupt data cause no simulated submission.
- [ ] Daily shadow report shows all proposed orders, reasons skipped and estimated costs.

## Stage 3: supervised paper
- [ ] Broker account `DU…` and API permissions independently verified.
- [ ] Live paper inventory, cash, open orders, completed orders, fees and corporate actions reconcile against transactional ledger.
- [ ] Startup process in `RECOVERING` does not issue orders until proof passes.
- [ ] ACK timeout, partial fill, late fee, cancel race and gateway restart fault tests pass.
- [ ] All orders respect single-writer lease, per-session ID, cash reservations and risk permits.
- [ ] Kill/cancel/flatten behaviors are tested and **never overpromise execution**.
- [ ] Notification delivery, backup restoration and operator response drills pass.

## Stage 4: unattended paper release
- [ ] All documented INV/CT/UX acceptance criteria have evidence or an independently reviewed scope exclusion.
- [ ] No P0 incident is open; application-level and broker-level ready signals are distinct.
- [ ] Approved immutable binary/dependency image, strategy hash and config hash; rollback and migration tested.
- [ ] Operational runbook includes monitoring, pager, recovery, reconciliation, exchange calendar, holidays and financial data vendor.
- [ ] Shadow and supervised paper soak periods have credible representative outcomes; reconciliation reports are archived.
- [ ] Operator signs a time-limited paper-only deployment authorization with a recovery/contact plan.
- [ ] Unattended deployment starts *disabled* and only transitions to ready after authoritative startup checks.

## Change control

Change of strategy code, symbol identity, order type, exchange/market, risk policy, broker account, data class or ledger migration **revokes or requires re-review** of previous approval. An AI research cycle cannot renew a trading approval.

Use this checklist to highlight outstanding engineering work rather than manually checking boxes unsupported by test/release evidence.
