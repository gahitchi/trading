#!/usr/bin/env python3
"""
collab: structured, append-only conversations between the AI agents working on this repo.

Standard library only, so any agent can run it with plain `python3`. See COLLABORATION.md.

    python3 tools/collab.py list                      # threads and whose turn it is
    python3 tools/collab.py inbox claude              # threads waiting for a reply from an agent
    python3 tools/collab.py show 0001
    python3 tools/collab.py new  --agent claude --title "..." --kind proposal --body-file msg.md
    python3 tools/collab.py post 0001 --agent chatgpt --kind reply --body-file msg.md
    python3 tools/collab.py status 0001 --agent claude --set agreed
    python3 tools/collab.py check [--base REF]        # validate format; with --base also append-only
    python3 tools/collab.py check-commits A..B        # chatgpt commits on main may only touch collab/

Exit code 0 = OK, 1 = validation failure, 2 = usage error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THREADS = ROOT / "collab" / "threads"
AGENTS = ("claude", "chatgpt", "gabriel")
AI_AGENTS = ("claude", "chatgpt")
KINDS = ("proposal", "review", "reply", "agree", "object", "question", "decision", "status")
STATUSES = ("open", "agreed", "disagreed", "needs-gabriel", "closed")
FINDING_TAGS = ("blocking", "advisory", "question")

HEADER = re.compile(r"^### (?P<agent>[a-z]+) · (?P<at>\d{4}-\d\d-\d\dT\d\d:\d\dZ) · (?P<kind>[a-z]+)$")
FINDING = re.compile(r"^- \*\*(?P<id>[A-Z]\d+) \[(?P<tag>[a-z]+)\]\*\*")
FIELDS = ("id", "title", "status", "opened_by", "created", "refs")
MARKER = "<!-- messages: append only, newest last; never edit or delete a posted message -->"


class CollabError(Exception):
    pass


# ----------------------------------------------------------------------------- parsing


def parse(text: str, name: str = "thread") -> dict:
    if not text.startswith("---\n"):
        raise CollabError(f"{name}: must start with a '---' front-matter block")
    try:
        head, rest = text[4:].split("\n---\n", 1)
    except ValueError as e:
        raise CollabError(f"{name}: front matter is not closed with '---'") from e
    meta = {}
    for line in head.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise CollabError(f"{name}: bad front-matter line {line!r}")
        meta[key.strip()] = value.strip()
    missing = [f for f in FIELDS if f not in meta]
    if missing:
        raise CollabError(f"{name}: front matter misses {missing}")
    if meta["status"] not in STATUSES:
        raise CollabError(f"{name}: status {meta['status']!r} not in {STATUSES}")
    if meta["opened_by"] not in AGENTS:
        raise CollabError(f"{name}: opened_by {meta['opened_by']!r} not in {AGENTS}")
    if MARKER not in rest:
        raise CollabError(f"{name}: missing the messages marker line")
    body = rest.split(MARKER, 1)[1]
    messages, current = [], None
    for lineno, line in enumerate(body.splitlines(), 1):
        if line.startswith("### "):
            m = HEADER.match(line)
            if not m:
                raise CollabError(
                    f"{name}: bad message header {line!r} (expected '### agent · YYYY-MM-DDTHH:MMZ · kind')"
                )
            if m["agent"] not in AGENTS:
                raise CollabError(f"{name}: unknown agent {m['agent']!r}")
            if m["kind"] not in KINDS:
                raise CollabError(f"{name}: unknown message kind {m['kind']!r}; use one of {KINDS}")
            current = {"agent": m["agent"], "at": m["at"], "kind": m["kind"], "lines": []}
            messages.append(current)
        elif current is not None:
            current["lines"].append(line)
            f = FINDING.match(line)
            if f and f["tag"] not in FINDING_TAGS:
                raise CollabError(f"{name}: finding {f['id']} has tag {f['tag']!r}; use one of {FINDING_TAGS}")
        elif line.strip():
            raise CollabError(f"{name}: text after the marker must belong to a message (line {lineno})")
    times = [m["at"] for m in messages]
    if times != sorted(times):
        raise CollabError(f"{name}: messages are not in chronological order")
    if not messages:
        raise CollabError(f"{name}: a thread needs at least one message")
    return {"meta": meta, "messages": messages, "body": body}


def thread_files() -> list[Path]:
    return sorted(THREADS.glob("[0-9][0-9][0-9][0-9]-*.md")) if THREADS.exists() else []


def find(tid: str) -> Path:
    tid = tid.zfill(4)
    hits = [p for p in thread_files() if p.name.startswith(tid + "-")]
    if not hits:
        raise CollabError(f"no thread {tid}")
    return hits[0]


def waiting_on(t: dict) -> str | None:
    """Which AI agent should speak next on an open thread (None if nobody)."""
    if t["meta"]["status"] not in ("open", "disagreed"):
        return None
    last = t["messages"][-1]["agent"]
    if last == "gabriel":
        return "both"
    return "chatgpt" if last == "claude" else "claude"


# ----------------------------------------------------------------------------- writing


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(s) > 50:
        s = s[:51].rsplit("-", 1)[0]  # cut at a word boundary
    return s.strip("-") or "thread"


def render_message(agent: str, kind: str, body: str, at: str | None = None) -> str:
    return f"\n### {agent} · {at or now()} · {kind}\n\n{body.strip()}\n"


def cmd_new(a) -> int:
    THREADS.mkdir(parents=True, exist_ok=True)
    ids = [int(p.name[:4]) for p in thread_files()]
    tid = f"{(max(ids) + 1 if ids else 1):04d}"
    path = THREADS / f"{tid}-{slugify(a.title)}.md"
    refs = ", ".join(a.ref or [])
    text = (
        f"---\nid: {tid}\ntitle: {a.title}\nstatus: open\nopened_by: {a.agent}\n"
        f"created: {now()[:10]}\nrefs: [{refs}]\n---\n\n{MARKER}\n" + render_message(a.agent, a.kind, read_body(a))
    )
    parse(text, path.name)
    path.write_text(text)
    print(path.relative_to(ROOT))
    return 0


def cmd_post(a) -> int:
    path = find(a.id)
    t = parse(path.read_text(), path.name)
    if t["meta"]["status"] == "closed":
        raise CollabError(f"thread {a.id} is closed; open a new thread that references it")
    at = now()
    if at < t["messages"][-1]["at"]:
        at = t["messages"][-1]["at"]
    new = path.read_text().rstrip("\n") + "\n" + render_message(a.agent, a.kind, read_body(a), at)
    parse(new, path.name)
    path.write_text(new)
    print(f"posted to {path.relative_to(ROOT)}")
    return 0


def cmd_status(a) -> int:
    path = find(a.id)
    text = path.read_text()
    t = parse(text, path.name)
    if a.set == "agreed":
        agreed = {m["agent"] for m in t["messages"] if m["kind"] == "agree"}
        decided = any(m["agent"] == "gabriel" and m["kind"] == "decision" for m in t["messages"])
        if not (decided or set(AI_AGENTS) <= agreed):
            raise CollabError(
                "'agreed' needs an 'agree' message from both claude and chatgpt, or a decision by gabriel"
            )
    new = re.sub(r"^status: .*$", f"status: {a.set}", text, count=1, flags=re.M)
    new = new.rstrip("\n") + "\n" + render_message(a.agent, "status", f"Status set to **{a.set}**. {a.note or ''}")
    parse(new, path.name)
    path.write_text(new)
    print(f"{path.relative_to(ROOT)}: status {a.set}")
    return 0


def read_body(a) -> str:
    if a.body_file:
        body = Path(a.body_file).read_text()
    elif a.body:
        body = a.body
    else:
        body = sys.stdin.read()
    if not body.strip():
        raise CollabError("empty message")
    if any(line.startswith("### ") for line in body.splitlines()):
        raise CollabError("message bodies may not contain '### ' headings (reserved for message headers); use ####")
    if MARKER in body:
        raise CollabError("message body may not contain the marker line")
    return body


# ----------------------------------------------------------------------------- reading


def cmd_list(a) -> int:
    files = thread_files()
    if not files:
        print("no threads")
        return 0
    for p in files:
        t = parse(p.read_text(), p.name)
        m = t["meta"]
        w = waiting_on(t)
        last = t["messages"][-1]
        print(
            f"{m['id']}  {m['status']:<14} {('waiting: ' + w) if w else '':<17} "
            f"last {last['agent']} {last['at']}  {m['title']}"
        )
    return 0


def cmd_inbox(a) -> int:
    n = 0
    for p in thread_files():
        t = parse(p.read_text(), p.name)
        if waiting_on(t) in (a.agent, "both"):
            n += 1
            last = t["messages"][-1]
            print(f"{t['meta']['id']}  {t['meta']['title']}  (last: {last['agent']} {last['kind']} {last['at']})")
    if not n:
        print(f"nothing waiting for {a.agent}")
    return 0


def cmd_show(a) -> int:
    print(find(a.id).read_text())
    return 0


# ----------------------------------------------------------------------------- validation


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def check_append_only(base: str) -> list[str]:
    errors = []
    try:
        changed = git("diff", "--name-status", base, "--", "collab/threads").splitlines()
    except subprocess.CalledProcessError as e:
        return [f"cannot diff against {base}: {e.stderr.strip()}"]
    for line in changed:
        status, *paths = line.split("\t")
        path = paths[-1]
        if status.startswith("D"):
            errors.append(f"{path}: threads may not be deleted")
            continue
        if status.startswith("R"):
            errors.append(f"{path}: threads may not be renamed")
            continue
        if status.startswith("A"):
            continue
        try:
            old = parse(git("show", f"{base}:{path}"), path)
            new = parse((ROOT / path).read_text(), path)
        except CollabError as e:
            errors.append(str(e))
            continue
        if not new["body"].startswith(old["body"].rstrip("\n")):
            errors.append(f"{path}: posted messages were edited or removed (threads are append-only)")
        for key in ("id", "opened_by", "created"):
            if old["meta"][key] != new["meta"][key]:
                errors.append(f"{path}: front-matter field {key!r} may not change")
    return errors


def cmd_check(a) -> int:
    errors, ids = [], set()
    for p in thread_files():
        try:
            t = parse(p.read_text(), p.name)
        except CollabError as e:
            errors.append(str(e))
            continue
        if t["meta"]["id"] != p.name[:4]:
            errors.append(f"{p.name}: id {t['meta']['id']} does not match the file name")
        if t["meta"]["id"] in ids:
            errors.append(f"{p.name}: duplicate thread id")
        ids.add(t["meta"]["id"])
    if a.base:
        errors += check_append_only(a.base)
    for e in errors:
        print(f"ERROR {e}")
    print(f"{len(thread_files())} thread(s) checked, {len(errors)} error(s)")
    return 1 if errors else 0


def cmd_check_commits(a) -> int:
    """On main, commits whose message carries 'Agent: chatgpt' may only touch collab/."""
    errors = []
    shas = git("rev-list", "--no-merges", a.range).split()
    for sha in shas:
        msg = git("log", "-1", "--format=%B", sha)
        agents = re.findall(r"^Agent:\s*(\S+)\s*$", msg, flags=re.M | re.I)
        if any(x.lower() == "chatgpt" for x in agents):
            files = [f for f in git("show", "--name-only", "--format=", sha).splitlines() if f]
            outside = [f for f in files if not f.startswith("collab/")]
            if outside:
                errors.append(f"{sha[:8]}: chatgpt commit on main touches {outside[:5]} (code goes through a branch)")
    for e in errors:
        print(f"ERROR {e}")
    print(f"{len(shas)} commit(s) checked, {len(errors)} error(s)")
    return 1 if errors else 0


# ----------------------------------------------------------------------------- CLI


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="collab", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    ib = sub.add_parser("inbox")
    ib.add_argument("agent", choices=AGENTS)
    ib.set_defaults(fn=cmd_inbox)
    sh = sub.add_parser("show")
    sh.add_argument("id")
    sh.set_defaults(fn=cmd_show)
    for name, fn in (("new", cmd_new), ("post", cmd_post)):
        x = sub.add_parser(name)
        if name == "post":
            x.add_argument("id")
        else:
            x.add_argument("--title", required=True)
            x.add_argument("--ref", action="append", help="e.g. branch:foo, commit:abc1234, file:src/lab/x.py")
        x.add_argument("--agent", required=True, choices=AGENTS)
        x.add_argument("--kind", required=True, choices=KINDS)
        x.add_argument("--body")
        x.add_argument("--body-file")
        x.set_defaults(fn=fn)
    st = sub.add_parser("status")
    st.add_argument("id")
    st.add_argument("--agent", required=True, choices=AGENTS)
    st.add_argument("--set", required=True, choices=STATUSES)
    st.add_argument("--note")
    st.set_defaults(fn=cmd_status)
    ck = sub.add_parser("check")
    ck.add_argument("--base")
    ck.set_defaults(fn=cmd_check)
    cc = sub.add_parser("check-commits")
    cc.add_argument("range")
    cc.set_defaults(fn=cmd_check_commits)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except CollabError as e:
        print(f"collab: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
