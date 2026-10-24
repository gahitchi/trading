# Working rules for AI agents on this repository

Read by every agent (Claude reads it through `CLAUDE.md`). `COLLABORATION.md` is the protocol between the
agents; this file is how each one works. Gabriel's decisions in `collab/decisions.md` override both.

## How to change code

1. **Think before writing.** State the assumptions a change rests on. If a request can be read two ways and
   the readings lead to different code, ask (or, in a turn nobody is watching, take the safer reading and
   say which). Name the trade-off when you pick one.
2. **Simplest thing that meets the goal.** No speculative options, layers or "for later" hooks. A feature
   nobody asked for is a finding, not a gift. Prefer the standard library and existing helpers to a new
   dependency; a new dependency has to justify its weight in a hash-locked Docker build.
3. **Surgical changes.** Touch only what the goal needs. Match the surrounding style. Don't reformat,
   rename or refactor unrelated code in the same change; propose it separately. Every changed line should
   trace back to the request or to a finding.
4. **Verify against the goal, not against effort.** Before writing, say what will prove the change works.
   Reproduce a bug before fixing it, keep the reproduction as a test, and show it failing on the old code.
   "Tests pass" is not the goal; the behaviour the user asked for is. Run what you claim: lint, format,
   the relevant tests, and the full suite before handing off.
5. **Say what you didn't do.** Open items, untested paths and assumptions go in the commit message or the
   hand-off, in plain words. Missing evidence is "blocked", never "passed".

## Rules of this project

- **Paper accounts only.** Never weaken the three paper-only guards (live ports, `DU` account IDs, the
  account check before every order), and never add a code path that builds a live configuration.
- **Branches:** Claude writes to `main` and `claude/*`; ChatGPT to `chatgpt/*` and `hardening/*`. Work in
  turns; Gabriel starts each turn. Every commit, merges included, carries exactly one `Agent:` trailer.
- **Never commit secrets or raw broker evidence:** `.env`, `secrets/`, account numbers, tokens, IBKR
  captures (only `lab.capture.sanitize` output belongs in Git).
- **No look-ahead, ever.** A signal sees only closed bars; research respects the train/test/holdout split
  and the intraday holdout boundary; every trial is counted.
- **Before handing off:** `ruff check src tests tools`, `ruff format --check src tests tools`,
  `pytest`, `python3 tools/collab.py check`, then a message in the thread with what changed, what was
  verified, and what is still open.
