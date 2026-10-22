# Collaboration protocol

Two AI agents work on this repository for its owner. This file is the contract between them.

| Who | Role |
|---|---|
| **Gabriel** | Owner. Final decision on anything the agents disagree on. Relays turns between the agents. |
| **Claude** | Maintainer of `main` and integrator: decides what lands on `main`, keeps it green. |
| **ChatGPT** | Contributor and reviewer: develops on its own branches, reviews Claude's changes. |

Neither agent can wake the other. Gabriel starts each agent's turn ("check your inbox"); everything they need
to know is in the repository, so a turn never depends on chat history.

## 1. Channel: threads in `collab/threads/`

All communication between the agents happens in thread files, one Markdown file per topic, managed with
`tools/collab.py` (Python standard library only):

```bash
python3 tools/collab.py inbox chatgpt            # what is waiting for me?
python3 tools/collab.py show 0001
python3 tools/collab.py post 0001 --agent chatgpt --kind reply --body-file /tmp/reply.md
python3 tools/collab.py new --agent chatgpt --kind proposal --title "..." --ref branch:chatgpt/x --body-file m.md
python3 tools/collab.py check                    # always run before committing
```

Rules:
- **Append-only.** Never edit or delete a posted message, yours or the other agent's. Corrections are new
  messages. CI rejects edits (`check --base`).
- **Messages go straight to `main`**, in commits that touch only `collab/` and carry the trailer
  `Agent: chatgpt` or `Agent: claude`. Always `git pull --rebase` before pushing; if two messages race,
  rebase and post again. CI rejects ChatGPT commits on `main` that touch anything outside `collab/`.
- **Message kinds**: `proposal` (a change for `main`), `review`, `reply`, `question`, `agree`, `object`,
  `status`, and `decision` (Gabriel only).
- One topic per thread. A new topic gets a new thread that references the old one.

## 2. Findings

Reviews state findings with an ID and a tag, so replies can answer them one by one:

```markdown
- **F3 [blocking]** The critic gate rejects every hypothesis ... Evidence: `...command or test...`
- **F4 [advisory]** Consider ...
- **F5 [question]** Is ... intended?
```

- `blocking`: must be resolved before the change lands. Requires **evidence**: a command, a failing test, a
  file and line, or a measurement. Opinions are `advisory`.
- Every blocking finding gets an answer by ID: `F3: accept`, `F3: fixed in <sha>`, or
  `F3: dispute: <reason and counter-evidence>`.
- Findings IDs are unique within a thread (F1, F2, ... in order of appearance, across both agents).

## 3. How code reaches `main`

- **ChatGPT** develops on branches named `chatgpt/<topic>` (the existing `hardening/unattended-research-20261022`
  is grandfathered). To propose work, open a `proposal` thread with: branch, commit SHA, what changed, why, and
  the output of `pytest -q` and `ruff check src tests`.
- **Claude** reviews in the thread and integrates what has no open blocking finding, by merge or cherry-pick,
  crediting the author in the commit message. Claude may integrate part of a branch and say which part.
- **Claude's own changes** to safety-critical code go through the same review: open a `proposal` thread before
  or with the commit, and wait for ChatGPT's reply (or Gabriel's waiver) before building further on it.
  Safety-critical means `src/lab/agents/`, `safety.py`, `paper.py`, `backtest.py`, `experiments.py`,
  `stats.py`, `research/` gates and sandbox, `data.py` validation, and deployment files.
- Everything on `main` must pass CI: lint, format, tests, the offline example and the Docker build.

## 4. Agreement and disagreement

- A thread is **agreed** when both agents have posted `agree` (or Gabriel posts a `decision`). Record the
  outcome in `collab/decisions.md` with the thread ID.
- After **two rounds** of disagreement on a finding, set the thread to `needs-gabriel` and write a summary:
  each position in at most five lines, with the evidence. Gabriel decides; the decision is final unless
  new evidence appears.
- Don't reopen agreed or decided topics without new evidence.

## 5. Coordination

- Before starting work that touches shared code, add a line to `collab/STATUS.md` (who, what, files, branch).
  Don't edit files another agent has claimed; ask in a thread.
- Evidence over assertion: behaviour claims come with a reproducible command or test.
- Critique the code, not the agent. Be concise. Credit good work explicitly.

## 6. Safety rules both agents keep

- Paper accounts only. Never weaken the paper-only guards, the kill switch, or reconciliation.
- Never commit secrets (`.env`, `secrets/`, account numbers, tokens).
- Never relax a statistical gate to make a result pass; propose the change in a thread with evidence.
