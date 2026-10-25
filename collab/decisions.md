# Decisions

Outcomes recorded when both agents posted `agree`, or Gabriel posted a `decision`. Newest last.
Format: `- <date> · thread <id> · <decision> · <who agreed>`

- 2026-10-22 · — · Each agent writes only to its own branches (Claude: `main`, `claude/*`; ChatGPT:
  `chatgpt/*`, `hardening/*`); both review each other's work ruthlessly; Gabriel decides disagreements
  (COLLABORATION.md) · set by Gabriel
- 2026-10-22 · G002 · Short selling and leverage are in scope, scaled by risk profile ("the bots can use leverage
  and shorts much more aggressively"); the bots should be built like highly skilled, experienced traders using
  advanced techniques. Paper accounts only, unchanged · set by Gabriel (in chat, relayed by Claude)
- 2026-10-22 · G001 · Merge-signature check by branch containment, no merge-parent fallback; metrics fail closed on incomplete curves and need 3 marks (main `124f803`) · claude, chatgpt
- 2026-10-22 · G003 · Unattended, continuous, uninterrupted trading is the base (default) mode; manual and
  supervised modes are options the operator can choose. Paper accounts only, unchanged · set by Gabriel (in chat,
  relayed by Claude)
- 2026-10-22 · C002 · Work in turns, not in parallel: while one agent builds, the other doesn't change code; when a
  turn ends, the other reviews it and takes its turn. Gabriel starts each turn · set by Gabriel (in chat, relayed by
  Claude)
- 2026-10-23 · C003 · The risk profile drives shorts and leverage; absolute ceilings default to the broker's
  maximum; default profile `balanced`. Trading speeds: daily, 1h, 15m, 5m; quick speeds only for the dynamic and
  aggressive profiles. Market data delayed until Gabriel subscribes to real-time · set by Gabriel (in chat,
  relayed by Claude)
- 2026-10-24 · G013 · Autonomous paper trading must not stop by itself: the default autonomous mandate is
  standing (no expiry); a time limit is opt-in, supervised and manual mandates stay leases. Deployment: a
  laptop/desktop for the testing phase, then a cloud server for real unattended operation · set by Gabriel
  (in chat, relayed by Claude)
- 2026-10-24 · C006 · An external audit by Google Gemini (`docs/audits/2026-10-24-quantitative-research-upgrade-audit.md`,
  F-R01 to F-R19) is filed on main; Claude fixes every finding; both agents check it at the start of each turn.
  Installation must be quick and close to plug and play · set by Gabriel (in chat, relayed by Claude)
- 2026-10-24 · — · Neither Claude nor ChatGPT touches the branch `chatgpt/nicola` (no commits, pushes, merges,
  rebases, resets, deletion, cherry-picks, reviews, or work built on it) · set by Gabriel (in chat, relayed by
  Claude)
