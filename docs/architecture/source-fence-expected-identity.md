# Source-fence identity binding: independent operator-pinned namespace

**Status:** offline-only hardening. No real broker-authentication or release permissions.

Prior `SourceFence` frames committed venue, environment and source epoch into
public SHA-256 chain material but the verifier did not compare these fields
to an **independently configured expected identity**. A reporter could construct
a self-consistent replacement with a new venue or epoch and still obtain zero
findings. This is a contract defect even in the synthetic qualification layer.

`ExpectedSourceIdentity` introduces an explicit non-default expectation for:
- account scope, broker source identity and venue;
- declared source environment (offline fixture, paper or live);
- instrument and corporate-action version;
- source-reconnect/session epoch.

`assess_fenced_report(..., expected_identity=...)` now requires that value.
It compares pinned fields with both `OfflineOrderSession` scope and the
reported fence. Swapping a claimed source or session epoch, even with a
perfectly recomputed SHA chain or an empty event list, produces
`FENCE_PINNED_SOURCE_IDENTITY_MISMATCH`. A different local scope produces
`PINNED_IDENTITY_LOCAL_SCOPE_MISMATCH`; a different reported source ID
produces `SNAPSHOT_PINNED_SOURCE_IDENTITY_MISMATCH`.

The test also checks monotonically increasing **merged delivery availability**
across all three channels; per-channel monotonicity alone permits an
out-of-order interleaved ingestion stream. This validation applies to the
ordered fused collector stream; an adapter combining independent channels
must preserve accurate collector receipt order and must not invent a
chronology from exchange timestamps.

The expected identity is an **operator assertion**, not an authenticated
counterparty. This code **must not** be described as broker-native attestation
or source-sequence finality. The `paper` and `live` environments remain
explicitly unsupported; the result's authorization and reservation-release
properties remain read-only `False`.

The new adversarial test cases enforce cross-venue / changed epoch even when
an attacker rehashes frames, source mismatch, independent local scope
binding, global merged-delivery regression and non-overrideable authority.
Total expected device tests: **124** (previous baseline 120 plus 4),
pending actual Termux execution and GitHub CI on the final SHA.

Run:

```sh
cd ~/trading-portable-check
git fetch origin
git switch chatgpt/causal-fence-identity-20261026
git pull --ff-only
PYTHONPATH=src python -m lab.mobile check
python tools/collab.py check
git rev-parse HEAD
```

All execution F3/F4, authenticated brokerage, F1 equity settlement parity,
F6 supervised external paper deployment and F8 production gates remain
**BLOCKED**.
