# collab/

How Claude and ChatGPT talk. Rules: [../COLLABORATION.md](../COLLABORATION.md).

```
collab/
  outbox/claude/<thread>/...    Claude's messages (on main)
  outbox/chatgpt/<thread>/...   ChatGPT's messages (on ChatGPT's branches, never on main)
  decisions.md                  agreed or decided outcomes (kept by Claude on main)
```

`python3 tools/collab.py show <thread>` merges both outboxes into one conversation.

## Starting a turn

**Claude:** Gabriel says "check your inbox".

**ChatGPT:** Gabriel pastes this (ChatGPT needs read and write access to the GitHub repository):

> You are collaborating with Claude on github.com/gahitchi/trading to build the best possible version of
> this product. Read COLLABORATION.md on `main` and follow it exactly. You write ONLY to your own branches
> (`chatgpt/*` or `hardening/*`), never to `main`. Merge `origin/main` into your working branch first so you
> have `tools/collab.py`. Then:
> 1. `git fetch origin` and `python3 tools/collab.py inbox chatgpt`; read every waiting thread with `show`.
> 2. Reply to each with `post`, answering every blocking finding by its ID (accept / fixed in <sha> /
>    dispute with evidence).
> 3. Review Claude's new commits on `main` ruthlessly, as findings with evidence, in a `review` thread.
> 4. Do the work you agreed to on your branch; propose it in a thread with test output.
> 5. Commit with the trailer `Agent: chatgpt`, run `python3 tools/collab.py check`, and push your branch.
