# F0.6 review packet — reference PAPER foundation

**Date:** 2026-10-27. **Owner:** ChatGPT development branch
`chatgpt/f0-wheel-reproducibility-20261027`.
**Disposition:** **CANDIDATE — independently unreviewed**.
No external reviewer acceptance, broker qualification, live-money authority,
strategy approval, release signing or F0 qualification is asserted.

## Exact candidate evidence protocol

Review **one exact 40-character commit SHA**, not the moving branch tip.
For the chosen SHA, use its GitHub CI run to verify the following:

1. `collab`: signed commit ownership and immutable historical metadata;
   any legacy message exception must refer to an exact unchanged Git blob.
2. `lint`: Ruff lint and formatting on the **identical SHA**.
3. `test (3.13)` and `test (3.14)`: all fast tests, real-clock harness and
   offline real-price example on the **identical SHA**.
4. `docker`: unprivileged image smoke and read-only, content-addressed
   image/OS/Python package snapshot.
5. `package`: frozen runtime/build locks, source/sdist/wheel build,
   separate repeat wheel build with commit-timestamp `SOURCE_DATE_EPOCH`,
   complete committed `src/lab` package-to-wheel byte equality,
   hash-complete wheel `RECORD`, exact wheel SHA agreement, verified
   write-once `f0-candidate/source.json` and
   `f0-candidate/wheel-repeat.json` artifacts. Verify any uploaded receipts
   are tied to the reviewed SHA. They are **unsigned runner observations**.

For the candidate, capture CI run IDs, artifact names and SHA-256 digests in an
external immutable review record; **do not manually edit generated receipts**.

## Review the foundation in dependency order

| Step | Current engineering implementation | Required independent exit |
| --- | --- | --- |
| R0.1 | `docs/architecture/f0-reference-scope.json`: SPY/ARCX, USD, NYSE calendar, IBKR **PAPER**, no order authority | Owner approval of actual symbol, data entitlement, time horizon, venue action version and scoped strategy |
| R0.2 | Pinned Docker source/image, dependency locks, OS/Python runtime inventory; repeat-built wheel/source parity | Independent builder, clean Arch and Windows deployment provenance, signed/attested build provenance |
| R0.3 | Lint, Python 3.13/3.14, Docker, package, test harness and collaboration checks | Independent exact-SHA green CI and no pending blocking tests |
| R0.4 | Exact clean Git commit/tree, pinned input digests, immutable source/image/wheel records | Actual data/config/account provenance with privacy-safe custody and independent verifier |
| R0.5 | `docs/architecture/f0-contract-map.json` complete F0–F8 gate ownership and order validator | Design and failure-mode review; resolve any rejected ownership and duplicate authority |
| R0.6 | Structured review packet and regression inventory prepared | **Independent reviewer/owner sign-off** on exact SHA; unresolved critical findings remain blocking |

## Reviewer challenge cases

- Dirty or untracked checkout, switched Git SHA, mismatched lock graph or
  F0 map, forged live mode, modified candidate receipt, symlink or out-of-tree
  evidence violations must be refused.
- Docker container under root, unpinned dependency claims, malicious image
  metadata, OS package list discrepancy and foreign runtime observations must
  be refused.
- Wheel with changed `lab` source byte, omitted file, injected dist-info
  payload, duplicate ZIP members, forged `RECORD` digest, different second
  wheel, unsupported metadata or excessive expanded size must be refused.
- Newly malformed collaboration files must fail, despite the *four* immutable
  metadata errata; mutation of any grandfathered blob must fail.
- Documentation or verifier success must never set
  `qualification=QUALIFIED`, a broker authorization flag or any of the seven
  independent release gates.

## Scope, limits and disposition

The F0 evidence is about **repeatability and integrity**, not the soundness of
future financial/economic models. In particular, two local wheel builds on one
GitHub runner do *not* demonstrate independent reproducibility, signed
publication, a trusted build runner or a clean OS install. The exact
configuration, market data, broker evidence and release-candidate signatures
remain absent. `main` remains Claude-owned and is unchanged by this branch.

**No F1 qualification before accepted F0.** Isolated design or adversarial
fixtures for the upcoming exact-Decimal accounting contract may be developed
without enabling execution; they cannot be considered qualified or integrated
without passing the reviewed F0 gates.
