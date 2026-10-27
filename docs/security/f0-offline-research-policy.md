# F0 temporary offline-research vulnerability exception

**Expiry:** 2026-11-26. **Source scan:** commit
`c9466aeaa45f285c5a517cf97252d23bfe8ee74a`,
[GitHub Actions run 40537344968](https://github.com/gahitchi/trading/actions/runs/40537344968).

This records owner tolerance of existing OS dependency risk for an isolated
non-deployment F0 **research** workflow; it is not proof those vulnerabilities
are harmless. No owner or agent has asserted the affected code is unreachable.
The accepted set is limited to the **55 recorded High package/version/CVE
matches (14 distinct CVEs), zero Critical**. New High or Critical matches
fail closed. The exception is a time-limited engineering accommodation, not
an automatic release waiver.

## Required restrictions

- No broker sessions, account tokens, privileged containers or real-money use.
- Offline reference tests only; no exposed dashboard or internet-facing service.
- Preserve unprivileged runtime, sandbox tests, paper-only code guards and CI.
- Every candidate must rerun the pinned Syft/Grype inventory on its actual
  image and preserve the exact result; vulnerability database changes can
  revoke the exception.
- Main/PR/release CI always retains High/Critical failure semantics.
- Reassess by 2026-11-26. Independent security review and owner acceptance
  of actual operating conditions are still required for F0 formal exit.

**Not covered:** real broker paper connection, unattended or externally
deployed operations, research with untrusted code/inputs, live trading,
release readiness, image-signing verification, or independent approval.

The verifier also requires that the scanner's image ID, OS, architecture,
and ordered layer digests match the built image identity captured and
workflow-attested in `f0-container-image/image.json`. It checks that the
Grype database is recent. The scanner output is independently signed
with GitHub OIDC and verified against the exact source/workflow. This
does not replace third-party security review or signed deployed OCI images.

To validate an exact observed image scan:

```bash
python3 -m tools.foundation_vulnerabilities \
  --report /path/to/vulnerabilities.json \
  --image /path/to/image.json
```

Exit zero means only that its identified High findings remain within
the documented, narrow, expiring research-development baseline.
Output deliberately states `qualification=BLOCKED`,
`broker_authorization=false`, and `release_authorization=false`.
