# F0 acceptance slice 01 — clean source and dependency inventory

**Status:** development candidate, not qualified. Scope: F0.1–F0.4 only. No order dispatch, broker login, new strategies or release-gate changes.

## Reference vertical slice

The machine-readable scope is [f0-reference-scope.json](f0-reference-scope.json).
For foundational reproducibility tests it designates one USD-listed equity/ETF
(**SPY / ARCX**, NYSE calendar, daily-only), an **IBKR PAPER** context and
**no execution authority**. This is a *provisional internal test reference*,
not an assertion of IBKR market-data entitlement, actual account availability,
qualified corporate actions or permission to place an order. Gold, forex,
crypto, other brokers, unrestricted autonomy and real-money routing are
excluded from this reference candidate; broader product development is not
cancelled or treated as qualified by this choice.

## Why this code exists

The existing tools already provide simulation/runtime source digests
(`src/lab/provenance.py`), locked Python graphs, pinned Docker parent,
hosted CI and a strict **seven-gate release checker**
(`tools/release_check.py`). None is a standalone **write-once, exact-source
receipt** for the F0 scoped milestone. This new facility adds only that
missing narrow function; it does not create a second release verifier.

`tools/foundation_manifest.py` is standard-library-only and requires:

1. The caller supplies the **Git checkout root**, without any uncommitted
   tracked or untracked changes.
2. Git HEAD commit and tree identities are available.
3. All fixed source/lock/CI/OS-build-definition inputs are regular files,
   not symlinks, and SHA-256 hashes of their working bytes match the exact
   bytes at `HEAD`; a second cleanliness check catches changes during read.
4. `f0-reference-scope.json` is present and explicitly **PAPER_ONLY** with
   **NONE_IN_F0** authority, so the source inventory cannot be generated
   for a mislabeled live or trading-enabled reference slice.
5. Output is created **outside the Git checkout** with exclusive creation
   (no silent overwrite). The verifier re-observes all inputs and rejects a
   changed SHA, dependency lock, scope, OS/kernel/Python tuple or invented
   release authorization. The receipt is **not a signature**.

From a clean checkout, use a private folder outside the repository:

```sh
mkdir -p "$HOME/trading-evidence"
python3 tools/foundation_manifest.py create \
  --out "$HOME/trading-evidence/f0-candidate.json"
python3 tools/foundation_manifest.py verify \
  --input "$HOME/trading-evidence/f0-candidate.json"
```

Both successful commands deliberately report `qualification=BLOCKED`,
`release_authorization=false`. Exit zero means **source/environment inventory
integrity only**, never that any trading gate passed. A second `create` on
the same path refuses; choose a different filename for a new commit.

CI's `package` job creates and verifies a fresh inventory and uploads it
along with the built wheel's SHA-256 for later review. These are *ephemeral
unsigned hosted-run observations* until reviewer evidence and a genuinely
immutable release artifact are established. Windows/Arch/Termux observations
are different environments: verifying a receipt from another OS/kernel
intentionally fails rather than silently claiming the same deployment.

## F0.2: Docker image and installed-dependency observation

**Development implementation:** `tools/foundation_image.py` and
`tests/test_foundation_image.py`. This extends the exact-commit source receipt
without granting it any trading or release authority.

The hosted Docker CI job records a tightly filtered, source-commit-bound
container observation:
- Docker's local content-addressed **image ID**, platform, unprivileged `lab`
  user and root filesystem layer digests. This is a **local image ID**, not
  a registry-published or signed manifest digest.
- A **byte equality check** that `dpkg-query -W` in the running, isolated
  image matches the OS package list written during the pinned Docker build.
- SHA-256 of that complete Debian package list, and SHA-256 of sorted installed
  Python package name/version pairs, with explicit NautilusTrader and
  Trading Lab package versions.
- SHA-256 of the independently reconstructed F0 source-inventory record
  linking those observations to an exact source commit, tree, lock graph
  and builder environment.

The observation is verified immediately and uploaded as a **separate unsigned
CI artifact**; its creation and verification run without any IBKR session,
credentials or external network inside the container. Forged identifiers,
root-user containers, missing layers, changed dependencies, incompatible
release versions, dirty checkouts, mutated receipts, and wrong release
authorization all fail closed in synthetic fixture tests.

**The Docker CI job also scans the **built image** with exact-pinned
Syft v1.55.0 and Grype v0.121.0, retaining an SPDX JSON inventory and
vulnerability findings. Unresolved known **High/Critical** findings
fail the scan job; medium/low findings still require reviewer assessment.
A signed GitHub OIDC attestation binds the *container identity receipt*
to the exact CI source/workflow, and `gh attestation verify` checks it.
This is **not a signed OCI image or registry-published digest**, and a
green scan is not evidence that the container is free of vulnerabilities
the vulnerability database does not yet know about. Scanner failures
cannot be treated as passing F0 acceptance.

The snapshot does not prove reproducible binaries.** The image could have
been built by a malicious or compromised CI runner, contain transitive
software that is not covered by the lock, or vary between independent builds.
A deterministic two-builder comparison, signed registry digest and
provenance/SBOM verification, independently reviewed OS package and CVE
assessment, and clean Arch/Windows installer observations remain open.
Do not use this receipt as a deployment token, paper account authorization,
market-data source proof, or R6/F8 release criterion.

## F0.2: source-to-wheel parity and repeated local builds

The `tools/foundation_wheel.py` verifier checks that the **complete tracked
`src/lab` package tree** appears inside the wheel with byte-identical content
(including source modules, UI assets, icons, fonts and defaults). It rejects
unexpected executable/data payloads, duplicate or unsafe ZIP members,
out-of-bounds expanded archive size, mismatched Python distribution identity,
invalid `WHEEL` metadata, and any missing, malformed or hash-inconsistent
`RECORD` rows.

The package CI job builds the wheel once from the sdist and again directly
from the source tree in **separate directories**, with
`SOURCE_DATE_EPOCH` pinned to the exact candidate commit timestamp. It
requires the two resulting wheel files to be **byte-for-byte identical** and
stores an exclusive-write, SHA-256/source-commit-bound receipt. Rebuilding and
verifying on a modified source tree invalidates that receipt.

This tests *local repeatability on one GitHub runner*. It is **not independent
builder reproduction**, signed package provenance, a comparison between the
actual Arch/Windows-installed code and the wheel, or a supply-chain audit.
The receipt always states `qualification=BLOCKED`. No trading authority can
derive from these checks.

## F0.2 cross-runner wheel comparison (GitHub-hosted Ubuntu 22.04 and 24.04)

The `.github/workflows/f0-two-runner-wheel.yml` workflow rebuilds the
wheel on **two separately provisioned GitHub-hosted runners**, using
Python 3.13, frozen build dependencies and the exact Git commit time as
`SOURCE_DATE_EPOCH`. Each runner uploads its wheel and a relocatable
SHA-256 manifest. A separate verification job checks both source commit
IDs, verifies each wheel against its original manifest, and reuses
`tools/foundation_wheel.py` to prove byte-for-byte equality and committed
source-to-package parity. The validator writes a third unsigned receipt.

This is a repeatability check across two operating system images, **not**
a comparison across independently controlled trust providers. GitHub,
its ephemeral runners, uploaded artifacts and Actions supply chain
remain common dependencies.

On public push candidates, the verified wheel is signed through
`actions/attest` using GitHub OIDC/Sigstore artifact provenance,
and the workflow calls `gh attestation verify` to require the exact
repository, source revision, branch ref, signing workflow and hosted runner.
The signature attests **wheel digest and GitHub workflow origin**, not
whether the software, market data, financial models or deployment is safe.
The signed wheel does not imply a signed Docker image or a reviewed release.

## F0.4 private local runtime input custody (operator only)

`tools/foundation_inputs.py` hashes the three specific local inputs
necessary to reproduce the provisional SPY / ARCX daily research context:
the root `lab.toml`, `data/manifest.json` and `data/SPY.parquet`.
It requires a clean exact-source checkout, the configured SPY/ARCX
daily reference, a manifest entry for SPY and Parquet magic/header
markers. It records **only SHA-256 and byte lengths by neutral role**.
It does not expose raw data, vendor metadata, broker account IDs,
configuration values or credentials, and it is **never uploaded in CI**.

The normal tracked `lab.toml` is intentionally a multi-instrument product
configuration, not this isolated F0 reference. To verify the **same exact CI
commit without editing any tracked source file**, save an explicit, private
SPY-only TOML outside the repository (for example `$HOME/trading-evidence/f0-pilot.toml`).
The verifier reads it read-only with `--config`, validates that it contains
**only** SPY/ARCX, daily speed and a non-live paper context, and records
only its byte hash and size. It never uploads the configuration or turns it
into an order-routing authority. The default without `--config` still
checks the root `lab.toml` and **will reject the normal multi-ticker config**.

On an operator machine with genuine local datasets present, run:

```sh
cd /path/to/clean/trading-checkout
mkdir -p "$HOME/trading-evidence"
python3 -m tools.foundation_inputs --config "$HOME/trading-evidence/f0-pilot.toml" capture \
  --out "$HOME/trading-evidence/f0-inputs-$(git rev-parse --short HEAD).json"
python3 -m tools.foundation_inputs --config "$HOME/trading-evidence/f0-pilot.toml" verify \
  --input "$HOME/trading-evidence/f0-inputs-$(git rev-parse --short HEAD).json"
```

These commands are read-only on the source and input data, refuse
replacing an existing receipt, and never submit orders. Keep the receipt
private. Even a matching manifest and file hash **does not prove point-in-time
source authenticity, complete OHLCV bars, valid corporate actions or market
data entitlement**. Those require independent F2 and actual broker/source
evidence. An absent market-data file fails rather than inventing fixtures.

## Explicit F0 gaps and next submilestones

- **F0.1:** final owner/operator decision on the pilot symbol, instrument
  action version, source entitlement, data horizon and frozen strategy policy.
  Reference scope alone does not certify them.
- **F0.2:** source-linked Docker image/OS/Python inventory, local repeat
  builds and two separate GitHub-hosted Ubuntu runner builds are checked
  against the exact source. Independent-provider provenance, signed image
  and wheel attestations, and Arch/Windows installer verification remain open.
- **F0.3:** CI already runs dual Python, lint, packaging, Docker and serialized
  real-clock harness. We must still make all collaboration checks green on
  the *exact final candidate*. A separate older G040 message has malformed
  timestamp metadata on another branch; the original message is immutable
  under COLLABORATION.md. No validator relaxation, editing or deletion is
  authorized; this governance issue remains an explicit blocker.
- **F0.4:** private local byte custody for `lab.toml`, the SPY source
  manifest and Parquet data is implemented. The source receipt does not
  disclose private values, and actual validated/authenticated market
  source, permissions and broker evidence remain external blockers.
- **F0.5–F0.6:** review architecture decisions and resolve all independent
  blocking findings; freeze a reviewed immutable, signed or otherwise
  externally attested *release* candidate and bind evidence to its exact SHA.

**Exit condition:** no F0 completion claim until exact code, build, OS,
configuration/data identities and independent review are demonstrably tied
to one candidate. F1 economic conservation and F2 data integrity come
**after** this reviewed foundation; the tool here authorizes no execution.
