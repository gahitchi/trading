# Working rules for AI agents on this repository

Read by every agent (Claude reads it through `CLAUDE.md`). Gabriel's instructions override it.

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

- **Paper only.** Never add a code path that sends real exchange orders or uses exchange API keys without
  Gabriel's explicit decision. Never weaken the kill switch or the pre-trade risk manager.
- **Branches:** work on `claude/*` branches and land changes on `main` through pull requests.
- **Hands off other people's branches.** Never commit to, rebase, reset or delete `chatgpt/nicola` or any branch
  owned by the collaborator ssirto (`chatgpt/gold-*`). Only Gabriel decides what happens to them.
- **Never commit secrets:** `.env`, API keys, tokens, account numbers.
- **No look-ahead, ever.** Research sees only data before the forward holdout; every configuration tried is
  counted in the trial log.
- **Before handing off:** `ruff check src tests`, `ruff format --check src tests`, `pytest -n auto`, then a
  message with what changed, what was verified, and what is still open.
