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
