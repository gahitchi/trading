# Capture-wide ID redaction: structured discovery before evidence sanitization

**Scope:** `lab.capture.sanitize` only. No broker, trading authorization,
order generation or statistical threshold changes.

## Trigger and evidenced failure

While validating G038, the existing integration test
`tests/test_capture.py::test_bundle_exports_the_authoritative_sqlite_ledger_and_sanitize_pseudonymizes_it`
failed on a global-substring assertion: the broker's trade ID `T2`
also appears as a **harmless substring** of any ISO timestamp containing
`T20:...`. It was not valid evidence of a privacy leak. The test now checks
whole-ID-token boundaries while preserving time data.

Independent of that false positive, the original field-only redactor
did leave **actual** identifiers in free-text messages such as
`"broker trade T2 recovered"`. The added cross-file JSON/JSONL regression
demonstrates that real, separately reproducible coverage gap.

Existing `_redact` pseudonymized values only in recognized ID fields,
map keys and account-pattern strings. It left matching execution IDs
inside free-text broker recovery messages and risk-event detail strings
untouched. Such captures must never be published without human review,
but the software should eliminate known repeat occurrences itself.

## Design

- First pass across JSON/JSONL input files finds recognized structured
  order/trade/account IDs and order-keyed dictionaries.
- Compute stable HMAC aliases using the same private key and existing
  `_pseudo` mechanism.
- Second pass recursively redacts ID fields as before and substitutes
  recognized IDs in ordinary text, including different files and JSONL.
  Only full-token matches are replaced, not substrings in timestamps
  or unrelated longer identifiers.
- One precompiled, escaped longest-first pattern handles overlapping
  IDs such as `T2` and `T23` in linear passes over the text,
  avoiding an explicit scan of every ID for every evidence field.
- Structured ID handling remains unchanged, and no raw SQLite DB or
  secret material is copied into the sanitized output.
- The output remains **review-required** with
  `manual_review_required=true`, `reviewer_attestation=null`.
  Unknown IDs and arbitrary embedded secrets in prose are not
  guaranteed clean. Short IDs of length 1 can only be masked reliably
  in structured fields (replacing every letter in free text would
  destroy the meaning of the capture).

## Regression evidence

`tests/test_capture.py` now exercises an ID found only in a **later**
file but leaked in an earlier risk message, and overlapping trade IDs
referenced in JSONL prose.

This is a new finding from the G038 CI; it should be peer-reviewed as
G039 separately. Its parent execution-accounting changes are G038;
independent review of G036/G037/G038 remains a prerequisite to merging.
