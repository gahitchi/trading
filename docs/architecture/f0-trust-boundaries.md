# F0 threat model, trust boundaries, and qualification ownership

**Scope:** one SPY/ARCX USD daily PAPER-only reference. **Status:** design
candidate; no trading or release approval. **Revision:** 2026-10-27.

## Trust boundary rules

1. **Source control is an input, not an authority.** Git commit/tree IDs
   identify bytes; a `chatgpt/*` head, green CI, or `Agent:` trailer
   cannot grant order-routing permission or release status.
2. **Builders are untrusted until checked.** Hash-locked Python graphs,
   pinned Docker/OS package definitions and source-time wheels reduce
   nondeterminism. Cross-runner equality reduces accidental drift but
   both GitHub runners share an administrative trust provider.
3. **Verifiers must fail closed.** A valid receipt is generated only for
   clean exact-source candidates. Hostile paths, symlinks, changed
   artifact identities, malformed archives, incorrect risk-policy scope,
   missing proof files and forged status flags are rejected.
4. **Machine data never enters CI automatically.** Real `lab.toml`,
   market manifests and bars remain on the operator host. Only local
   SHA-256/size digests may be recorded. Do not use the hash of a
   low-entropy secret as a published evidence artifact.
5. **No F0 result is a trading authorization.** Only F3/F4 can eventually
   establish typed risk/OMS authority and only F6 can check an actual
   authenticated PAPER broker, after F1-F5 are qualified.
6. **Signing does not mean correctness.** GitHub OIDC/Sigstore
   attestations associate a wheel digest and workflow identity with a
   build event, not security review, data correctness, strategy edge,
   accounting parity or operator consent.

## Asset, adversary and residual-risk matrix

| Boundary | Adversarial failure mode | Existing fail-closed test/control | Remaining risk / owner |
| --- | --- | --- | --- |
| Git source and collaboration | Mutable branch, forged message, misdated history | Exact HEAD/tree, immutable G040–G043 byte-pinned errata, Git ownership check, commit trailer CI | Branch protection / authentic signer review, independent owner |
| Python dependency resolution | Silent upgrade or substituted wheel | `uv.lock`, `requirements*.lock` hash requirements, frozen CI, Docker SHA base and snapshot | Upstream ecosystem compromise, provenance/CVE review |
| GitHub workflow execution | Mutable action tag, unauthorized OIDC signing | All F0 workflows pin action commits; attest job scoped to minimal writing permissions and push | Common GitHub runner/operator trust, third-party independent review |
| Python distribution assembly | Injected module/UI, omitted file, corrupt RECORD, ZIP injection or bomb | Exact committed `src/lab` parity, strict RECORD and wheel identity, ZIP member limits | Installer/host package differences, independent operator test |
| Repeat-build artifact transport | Wheel substituted in download, different source SHA or build | Separate 22.04/24.04 jobs, SHA manifests, source-commit checks and equality verifier | GitHub artifact service common trust, independent provider verification |
| Local deployment and runtime | Unexpected OS/kernel, root container, changed installed packages | Recorded source/OS Python hashes, Docker `lab` user and `dpkg` installed equality | Clean Arch/Windows installation evidence and signed installed artifact inventory |
| Market input and research config | Replaced, backdated, revised or fabricated prices; wrong venue | Private local byte identities, SPY/ARCX daily scope, missing-data refusal | **F2** validates provenance, PIT timestamps, membership, actions and entitlements |
| Economic ledger and broker | Partial-fill, short, split, cancellation, fee or cash drift | No F0 broker authorization or qualified trade dispatch | **F1–F4** exact accounting and safe OMS; **F6** actual PAPER reconciliation |
| Research and released runtime | Optimistic simulated edge, self-approved candidate | F0 reports `BLOCKED`, seven independent release gates stay blocked | **F5** independent statistical evidence; **F8** independent final approval |

## F0.6 acceptance responsibilities

The **author** produces a complete exact-SHA candidate and makes failures
reproducible. An **independent reviewer** validates both workflows against
the *same* commit, inspects source, obtains and checks signed wheel
provenance, audits residual risks and signs the acceptance decision.
The **operator** separately verifies the installed Arch/Windows baseline
and retains local private configuration/data receipts. These are
different roles; neither test success nor the author’s review can
substitute for independent approval.

Minimum reviewer challenges: modify any pinned SHA, alter or insert a
wheel member, forge a RECORD hash or second-builder SHA, change an
immutable collaboration blob, switch config to LIVE/intraday, replace
Parquet data, strip evidence, or claim release authorization. Each
must produce refusal and must not enable trading.

F0 can be labelled `QUALIFIED` only when the actual review has passed,
the candidate is frozen at an immutable revision, real deployment/data
identities are available under appropriate custody, and all blocking
findings are closed. Until then F1 coding may be explored in isolation
but no F1 engine/accounting behavior is claimed to be qualified.
