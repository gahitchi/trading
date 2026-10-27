#!/usr/bin/env python3
"""
collab: conversations between the AI agents on this repo, where each agent writes ONLY to its own branch.

Every message is one immutable file in the author's outbox, committed on a branch the author owns:

    collab/outbox/<agent>/<thread>/<YYYYMMDDTHHMMSSZ>[-n]-<kind>.md      (attachments may sit next to it)

The tool reads the outboxes of every branch (each only for the agent that owns that branch) and merges them into
one conversation per thread. Standard library only; it works from any branch, even run from a copy:

    git fetch origin                                   # or: python3 tools/collab.py sync
    python3 tools/collab.py inbox chatgpt              # threads waiting for chatgpt
    python3 tools/collab.py show C001                  # the whole conversation, both branches merged
    python3 tools/collab.py post C001 --agent chatgpt --kind reply --body-file reply.md
    python3 tools/collab.py new --agent chatgpt --kind proposal --title "..." --body-file m.md
    python3 tools/collab.py check [--base REF]         # format, ownership, immutability (CI runs this)

Branch ownership (who may write on which branch) is OWNERS below, mirrored in COLLABORATION.md.
Exit codes: 0 OK, 1 validation failure, 2 usage error.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

AGENTS = ("claude", "chatgpt", "gabriel")
AI_AGENTS = ("claude", "chatgpt")
PREFIX = {"claude": "C", "chatgpt": "G", "gabriel": "H"}  # thread ids: C001 opened by claude, G001 by chatgpt
# Branch -> owning agent. Gabriel (the human owner) may write on any branch.
OWNERS = (("main", "claude"), ("claude/*", "claude"), ("chatgpt/*", "chatgpt"), ("hardening/*", "chatgpt"))
KINDS = ("proposal", "review", "reply", "question", "agree", "object", "escalate", "close", "decision")
OPENING_KINDS = ("proposal", "review", "question")
FINDING_TAGS = ("blocking", "advisory", "question")
OUTBOX = "collab/outbox"

# Closed-set, immutable historical metadata errata: original messages are
# unchanged. New messages still obey exactly the original validator.
LEGACY_METADATA_ERRATA = {
    "collab/outbox/chatgpt/G040/20261027T122006Z-proposal.md": (
        "5ae7bf5a3efd627a27cc3943610d6df67cf4f9bf",
        "2026-10-27T12:20:06.886Z",
        "Proposal — versioned institutional development tree and acceptance roadmap",
    ),
    "collab/outbox/chatgpt/G041/20261027T124500Z-proposal.md": (
        "8e7436810da9dc2dc95c47cf4dab68a04865bcf3",
        "2026-10-27T12:45:00Z",
        "F0–F8 foundation-first roadmap: corrected canonical execution order",
    ),
    "collab/outbox/chatgpt/G042/20261027T130419Z-proposal.md": (
        "a4d9fd804f37193f9572e9ccd6f5be4b37b67a15",
        "2026-10-27T13:04:19Z",
        "F0.1–F0.4 foundation first: reference slice and exact-candidate inventory",
    ),
    "collab/outbox/chatgpt/G043/20261027T134152Z-proposal.md": (
        "419d683227b5ce451f9db31114c3978d4bd3ae78",
        "2026-10-27T13:41:52Z",
        "F0.2 exact runtime build provenance: narrow candidate",
    ),
}


def _historical_message_title(text: str, path: str, meta: dict) -> str | None:
    """Byte-pinned erratum, never a wildcard exemption or message mutation."""
    entry = LEGACY_METADATA_ERRATA.get(path)
    if entry is None:
        return None
    raw = text.encode("utf-8")
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + bytes([0]) + raw).hexdigest()
    expected_blob, original_at, title = entry
    if blob != expected_blob or meta.get("at") != original_at or "title" in meta:
        raise CollabError(f"{path}: registered historical metadata digest or fields changed")
    return title


NAME = re.compile(r"^(?P<stamp>\d{8}T\d{6}Z)(?:-(?P<seq>\d+))?-(?P<kind>[a-z]+)\.md$")
THREAD_ID = re.compile(r"^[CGH]\d{3}$")
FINDING = re.compile(r"^- \*\*(?P<id>[A-Z]+\d+) \[(?P<tag>[a-z]+)\]\*\*")


class CollabError(Exception):
    pass


# ----------------------------------------------------------------------------- git helpers

ROOT: Path | None = None  # tests point this at a temporary repository


def root() -> Path:
    if ROOT is not None:
        return ROOT
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if p.returncode != 0:
        raise CollabError("run inside a clone of the repository")
    return Path(p.stdout.strip())


def git(*args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=root(), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise CollabError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def owner_of(branch: str) -> str | None:
    for pattern, agent in OWNERS:
        if fnmatch.fnmatch(branch, pattern):
            return agent
    return None


def current_branch() -> str:
    b = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    if b == "HEAD":  # detached HEAD, e.g. a CI checkout
        b = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME") or b
    return b


def path_agent(path: str) -> str:
    parts = PurePosixPath(path).parts
    return parts[2] if len(parts) > 2 else ""


# ----------------------------------------------------------------------------- messages


def parse_message(text: str, path: str) -> dict:
    parts = PurePosixPath(path).parts
    if len(parts) != 5 or parts[:2] != ("collab", "outbox"):
        raise CollabError(f"{path}: messages live at collab/outbox/<agent>/<thread>/<stamp>-<kind>.md")
    agent, thread, name = parts[2], parts[3], parts[4]
    if agent not in AGENTS:
        raise CollabError(f"{path}: unknown agent {agent!r}")
    if not THREAD_ID.match(thread):
        raise CollabError(f"{path}: thread id {thread!r} must look like C001 / G001 / H001")
    m = NAME.match(name)
    if not m:
        raise CollabError(f"{path}: file name must be <YYYYMMDDTHHMMSSZ>[-<n>]-<kind>.md")
    if m["kind"] not in KINDS:
        raise CollabError(f"{path}: unknown kind {m['kind']!r}; use one of {KINDS}")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise CollabError(f"{path}: must start with a '---' front-matter block")
    head, body = text[4:].split("\n---\n", 1)
    meta = {}
    for line in head.splitlines():
        if line.strip():
            key, sep, value = line.partition(":")
            if not sep:
                raise CollabError(f"{path}: bad front-matter line {line!r}")
            meta[key.strip()] = value.strip()
    for key, expected in (("agent", agent), ("thread", thread), ("kind", m["kind"])):
        if meta.get(key) != expected:
            raise CollabError(f"{path}: front matter {key}={meta.get(key)!r} must match the path ({expected!r})")
    stamp = datetime.strptime(m["stamp"], "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")
    legacy_title = _historical_message_title(text, path, meta)
    if legacy_title is None and meta.get("at") != stamp:
        raise CollabError(f"{path}: front matter at={meta.get('at')!r} must match the file name ({stamp})")
    if not body.strip():
        raise CollabError(f"{path}: empty message")
    for line in body.splitlines():
        f = FINDING.match(line)
        if f and f["tag"] not in FINDING_TAGS:
            raise CollabError(f"{path}: finding {f['id']} has tag {f['tag']!r}; use one of {FINDING_TAGS}")
    return {
        "path": path,
        "agent": agent,
        "thread": thread,
        "kind": m["kind"],
        "at": stamp,
        "seq": int(m["seq"] or 0),
        "title": legacy_title if legacy_title is not None else meta.get("title", ""),
        "refs": meta.get("refs", ""),
        "body": body,
    }


def is_message(path: str) -> bool:
    return path.startswith(OUTBOX + "/") and path.endswith(".md") and len(PurePosixPath(path).parts) == 5


def collect(include_worktree: bool = True, any_worktree_author: bool = False) -> dict[str, list[dict]]:
    """All messages from all branches, each read only from a branch its author owns. Keyed by thread.

    any_worktree_author: also take other agents' files present in the working tree (merged copies) at face
    value. Only for structural checks: authorship of such files is verified separately by `check --base`.
    """
    found: dict[str, dict] = {}
    refs = git("for-each-ref", "--format=%(refname:short)", "refs/remotes", "refs/heads").split()
    for ref in refs:
        branch = ref.split("/", 1)[1] if ref.startswith("origin/") else ref
        owner = owner_of(branch)
        if owner is None:
            continue
        for path in git("ls-tree", "-r", "--name-only", ref, "--", OUTBOX, check=False).splitlines():
            if is_message(path) and path_agent(path) in (owner, "gabriel") and path not in found:
                found[path] = parse_message(git("show", f"{ref}:{path}"), path)
    if include_worktree:
        owner = owner_of(current_branch())
        base = root() / OUTBOX
        if base.exists():
            for f in sorted(base.rglob("*.md")):
                path = f.relative_to(root()).as_posix()
                allowed = any_worktree_author or path_agent(path) in (owner, "gabriel")
                if is_message(path) and allowed and path not in found:
                    found[path] = parse_message(f.read_text(), path)
    threads: dict[str, list[dict]] = {}
    for msg in found.values():
        threads.setdefault(msg["thread"], []).append(msg)
    for msgs in threads.values():
        msgs.sort(key=lambda m: (m["at"], m["seq"], m["agent"]))
    return dict(sorted(threads.items()))


def thread_title(msgs: list[dict]) -> str:
    return next((m["title"] for m in msgs if m["title"]), "(untitled)")


def thread_state(msgs: list[dict]) -> str:
    last = msgs[-1]
    if last["kind"] == "close" or (last["agent"] == "gabriel" and last["kind"] == "decision"):
        return "closed"
    if last["kind"] == "escalate":
        return "needs-gabriel"
    last_by = {m["agent"]: m["kind"] for m in msgs if m["agent"] in AI_AGENTS}
    if all(last_by.get(a) == "agree" for a in AI_AGENTS):
        return "agreed"
    return "open"


def waiting_on(msgs: list[dict]) -> str | None:
    state = thread_state(msgs)
    if state == "needs-gabriel":
        return "gabriel"
    if state != "open":
        return None
    last = msgs[-1]["agent"]
    return "both" if last == "gabriel" else ("chatgpt" if last == "claude" else "claude")


# ----------------------------------------------------------------------------- writing


def clock() -> datetime:
    """UTC now; COLLAB_NOW=2026-10-22T14:00:00Z overrides it (tests, replaying a message)."""
    fixed = os.environ.get("COLLAB_NOW")
    if fixed:
        return datetime.strptime(fixed, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return datetime.now(UTC)


def write_message(
    agent: str, thread: str, kind: str, body: str, title: str = "", refs: list[str] | None = None
) -> Path:
    branch = current_branch()
    owner = owner_of(branch)
    if agent != "gabriel" and owner != agent:
        raise CollabError(f"branch {branch!r} belongs to {owner or 'nobody'}; {agent} may only write on its own branch")
    if not body.strip():
        raise CollabError("empty message")
    now = clock()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    d = root() / OUTBOX / agent / thread
    d.mkdir(parents=True, exist_ok=True)
    taken = {(m["stamp"], int(m["seq"] or 0)) for p in d.glob("*.md") if (m := NAME.match(p.name))}
    seq = 0
    while (stamp, seq) in taken:
        seq += 1
    name = f"{stamp}-{kind}.md" if seq == 0 else f"{stamp}-{seq}-{kind}.md"
    front = [f"agent: {agent}", f"thread: {thread}", f"kind: {kind}", f"at: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}"]
    if title:
        front.append(f"title: {title}")
    if refs:
        front.append(f"refs: [{', '.join(refs)}]")
    text = "---\n" + "\n".join(front) + "\n---\n\n" + body.strip() + "\n"
    path = d / name
    parse_message(text, path.relative_to(root()).as_posix())
    path.write_text(text)
    return path


def read_body(a) -> str:
    if a.body_file:
        return Path(a.body_file).read_text()
    if a.body:
        return a.body
    return sys.stdin.read()


def cmd_new(a) -> int:
    if a.kind not in OPENING_KINDS:
        raise CollabError(f"a thread opens with one of {OPENING_KINDS}")
    prefix = PREFIX[a.agent]
    used = [int(t[1:]) for t in collect() if t.startswith(prefix)]
    tid = f"{prefix}{(max(used) + 1 if used else 1):03d}"
    path = write_message(a.agent, tid, a.kind, read_body(a), a.title, a.ref)
    print(f"{tid}: {path.relative_to(root())}")
    return 0


def cmd_post(a) -> int:
    threads = collect()
    if a.thread not in threads:
        raise CollabError(f"no thread {a.thread} (run `git fetch origin` first?)")
    path = write_message(a.agent, a.thread, a.kind, read_body(a))
    print(f"posted {path.relative_to(root())}")
    return 0


# ----------------------------------------------------------------------------- reading


def cmd_list(a) -> int:
    threads = collect()
    if not threads:
        print("no threads")
        return 0
    for tid, msgs in threads.items():
        w = waiting_on(msgs)
        last = msgs[-1]
        print(
            f"{tid}  {thread_state(msgs):<13} {('waiting: ' + w) if w else '':<17} "
            f"{len(msgs):>2} msg, last {last['agent']} {last['at']}  {thread_title(msgs)}"
        )
    return 0


def cmd_inbox(a) -> int:
    n = 0
    for tid, msgs in collect().items():
        if waiting_on(msgs) in (a.agent, "both"):
            n += 1
            last = msgs[-1]
            print(f"{tid}  {thread_title(msgs)}  (last: {last['agent']} {last['kind']} {last['at']})")
    if not n:
        print(f"nothing waiting for {a.agent}")
    return 0


def cmd_show(a) -> int:
    threads = collect()
    if a.thread not in threads:
        raise CollabError(f"no thread {a.thread}")
    msgs = threads[a.thread]
    print(f"# {a.thread}: {thread_title(msgs)}   [{thread_state(msgs)}]\n")
    for m in msgs:
        print(f"## {m['agent']} · {m['at']} · {m['kind']}   ({m['path']})")
        if m["refs"]:
            print(f"refs: {m['refs']}")
        print(m["body"].rstrip() + "\n")
    return 0


def cmd_sync(a) -> int:
    git("fetch", "--prune", "origin")
    print("fetched all branches")
    return 0


# ----------------------------------------------------------------------------- validation


def on_owned_branch(sha: str, agent: str) -> bool:
    """True if a branch owned by `agent` (local or remote) contains commit `sha`."""
    refs = git("for-each-ref", "--contains", sha, "--format=%(refname:short)", "refs/remotes", "refs/heads").split()
    return any(owner_of(r.split("/", 1)[1] if r.startswith("origin/") else r) == agent for r in refs)


def merged_from_owner(path: str, author: str) -> bool:
    """A file written by another agent is allowed on this branch only as an unchanged copy from
    a branch that agent owns (e.g. after merging main into a chatgpt branch)."""
    here = git("rev-parse", f"HEAD:{path}", check=False).strip()
    if not here:
        return False
    for ref in git("for-each-ref", "--format=%(refname:short)", "refs/remotes", "refs/heads").split():
        branch = ref.split("/", 1)[1] if ref.startswith("origin/") else ref
        if owner_of(branch) == author and git("rev-parse", f"{ref}:{path}", check=False).strip() == here:
            return True
    return False


def cmd_check(a) -> int:
    """Validate this branch's outbox (format, thread openings, ownership) and, with --base, immutability."""
    errors = []
    branch = current_branch()
    owner = owner_of(branch)
    local = 0
    base_dir = root() / OUTBOX
    if base_dir.exists():
        for f in sorted(base_dir.rglob("*.md")):
            path = f.relative_to(root()).as_posix()
            local += 1
            try:
                parse_message(f.read_text(), path)
            except CollabError as e:
                errors.append(str(e))
    try:
        threads = collect(any_worktree_author=True)
    except CollabError as e:
        errors.append(str(e))
        threads = {}
    # A shallow checkout of one branch cannot see threads the other agent opened on its own branch: there,
    # the visible messages start with a reply. That is an incomplete view, not an invalid thread.
    visible = {
        owner_of(r.split("/", 1)[1] if r.startswith("origin/") else r)
        for r in git("for-each-ref", "--format=%(refname:short)", "refs/remotes", "refs/heads").split()
    }
    visible.add(owner)
    unseen = []
    for tid, msgs in threads.items():
        first = msgs[0]
        opener = next((agent for agent, prefix in PREFIX.items() if prefix == tid[0]), None)
        if opener not in visible and opener != first["agent"]:
            unseen.append(tid)
            continue
        if first["kind"] not in OPENING_KINDS or not first["title"]:
            errors.append(f"{tid}: the first message must be one of {OPENING_KINDS} and carry a title")
        if PREFIX[first["agent"]] != tid[0]:
            errors.append(f"{tid}: opened by {first['agent']}, but the id prefix belongs to someone else")
    if a.base:
        for line in git("diff", "--name-status", a.base, "HEAD", "--", OUTBOX).splitlines():
            status, *paths = line.split("\t")
            if status[0] in "DMRTC":
                errors.append(f"{paths[0]}: posted messages and attachments are immutable ({status})")
            new_path = paths[-1]
            author = path_agent(new_path)
            if status[0] in "ARC" and author not in (owner, "gabriel") and not merged_from_owner(new_path, author):
                errors.append(
                    f"{new_path}: branch {branch!r} belongs to {owner or 'nobody'}; "
                    f"it may not add files written as {author!r} (unless merged unchanged from {author}'s branch)"
                )
    if a.base and owner:
        known = (*AI_AGENTS, "gabriel")
        for sha in git("rev-list", f"{a.base}..HEAD").split():
            signed = re.findall(r"^Agent:\s*(\S+)\s*$", git("log", "-1", "--format=%B", sha), flags=re.M | re.I)
            # Every new commit, merges included, names exactly one known author (G007-F14, ChatGPT: a commit
            # without a trailer used to pass). Attribution metadata, not a cryptographic signature.
            if len(signed) != 1 or signed[0].lower() not in known:
                errors.append(
                    f"commit {sha[:8]} must carry exactly one 'Agent: <{'|'.join(known)}>' trailer "
                    f"(found {signed or 'none'})"
                )
                continue
            for agent in signed:
                signer = agent.lower()
                # A commit signed by the other agent is legitimate here only if it already lives on a
                # branch that agent owns, i.e. it arrived by merging that branch (idea: ChatGPT, G001-F1).
                if signer not in (owner, "gabriel") and not on_owned_branch(sha, signer):
                    errors.append(
                        f"commit {sha[:8]} is signed 'Agent: {agent}' on {owner}'s branch {branch!r}, "
                        f"and no branch owned by {signer} contains it"
                    )
    for e in errors:
        print(f"ERROR {e}")
    if unseen:
        print(f"note: opening not visible for {', '.join(unseen)} (the opener's branch is not fetched here)")
    print(
        f"branch {branch} (owner: {owner or 'none'}): {local} file(s) in this outbox tree, "
        f"{len(threads)} thread(s) across branches, {len(errors)} error(s)"
    )
    return 1 if errors else 0


# ----------------------------------------------------------------------------- CLI


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="collab", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync").set_defaults(fn=cmd_sync)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    ib = sub.add_parser("inbox")
    ib.add_argument("agent", choices=AGENTS)
    ib.set_defaults(fn=cmd_inbox)
    sh = sub.add_parser("show")
    sh.add_argument("thread")
    sh.set_defaults(fn=cmd_show)
    for name, fn in (("new", cmd_new), ("post", cmd_post)):
        x = sub.add_parser(name)
        if name == "post":
            x.add_argument("thread")
        else:
            x.add_argument("--title", required=True)
            x.add_argument("--ref", action="append", help="e.g. branch:foo, commit:abc1234, file:src/lab/x.py")
        x.add_argument("--agent", required=True, choices=AGENTS)
        x.add_argument("--kind", required=True, choices=KINDS)
        x.add_argument("--body")
        x.add_argument("--body-file")
        x.set_defaults(fn=fn)
    ck = sub.add_parser("check")
    ck.add_argument("--base", help="git ref to compare against: posted messages may not change")
    ck.set_defaults(fn=cmd_check)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except CollabError as e:
        print(f"collab: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
