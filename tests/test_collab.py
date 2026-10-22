"""tools/collab.py: thread format, turn-taking, append-only enforcement, commit policy."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def collab(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("collab_tool", REPO / "tools" / "collab.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "THREADS", tmp_path / "collab" / "threads")
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=tmp_path, check=True)
    return mod


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def test_conversation_turns_and_agreement(collab, tmp_path):
    assert (
        collab.main(
            [
                "new",
                "--agent",
                "claude",
                "--kind",
                "proposal",
                "--title",
                "Delisting returns",
                "--body",
                "- **F1 [blocking]** evidence here",
            ]
        )
        == 0
    )
    t = collab.parse(collab.find("1").read_text())
    assert collab.waiting_on(t) == "chatgpt"
    assert collab.main(["post", "1", "--agent", "chatgpt", "--kind", "agree", "--body", "F1: accept"]) == 0
    assert collab.waiting_on(collab.parse(collab.find("1").read_text())) == "claude"
    # 'agreed' requires agree from both AI agents
    assert collab.main(["status", "1", "--agent", "claude", "--set", "agreed"]) == 1
    assert collab.main(["post", "1", "--agent", "claude", "--kind", "agree", "--body", "agreed"]) == 0
    assert collab.main(["status", "1", "--agent", "claude", "--set", "agreed"]) == 0
    t = collab.parse(collab.find("1").read_text())
    assert t["meta"]["status"] == "agreed" and collab.waiting_on(t) is None
    assert [m["kind"] for m in t["messages"]] == ["proposal", "agree", "agree", "status"]


@pytest.mark.parametrize(
    "body,err",
    [
        ("### smuggled header", "reserved"),
        ("   ", "empty"),
    ],
)
def test_bad_bodies_rejected(collab, body, err, capsys):
    assert collab.main(["new", "--agent", "claude", "--kind", "proposal", "--title", "x", "--body", body]) == 1
    assert err in capsys.readouterr().err


def test_format_errors_are_caught(collab, tmp_path):
    collab.main(["new", "--agent", "claude", "--kind", "proposal", "--title", "x", "--body", "hello"])
    p = collab.find("1")
    p.write_text(p.read_text().replace("· proposal", "· shouting"))
    assert collab.main(["check"]) == 1
    p.write_text(
        p.read_text().replace("· shouting", "· proposal").replace("- **", "").rstrip() + "\n- **F9 [urgent]** x\n"
    )
    assert collab.main(["check"]) == 1


def test_append_only_against_git_history(collab, tmp_path):
    collab.main(["new", "--agent", "claude", "--kind", "proposal", "--title", "x", "--body", "original words"])
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "c1")
    base = git(tmp_path, "rev-parse", "HEAD").strip()
    collab.main(["post", "1", "--agent", "chatgpt", "--kind", "reply", "--body", "a reply"])
    assert collab.main(["check", "--base", base]) == 0  # appending is fine
    p = collab.find("1")
    p.write_text(p.read_text().replace("original words", "rewritten history"))
    assert collab.main(["check", "--base", base]) == 1  # editing a posted message is not


def test_chatgpt_commits_on_main_only_touch_collab(collab, tmp_path):
    (tmp_path / "README").write_text("x")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD").strip()
    collab.main(["new", "--agent", "chatgpt", "--kind", "proposal", "--title", "x", "--body", "hi"])
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "collab: hi", "-m", "Agent: chatgpt")
    assert collab.main(["check-commits", f"{base}..HEAD"]) == 0
    (tmp_path / "src.py").write_text("x = 1")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "sneaky code", "-m", "Agent: chatgpt")
    assert collab.main(["check-commits", f"{base}..HEAD"]) == 1


def test_repository_threads_are_valid():
    spec = importlib.util.spec_from_file_location("collab_repo", REPO / "tools" / "collab.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["check"]) == 0
