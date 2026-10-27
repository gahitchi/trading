# F0.6 review packet — reference PAPER foundation

**Date:** 2026-10-27. **Owner:** ChatGPT development branch
`chatgpt/f0-independent-wheel-build-20261027`.
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
   image/OS/Python package snapshot. Check exact-source GitHub OIDC signing
   of the image **identity receipt** and its workflow verification; examine
   the pinned Syft SPDX inventory and Grype vulnerability JSON of the built
   runtime image. An unresolved High/Critical finding or scanner failure
   blocks acceptance. The receipt signature is **not** an OCI image signature.
   First baseline and triage: [F0 container vulnerability audit](../audits/f0-vulnerability-baseline-2026-10-27.md).
   The initial Grype result is **failed** (3 Critical and 75 High package matches).
   Do not reinterpret a vulnerability-gated CI failure as a successful F0 review.
5. `package`: frozen runtime/build locks, source/sdist/wheel build,
   separate repeat wheel build with commit-timestamp `SOURCE_DATE_EPOCH`,
   complete committed `src/lab` package-to-wheel byte equality,
   hash-complete wheel `RECORD`, exact wheel SHA agreement, verified
   write-once `f0-candidate/source.json` and
   `f0-candidate/wheel-repeat.json` artifacts. Verify any uploaded receipts
   are tied to the reviewed SHA. They are **unsigned runner observations**.

6. `f0-two-runner-wheel`: separately provisioned **Ubuntu 22.04 and
   Ubuntu 24.04** jobs rebuild the exact same candidate from frozen
   dependencies and commit-time `SOURCE_DATE_EPOCH`. The verification job
   downloads each build, validates original source SHA and artifact SHA-256,
   and confirms identical wheels against the committed `src/lab` tree.
   The verified wheel is then attested using GitHub OIDC/Sigstore and
   checked against the exact repository, SHA, ref and signing workflow.
   A green job does not prove independence from GitHub as a trust provider
   or that the wheel is financially correct.
7. **Operator-only F0.4**: when the Arch installation and actual SPY
   dataset are available, generate `tools.foundation_inputs` receipt for
   the local `lab.toml`, `data/manifest.json` and
   `data/SPY.parquet`. The digest-only receipt stays local; do not
   upload real account/configuration or raw data to CI.

   This particular F0 receipt requires an **isolated SPY-only/ARCX daily**
   config, not the product's normal multi-ticker `lab.toml`. Supply a private
   external TOML with `--config` to keep the exact checked-out commit
   unchanged; its byte hash is verified and its content is never uploaded.
   A non-`DU` account identifier, known IBKR live default port (4001/7496),
   or explicit non-paper mode is refused. Preserve the reviewed Git commit;
   **do not** edit or commit the operator's production configuration for F0.
   Other endpoints, environment overrides, and actual broker account mode
   cannot be authenticated from configuration alone; broker evidence remains
   out of scope until F6.

For the candidate, capture CI run IDs, artifact names and SHA-256 digests in an
external immutable review record; **do not manually edit generated receipts**.

## Review the foundation in dependency order

| Step | Current engineering implementation | Required independent exit |
| --- | --- | --- |
| R0.1 | `docs/architecture/f0-reference-scope.json`: SPY/ARCX, USD, NYSE calendar, IBKR **PAPER**, no order authority | Owner approval of actual symbol, data entitlement, time horizon, venue action version and scoped strategy |
| R0.2 | Pinned Docker source/image and locks, OS/Python inventory, signed build-receipt custody, pinned Syft/Grype security scan, matching Ubuntu 22.04/24.04 wheels and verified wheel OIDC provenance | Triage scan findings, independent provider/security review, clean Arch/Windows installation, verified signed image and deployment provenance |
| R0.3 | Lint, Python 3.13/3.14, Docker, package, test harness and collaboration checks | Independent exact-SHA green CI and no pending blocking tests |
| R0.4 | Exact commit/tree source/image/wheel receipts; a local-only SHA-256 custody tool for the SPY configuration, vendor manifest and raw Parquet | Real local-data capture, point-in-time authenticity, independent source permissions/custody review |
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
future financial/economic models. The wheel build comparison now executes on two GitHub-hosted OS runners
and has GitHub OIDC-backed, workflow-verified build provenance, but it does *not*
demonstrate independence from GitHub, independent security/code review,
a signed container image, or a clean supported-OS installation. The exact
configuration, market data, broker evidence and release-candidate signatures
remain absent. `main` remains Claude-owned and is unchanged by this branch.

**No F1 qualification before accepted F0.** Isolated design or adversarial
fixtures for the upcoming exact-Decimal accounting contract may be developed
without enabling execution; they cannot be considered qualified or integrated
without passing the reviewed F0 gates.

## Bounded F0 offline-research CVE exception (owner direction, 2026-10-27)

To allow **non-deployment F0 research verification** without concealing Debian
risk, pushes to the exact ChatGPT F0 development branch may carry only the
known **55 High package/CVE/version/fix-state matches** in
`docs/security/f0-offline-cve-baseline.json` until **2026-11-26**.
The pinned Grype scan still runs and its JSON/SBOM artifacts remain available.
`tools/foundation_vulnerabilities.py` fails on any Critical, new High,
changed/high-fixable package, invalid scan or expired exception.

**This is neither an independent vulnerability-risk assessment nor F0
qualification.** PRs, `main`, every other branch, broker-connected
environments and any distribution/release retain the strict High/Critical
security gate. The research exception grants **no broker, account, strategy,
or release authorization**. The pilot must run offline without secrets,
broker sessions, untrusted inputs, privileged containers or external
service exposure. The independent reviewer may reject the exception.

Arch operator input and independent F0.6 acceptance remain required.
