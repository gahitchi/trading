"""Byte-pinned historical collaboration errata preserve strict new-message rules."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
G043 = "collab/outbox/chatgpt/G043/20261027T134152Z-proposal.md"
G044 = "collab/outbox/chatgpt/G044/20261027T225111Z-proposal.md"


def load_tool():
    spec = importlib.util.spec_from_file_location("collab_legacy_test", ROOT / "tools" / "collab.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


def test_exact_historical_proposal_stays_unchanged_but_is_readable():
    tool = load_tool()
    data = (ROOT / G043).read_text()
    record = tool.parse_message(data, G043)
    assert record["title"] == "F0.2 exact runtime build provenance: narrow candidate"
    assert record["at"] == "2026-10-27T13:41:52Z"
    assert len(tool.LEGACY_METADATA_ERRATA) == 5


@pytest.mark.parametrize("mutation", ["\n# extra text", "\nsecret", "\n---\n"])
def test_mutating_historical_message_revokes_exception(mutation):
    tool = load_tool()
    data = (ROOT / G043).read_text()
    with pytest.raises(tool.CollabError, match="digest or fields changed"):
        tool.parse_message(data + mutation, G043)


def test_adding_title_to_posted_record_is_not_an_approved_rewrite():
    tool = load_tool()
    data = (ROOT / G043).read_text().replace("kind: proposal\n", "kind: proposal\ntitle: forged\n")
    with pytest.raises(tool.CollabError, match="digest or fields changed"):
        tool.parse_message(data, G043)


def test_future_millisecond_timestamp_is_still_rejected():
    tool = load_tool()
    path = "collab/outbox/chatgpt/G999/20261027T160000Z-proposal.md"
    text = (
        "---\nagent: chatgpt\nthread: G999\nkind: proposal\n"
        "title: new milestone\nat: 2026-10-27T16:00:00.001Z\n---\n\n# New\n"
    )
    with pytest.raises(tool.CollabError, match="must match the file name"):
        tool.parse_message(text, path)


def test_exception_requires_exact_path_as_well_as_blob():
    tool = load_tool()
    data = (ROOT / G043).read_text()
    different = "collab/outbox/chatgpt/G043/20261027T134153Z-proposal.md"
    with pytest.raises(tool.CollabError, match="must match the file name"):
        tool.parse_message(data, different)


def test_exact_original_g044_is_accepted_without_allowing_rewritten_messages():
    tool = load_tool()
    original = (ROOT / G044).read_text()
    observed = tool.parse_message(original, G044)
    assert observed["thread"] == "G044"
    assert observed["title"] == "Roadmap integration proposal: F0-F8, independent engine and institutional MVP"
    assert observed["at"] == "2026-10-27T22:51:11Z"
    with pytest.raises(tool.CollabError, match="digest or fields changed"):
        tool.parse_message(original + "\nchanged", G044)
