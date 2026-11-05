"""Funding evidence tests: bounded API history, no-clobber cache, sign/mark settlement math.

All exchange responses are synthetic and injected; no live HTTP or holdout file reads.
"""

import json
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest

from lab.crash_funding import apply, audit, settlement_return, survey


def setup(tmp_path, boundary="2024-01-03T00:00:00Z"):
    config = tmp_path / "lab.toml"
    config.write_text('[paths]\ndata="data"\nresults="results"\n')
    folder = tmp_path / "results"
    folder.mkdir()
    (folder / "crash_partition.json").write_text(
        json.dumps({"version": 1, "holdout_start": boundary})
    )
    return config


def events(start="2024-01-01", hours=48, interval=8):
    t0 = pd.Timestamp(start, tz="UTC")
    return [
        {
            "symbol": "BTCUSDT",
            "fundingTime": int((t0 + pd.Timedelta(hours=i)).value // 1_000_000),
            "fundingRate": "0.00010000" if i % 16 == 0 else "-0.00005000",
            "markPrice": "100.00",
        }
        for i in range(0, hours, interval)
    ]


def fake_get(source, seen):
    def get(url):
        q = parse_qs(urlsplit(url).query)
        assert q["symbol"] == ["BTCUSDT"]
        assert q["limit"] == ["1000"]
        low = int(q["startTime"][0])
        high = int(q["endTime"][0])
        seen.append((low, high))
        assert high < int(pd.Timestamp("2024-01-03T00:00:00Z").value // 1_000_000)
        return [
            r for r in source if low <= r["fundingTime"] <= high
        ][:1000]
    return get


def test_survey_only_pre_holdout_queried_and_no_artifacts_created(tmp_path):
    cfg = setup(tmp_path)
    source, seen = events(), []
    r = survey(cfg, get=fake_get(source, seen))
    assert r["event_count"] == 6
    assert r["holdout_start"] == "2024-01-03"
    assert r["kind"].endswith("-v1")
    assert len(r["pages"]) == 1 and len(r["pages"][0]["source_sha256"]) == 64
    assert len(seen) == 1
    assert not (tmp_path / "data").exists()


def test_apply_and_read_only_audit_with_idempotent_no_clobber(tmp_path):
    cfg = setup(tmp_path)
    source, seen = events(), []
    get = fake_get(source, seen)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(survey(cfg, get=get)))
    assert apply(cfg, plan, get=get)["status"] == "committed"
    assert audit(cfg, plan)["ok"]
    assert apply(cfg, plan, get=get)["status"] == "verified-existing"
    root = tmp_path / "data" / "crash" / "funding-pinned" / "BTCUSDT"
    saved = list(root.glob("*.json"))
    assert len(saved) == 1
    before = saved[0].read_bytes()
    assert audit(cfg, plan)["ok"]
    assert saved[0].read_bytes() == before
    altered = json.loads(before)
    altered["events"][0]["fundingRate"] = "0.99"
    saved[0].write_text(json.dumps(altered))
    assert not audit(cfg, plan)["ok"]
    with pytest.raises(ValueError, match="differs"):
        apply(cfg, plan, get=get)


def test_source_change_rejected_before_any_cache_write(tmp_path):
    cfg = setup(tmp_path)
    source = events()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(survey(cfg, get=fake_get(source, []))))
    changed = [dict(row) for row in source]
    changed[0]["fundingRate"] = "0.0002"
    with pytest.raises(ValueError, match="SHA-256"):
        apply(cfg, plan, get=fake_get(changed, []))
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("issue", [
    "duplicate", "future", "off_grid", "wrong_symbol", "missing_mark",
    "nan", "large_rate", "missing_event", "out_of_order",
])
def test_invalid_exchange_history_fails_closed(tmp_path, issue):
    cfg = setup(tmp_path)
    source = events()
    if issue == "duplicate":
        source = [source[0], source[0], *source[1:]]
    elif issue == "future":
        source = [*source, {**source[-1], "fundingTime": int(pd.Timestamp("2024-01-03T00:00:00Z").value // 1_000_000)}]
        # Ensure malformed response actually reaches the validator.
        def invalid_get(url):
            return source
        with pytest.raises(ValueError, match="outside"):
            survey(cfg, get=invalid_get)
        return
    elif issue == "off_grid":
        source[0] = {**source[0], "fundingTime": source[0]["fundingTime"] + 5}
    elif issue == "wrong_symbol":
        source[0] = {**source[0], "symbol": "ETHUSDT"}
    elif issue == "missing_mark":
        source[0] = {k:v for k,v in source[0].items() if k != "markPrice"}
    elif issue == "nan":
        source[0] = {**source[0], "fundingRate": "NaN"}
    elif issue == "large_rate":
        source[0] = {**source[0], "fundingRate": "1.5"}
    elif issue == "missing_event":
        source = [r for i,r in enumerate(source) if i != 2]
    elif issue == "out_of_order":
        source[0], source[1] = source[1], source[0]
    with pytest.raises(ValueError):
        survey(cfg, get=fake_get(source, []))


def test_funding_sign_and_mark_price_for_fixed_base_short_and_long():
    entries = events(hours=24)
    start = pd.Timestamp("2024-01-01T04:00:00Z")
    exit_ = pd.Timestamp("2024-01-01T20:00:00Z")
    short = settlement_return(-1, 100.0, start, exit_, entries)
    long = settlement_return(1, 100.0, start, exit_, entries)
    assert short["settlements"] == 2
    assert short["funding_return"] == pytest.approx(+0.00005)
    assert long["funding_return"] == pytest.approx(-0.00005)
    assert short["coverage_asserted"] is False

    # Funding mark differs from the entry price: settle on mark, not on
    # the position's initial notional or last trade print.
    changed = [dict(e) for e in entries]
    changed[1]["markPrice"] = "200"
    assert settlement_return(-1, 100, start, exit_, changed)["funding_return"] == pytest.approx(0.0)


def test_boundary_settlements_never_silently_counted_or_discarded():
    entries = events(hours=24)
    with pytest.raises(ValueError, match="ambiguous"):
        settlement_return(-1, 100, pd.Timestamp("2024-01-01T08:00Z"), pd.Timestamp("2024-01-01T20:00Z"), entries)
    with pytest.raises(ValueError, match="ambiguous"):
        settlement_return(-1, 100, pd.Timestamp("2024-01-01T02:00Z"), pd.Timestamp("2024-01-01T16:00Z"), entries)
    with pytest.raises(ValueError, match="no funding settlement evidence"):
        settlement_return(-1, 100, pd.Timestamp("2024-01-01T02:00Z"), pd.Timestamp("2024-01-01T16:00Z"), [])


def test_invalid_or_unordered_accounting_events_fail_closed():
    entries = events(hours=24)
    ts0 = pd.Timestamp("2024-01-01T01:00:00Z")
    ts1 = pd.Timestamp("2024-01-01T23:00:00Z")
    with pytest.raises(ValueError, match="chronologically ordered"):
        settlement_return(-1, 100, ts0, ts1, list(reversed(entries)))
    with pytest.raises(ValueError, match="reference"):
        settlement_return(-1, 0, ts0, ts1, entries)


def test_plan_cannot_be_replayed_with_a_different_holdout(tmp_path):
    cfg = setup(tmp_path)
    source = events()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(survey(cfg, get=fake_get(source, []))))
    partition = tmp_path / "results" / "crash_partition.json"
    partition.write_text(json.dumps({
        "version": 1, "holdout_start": "2024-01-04T00:00:00Z",
    }))
    with pytest.raises(ValueError, match="protected holdout"):
        apply(cfg, plan, get=fake_get(source, []))


def test_survey_validates_all_paginated_pages_and_bounds(tmp_path):
    cfg = setup(tmp_path, boundary="2024-02-12T00:00:00Z")
    rows = events(hours=42*24, interval=1)
    calls = []
    def get(url):
        q = parse_qs(urlsplit(url).query)
        start = int(q["startTime"][0])
        end = int(q["endTime"][0])
        assert end == int(pd.Timestamp("2024-02-12T00:00:00Z").value // 1_000_000) - 1
        calls.append(start)
        return [row for row in rows if start <= row["fundingTime"] <= end][:1000]
    plan = survey(cfg, get=get)
    assert len(calls) == 2
    assert len(plan["pages"]) == 2
    assert [p["rows"] for p in plan["pages"]] == [1000, 8]
    assert plan["event_count"] == 1008
