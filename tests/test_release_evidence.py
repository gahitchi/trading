"""A release verdict cannot be made green by dropping gates or changing evidence."""

import hashlib

import pytest

from tools.release_check import REQUIRED_GATES, blockers


def _record(tmp_path):
    (tmp_path / "proof.txt").write_text("measured result")
    return {
        "version": 1,
        "gates": [
            {
                "id": ident,
                "status": "passed",
                "review_commit": "a" * 40,
                "reviewer": "independent reviewer",
                "evidence": ["proof.txt"],
                "evidence_sha256": {"proof.txt": hashlib.sha256(b"measured result").hexdigest()},
            }
            for ident in sorted(REQUIRED_GATES)
        ],
    }


def test_complete_evidence_is_bound_to_candidate_and_bytes(tmp_path):
    record = _record(tmp_path)
    assert not blockers(record, tmp_path, "a" * 40)
    assert blockers(record, tmp_path, "b" * 40)
    (tmp_path / "proof.txt").write_text("replacement")
    assert any("digest mismatch" in s for s in blockers(record, tmp_path, "a" * 40))


@pytest.mark.parametrize(
    "field,value",
    [
        ("review_commit", "yes"),
        ("reviewer", ""),
        ("evidence_sha256", {}),
        ("evidence", ["../outside.txt"]),
        ("evidence", ["/etc/passwd"]),
    ],
)
def test_incomplete_or_unconfined_evidence_cannot_pass(tmp_path, field, value):
    record = _record(tmp_path)
    record["gates"][0][field] = value
    assert blockers(record, tmp_path, "a" * 40)


def test_removing_a_gate_does_not_make_it_optional(tmp_path):
    record = _record(tmp_path)
    record["gates"].pop()
    assert any("required gate is missing" in s for s in blockers(record, tmp_path, "a" * 40))


def test_symlink_cannot_escape_evidence_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    record = _record(root)
    (tmp_path / "outside.txt").write_text("measured result")
    (root / "proof.txt").unlink()
    (root / "proof.txt").symlink_to(tmp_path / "outside.txt")
    assert any("outside the repository" in s for s in blockers(record, root, "a" * 40))
