# Collaboration protocol

Two AI agents build this product for its owner by reviewing each other's work, hard, and converging on what
survives the review. This file is the contract between them.

| Who | Owns | Role |
|---|---|---|
| **Gabriel** | everything | Owner. Decides disagreements. Starts each agent's turn. |
| **Claude** | `main`, `claude/*` | Builds on `main`. Reviews ChatGPT's work. Integrates what survives review. |
| **ChatGPT** | `chatgpt/*`, `hardening/*` | Builds on its branches. Reviews Claude's work. Proposes what should reach `main`. |

## 1. The one rule: each agent writes only to its own branches

- An agent never pushes to, rebases, or rewrites a branch it doesn't own, and never edits files on it.
- **`chatgpt/nicola` is off limits to both agents** (Gabriel, 2026-10-24): no commits, pushes, merges,
  rebases, resets, deletion, cherry-picks or reviews, and nothing built on it, although its name is under
  `chatgpt/*`.
- Reading everything is encouraged: `git fetch origin` and review the other agent's branches and commits.
- Every commit, merges included, carries exactly one trailer naming its author: `Agent: claude`,
  `Agent: chatgpt` or `Agent: gabriel`. CI rejects a new commit without one, with two, or with an unknown name,
  and a commit signed by an agent that doesn't own the branch unless it arrived by merging that agent's branch.
  (A trailer is attribution, not a cryptographic signature.)
- Work moves between branches only by **integration**: the owner of the receiving branch merges or
  cherry-picks it, credits the author, and says so in the conversation. ChatGPT may merge `main` into its
  branches at any time to stay current.
- A **merge** keeps the original signatures (CI accepts a foreign-signed commit when a branch its signer owns
  contains it). A **cherry-pick or re-implementation** is a new commit by the integrator: sign it with your own
  `Agent:` trailer and credit the source with `Integrated-from: <agent> <sha>`.

## 2. The channel: messages in each agent's own outbox

Each message is one file, committed on the author's own branch:

```
collab/outbox/<agent>/<thread>/<YYYYMMDDTHHMMSSZ>-<kind>.md    (+ attachments such as evidence files)
```

`tools/collab.py` (standard library only) reads the outboxes of all branches, each only for the agent that owns
that branch, and shows them as one conversation per thread:

```bash
git fetch origin
python3 tools/collab.py inbox chatgpt                 # what is waiting for me?
python3 tools/collab.py show C001                     # full conversation, both branches merged
python3 tools/collab.py post C001 --agent chatgpt --kind reply --body-file reply.md
python3 tools/collab.py new --agent chatgpt --kind proposal --title "..." --body-file m.md
python3 tools/collab.py check                         # before every commit
```

- Thread IDs carry their opener: `C001` (Claude), `G001` (ChatGPT), `H001` (Gabriel).
- Kinds: `proposal`, `review`, `question` (these open threads), `reply`, `agree`, `object`, `escalate`
  (to Gabriel), `close`, and `decision` (Gabriel).
- Messages are **immutable**: never edit or delete a posted file; correct yourself in a new message. CI checks.
- `check` fails on malformed messages in the branch being checked. A malformed message on another branch is
  only a warning there, since nobody but its author can supersede it; fix it on your own branch.
- The tool refuses to write as an agent on a branch that agent doesn't own.
- Gabriel can write on any branch with `--agent gabriel`.

## 3. Reviews: ruthless, specific, provable

Both agents review all of the other's work, not just what is offered for review. Judge it as a demanding
senior reviewer would: correctness first, then whether it should exist at all, then quality. Be blunt, and
make every claim checkable.

```markdown
- **F3 [blocking]** The critic gate rejects every hypothesis ... Evidence: `command, test, file:line or measurement`
- **F4 [advisory]** Consider ...
- **F5 [question]** Is ... intended?
```

- `blocking`: wrong results, unsafe behaviour, broken workflows, or work that should not land. Must carry
  evidence another agent can reproduce. Without evidence, it is `advisory`.
- `advisory`: design, quality, scope, taste.
- Finding IDs run in order across the whole thread (F1, F2, ... whoever writes them).
- Every blocking finding gets an answer by ID: `F3: accept`, `F3: fixed in <sha>`, or
  `F3: dispute: <reason and counter-evidence>`. Silence is not acceptance.
- Credit what is good, in one line. Praise is not a substitute for findings.

## 4. How work reaches `main`

1. ChatGPT proposes: a `proposal` thread with the branch, commit SHA, what and why, and the output of
   `pytest -q` and `ruff check src tests tools`.
2. Claude reviews it there. Anything with an open blocking finding stays out.
3. Claude integrates the rest (merge or cherry-pick, crediting ChatGPT), and posts what landed and what didn't.
4. Claude's own changes are reviewed by ChatGPT the same way. Changes to safety-critical code
   (`src/lab/agents/`, `safety.py`, `paper.py`, `backtest.py`, `experiments.py`, `stats.py`, `data.py`
   validation, `research/` gates and sandbox, deployment files) are announced in a thread with the commit,
   and Claude doesn't build further on them until ChatGPT has replied or Gabriel waives the review.

`main` must always pass CI: lint, format, tests, the offline example, the Docker build, and `collab.py check`.

## 5. Agreement and disagreement

- A thread is **agreed** when the last message of each agent is `agree`; Claude records it in
  `collab/decisions.md`.
- After **two rounds** without agreement on a finding, either agent posts `escalate` with each position in at
  most five lines and its evidence. Gabriel answers with `decision`; that stands unless new evidence appears.
- Don't reopen agreed or decided points without new evidence.

## 6. Turns

Neither agent can contact the other. Gabriel starts a turn ("check your inbox"). A turn: fetch, read the inbox,
read the other agent's new commits, reply to every waiting thread, do the agreed work on your own branch, post
what you did, push. Everything needed is in the repository; nothing depends on chat history.

External audits are a standing turn-start checkpoint. At the start of every turn, each agent also checks
`docs/audits/2026-10-24-quantitative-research-upgrade-audit.md` (Gemini, F-R01 to F-R19) against the current
code, and reports any finding that is still open, was fixed incompletely, or regressed, in thread C006.

## 7. Safety rules both agents keep

- Paper accounts only. Never weaken the paper-only guards, kill switch or reconciliation.
- Never commit secrets: `.env`, `secrets/`, account numbers, tokens.
- Never relax a statistical gate to make a result pass; propose it in a thread with evidence.
