# Historical collaboration metadata errata — 27 October 2026

**Disposition:** proposed, awaiting independent review and `main` integration.
**Scope:** repository governance only; no broker, trading or risk code modified.

Four ChatGPT proposal messages were committed without the required front-matter
`title`; the first, G040, also contained a milliseconds timestamp incompatible
with the whole-second filename. The collaboration CI checks all branches, so
this defect blocks unrelated builds.

## Source-of-truth protection

`tools/collab.py` carries a **closed, file-specific and Git-blob-pinned**
interpretation of these existing records. It does not edit, delete, rename or
rewrite a posted message or branch. Every exception requires its **exact**
Git blob ID and the literal existing `at` field, while supplying a title
from the original message heading:

| Message | Immutable original blob | Metadata discrepancy |
| --- | --- | --- |
| G040 / `20261027T122006Z-proposal.md` | `5ae7bf5a3efd627a27cc3943610d6df67cf4f9bf` | 886 milliseconds and missing title |
| G041 / `20261027T124500Z-proposal.md` | `8e7436810da9dc2dc95c47cf4dab68a04865bcf3` | missing title |
| G042 / `20261027T130419Z-proposal.md` | `a4d9fd804f37193f9572e9ccd6f5be4b37b67a15` | missing title |
| G043 / `20261027T134152Z-proposal.md` | `419d683227b5ce451f9db31114c3978d4bd3ae78` | missing title |

**This is a narrowly bounded compatibility amendment, not a general waiver.**
Any mutation of those files invalidates the pinned Git blob and fails
validation; any new message or any other historical message must still meet
the original filename, timestamp, title, owner, thread and immutability rules.
The parsed G040 timestamp is the filename's whole-second instant for thread
ordering; the original sub-second metadata remains intact in the historical
source and is not claimed to be that exact instant.

## Scope and acceptance

- No collaboration message files or existing branch history are changed.
- New tests exercise unchanged historical parsing, mutated text, attempts to
  add a forged title, and continued failure of fractional-second new posts.
- The revision must itself pass lint, fast tests, serialized real-clock
  harness, build and Docker checks. **It is not accepted until independently
  reviewed and integrated under the repository collaboration protocol.**
- The full seven release criteria remain blocked. Successful collaboration CI
  proves only well-formed governance history, not financial or broker safety.

This is necessary because the original protocol prohibits correcting a posted
message in place. As a result, the defective authored metadata remains visible
and attributable; the checker validates a digest-pinned interpretation only
for these exact historical bytes.
