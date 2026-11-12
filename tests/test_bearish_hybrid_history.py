"""Synthetic archive tests. No network requests, historical returns or protected data."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from lab.bearish_hybrid import MINUTE_NS
from lab.bearish_hybrid_history import load_hybrid_archive
from lab.bearish_hybrid_replay import DAY_NS, replay_hybrid_observations
from tests.test_bearish_hybrid import COSTS, PAPER, crash_sequence, events


def history():
    rows = crash_sequence()
    t = rows[-1].end_ns
    rows += [
        replace(rows[-1], end_ns=t + MINUTE_NS, open=97.8, high=97.9, low=97.7, close=97.82, quote_volume=100),
        replace(rows[-1], end_ns=t + 2 * MINUTE_NS, open=97.82, high=98.0, low=96.7, close=97.2),
    ]
    funding = events(rows[0].end_ns)
    oi_times = range(rows[0].end_ns - 20 * MINUTE_NS, rows[-1].end_ns + MINUTE_NS, 5 * MINUTE_NS)
    bundle = {
        "version": 1,
        "venue": "binance-usdm",
        "symbol": "BTCUSDT",
        "bars": [{**vars(row), "source_complete": True} for row in rows],
        "oi": [
            {"measured_ns": ns, "published_ns": ns, "value": 1_000_000 + i * 4000}
            for i, ns in enumerate(oi_times)
        ],
        "funding": [vars(f) for f in funding],
        "market": [
            {"asof_ns": row.end_ns, "spot_listed": True, "usdm_listed": True, "mean_daily_turnover_usdt_30d": 30_000_000}
            for row in rows
        ],
    }
    bound = (rows[-1].end_ns // DAY_NS + 1) * DAY_NS
    return bundle, bound


def read(bundle, boundary, *, verified=True):
    content = json.dumps(bundle, separators=(",", ":"), allow_nan=False).encode()
    return load_hybrid_archive(
        content, expected_sha256=hashlib.sha256(content).hexdigest(), boundary_ns=boundary, funding_verified=verified
    )


def test_digest_pinned_archive_supports_causal_virtual_signal():
    bundle, boundary = history()
    obs = read(bundle, boundary)
    assert len(obs) == len(bundle["bars"])
    assert obs[-3].oi is not None and obs[-3].oi.observed_ns <= obs[-3].bar.end_ns
    assert obs[-3].market.asof_ns == obs[-3].bar.end_ns
    assert all(f.published_ns <= row.bar.end_ns for row in obs for f in row.funding)
    result = replay_hybrid_observations(obs, boundary_ns=boundary, costs=COSTS, account=PAPER)
    assert len(result.trades) == 1 and not result.halted
    assert result.trades[0].exit_reason == "target"


def test_integrity_mismatch_and_duplicate_json_keys_are_rejected():
    bundle, boundary = history()
    payload = json.dumps(bundle).encode()
    with pytest.raises(ValueError, match="SHA-256"):
        load_hybrid_archive(payload, expected_sha256="0" * 64, boundary_ns=boundary)
    duplicate = payload.replace(b'"version": 1', b'"version": 1, "version": 1', 1)
    with pytest.raises(ValueError, match="duplicate archive JSON key"):
        load_hybrid_archive(
            duplicate, expected_sha256=hashlib.sha256(duplicate).hexdigest(), boundary_ns=boundary
        )


def test_holdout_observation_rejected_without_truncating():
    bundle, boundary = history()
    bundle["market"].append({**bundle["market"][-1], "asof_ns": boundary})
    with pytest.raises(ValueError, match="protected holdout"):
        read(bundle, boundary)


def test_future_published_oi_is_not_visible_earlier():
    bundle, boundary = history()
    base = read(bundle, boundary)
    end = bundle["bars"][-1]["end_ns"]
    future = {"measured_ns": end + MINUTE_NS, "published_ns": end + MINUTE_NS, "value": 100_000_000}
    bundle["oi"].append(future)
    assert read(bundle, boundary) == base


def test_noncausal_oi_and_invalid_metadata_rejected():
    bundle, boundary = history()
    bundle["oi"][0]["measured_ns"] = bundle["oi"][0]["published_ns"] + MINUTE_NS
    with pytest.raises(ValueError, match="noncausal"):
        read(bundle, boundary)
    bundle, boundary = history()
    bundle["market"][0]["spot_listed"] = 1
    with pytest.raises(ValueError, match="booleans"):
        read(bundle, boundary)


def test_funding_requires_explicit_external_attestation():
    bundle, boundary = history()
    obs = read(bundle, boundary, verified=False)
    result = replay_hybrid_observations(obs, boundary_ns=boundary, costs=COSTS, account=PAPER)
    assert not result.trades and not result.funded_signal_times_ns


def test_gap_is_preserved_and_replay_restarts_warmup():
    bundle, boundary = history()
    bundle["bars"].pop(-4)
    bundle["market"].pop(-4)
    obs = read(bundle, boundary)
    result = replay_hybrid_observations(obs, boundary_ns=boundary, costs=COSTS, account=PAPER)
    assert not result.trades and not result.funded_signal_times_ns
