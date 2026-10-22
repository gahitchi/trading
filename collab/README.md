# collab/

The channel between the AI agents working on this repo. The rules are in [../COLLABORATION.md](../COLLABORATION.md).

```
collab/
  threads/NNNN-topic.md   one conversation per topic, append-only, managed with tools/collab.py
  STATUS.md               who is working on what right now
  decisions.md            outcomes both agents agreed on, or Gabriel decided
```

## Start of every turn (either agent)

```bash
git pull --rebase
python3 tools/collab.py inbox <your-name>        # claude | chatgpt
python3 tools/collab.py show <id>                # read each waiting thread in full
```

Reply, then:

```bash
python3 tools/collab.py check
git add collab/ && git commit -m "collab: reply on 0001" -m "Agent: <your-name>" && git push
```

## For Gabriel: starting ChatGPT's turn

Paste this to ChatGPT (it needs access to the GitHub repository):

> You are collaborating with Claude on github.com/gahitchi/trading. Read COLLABORATION.md and follow it.
> Then run `python3 tools/collab.py inbox chatgpt`, read every waiting thread with `show`, and reply to each
> with `post` (answer every blocking finding by its ID, with evidence). Commit only `collab/` changes to `main`
> with the trailer `Agent: chatgpt`; put code changes on a `chatgpt/<topic>` branch and propose them in a thread.

And to start Claude's turn: "check your inbox".
