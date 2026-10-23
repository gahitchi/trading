import json
from unittest.mock import patch

import pytest

from lab.authority import Authority
from lab.persistence import atomic_json, read_json


@pytest.fixture
def authority(tmp_path):
    a = Authority(tmp_path, {"tickers": ["A"], "account": "DU1234567", "signals": [{"name": "one", "sha256": "abc"}]})
    a.issue("supervised")
    return a


def test_supervised_approval_is_exact_and_single_use(authority):
    order = {"ticker": "A", "qty": 10, "price": 100, "book": {"cash": 10000}}
    assert not authority.authorize(order)
    request = authority.requests()[0]["id"]
    authority.approve(request)
    assert not authority.authorize({**order, "qty": 11})
    assert not authority.authorize({**order, "book": {"cash": 9999}})
    assert authority.authorize(order)
    assert not authority.authorize(order)
    assert not Authority(authority.runtime, authority.identity).authorize(order)


def test_renewal_or_mode_change_invalidates_prior_approvals(authority):
    order = {"ticker": "A", "qty": 10}
    before = authority.read()
    assert not authority.authorize(order)
    request = authority.requests()[0]["id"]
    authority.approve(request)
    authority.issue("supervised")
    assert not authority.authorize(order)
    with pytest.raises(ValueError, match="active supervised mandate"):
        authority.approve(request)
    authority.issue("manual", targets={"A": 0.2})
    assert not authority.authorize(order, expected_nonce=before["nonce"])


def test_expiry_blocks_orders_but_not_reconciled_emergency_exit(authority):
    expiry = authority.read()["expires"]
    with patch("lab.authority.time.time", return_value=expiry + 1):
        with pytest.raises(ValueError, match="expired"):
            authority.authorize({"ticker": "A", "qty": 1})
        assert authority.authorize({"ticker": "A", "qty": 1}, emergency=True)


def test_runtime_binding_and_signature_are_enforced(authority):
    other = Authority(authority.runtime, {**authority.identity, "account": "DU7654321"})
    with pytest.raises(ValueError, match="identity"):
        other.read()
    rec = read_json(authority.path)
    rec["payload"]["mode"] = "autonomous"
    atomic_json(authority.path, rec)
    with pytest.raises(ValueError, match="signature"):
        authority.read()


def test_database_approval_without_operator_signature_cannot_authorize(authority):
    order = {"ticker": "A", "qty": 1}
    assert not authority.authorize(order)
    with authority.database() as db:
        db.execute("UPDATE request SET approval='fabricated'")
    assert not authority.authorize(order)


def test_manual_targets_and_lease_are_validated(authority):
    for mode, hours, targets in [
        ("bad", 1, {}),
        ("manual", 169, {}),
        ("manual", float("nan"), {}),
        ("manual", 1, {"B": 0.2}),
        ("manual", 1, {"A": True}),
    ]:
        with pytest.raises(ValueError):
            authority.issue(mode, hours, targets)
    authority.issue("manual", 1, {"A": -0.2})
    assert authority.read()["targets"] == {"A": -0.2}
    assert authority.authorize({"ticker": "A", "qty": 1})


def test_key_permissions_and_expired_request_fail_closed(authority):
    authority.authorize({"ticker": "A", "qty": 1})
    request = authority.requests()[0]["id"]
    with authority.database() as db:
        db.execute("UPDATE request SET expires=0")
    with pytest.raises(ValueError, match="expired"):
        authority.approve(request)
    authority.key_path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        authority.read()


def test_approval_capacity_is_bounded_and_expired_requests_are_reclaimed(authority):
    with authority.database() as db:
        db.executemany(
            "INSERT INTO request(id,payload,expires) VALUES(?,?,?)",
            ((str(i), json.dumps({}), authority.read()["expires"]) for i in range(1000)),
        )
    with pytest.raises(ValueError, match="capacity"):
        authority.authorize({"ticker": "A", "qty": 1})
    with authority.database() as db:
        db.execute("UPDATE request SET expires=0")
    assert not authority.authorize({"ticker": "A", "qty": 1})
    assert len(authority.requests()) == 1
