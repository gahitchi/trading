"""Source-pinned candle tail: bounded, append-only, no protected archive reads."""

import hashlib
import io
import json
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from lab import crash_data
from lab.crash_kline_tail import apply, audit, main, survey
from lab.crash_quality_gate import audit_quality


def zipped_day(day: str, *, bad=False):
    beginning = pd.Timestamp(day, tz="UTC")
    lines = []
    for i in range(1440):
        start = beginning + pd.Timedelta(minutes=i)
        ms = int(start.value // 1_000_000)
        high = 5 if bad and i == 40 else 101
        lines.append(f"{ms},100,{high},99,100,1,0,500,0,0,250,0")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("klines.csv", "\n".join(lines) + "\n")
    return buf.getvalue()


def setup(tmp_path):
    cfg = tmp_path / "lab.toml"
    cfg.write_text('[paths]\ndata = "data"\nresults = "results"\n')
    result = tmp_path / "results"
    result.mkdir()
    (result / "crash_partition.json").write_text(
        json.dumps({"version": 1, "holdout_start": "2026-05-03T00:00:00Z"})
    )
    folder = tmp_path / "data" / "crash" / "klines" / "BTCUSDT"
    folder.mkdir(parents=True)
    (folder / "2026-05.parquet").write_bytes(b"monthly-holdout-is-protected")
    (folder / "2026-05-03.parquet").write_bytes(b"daily-holdout-is-protected")
    blobs = {
        crash_data.url_of("klines", "BTCUSDT", day): zipped_day(day)
        for day in ("2026-05-01", "2026-05-02")
    }
    return cfg, folder, blobs


def test_dry_run_is_exactly_two_days_and_does_not_write_cache(tmp_path):
    cfg, folder, blobs = setup(tmp_path)
    requested = []

    def get(url):
        requested.append(url)
        return blobs[url]

    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    plan = survey(cfg, get=get)
    assert plan["holdout_start"] == "2026-05-03"
    assert [item["day"] for item in plan["archives"]] == ["2026-05-01", "2026-05-02"]
    assert [item["rows"] for item in plan["archives"]] == [1440, 1440]
    assert [item["archive_sha256"] for item in plan["archives"]] == [
        hashlib.sha256(blobs[url]).hexdigest() for url in requested
    ]
    assert len(requested) == 2
    assert not any("2026-05-03" in url or "monthly" in url for url in requested)
    assert before == {p.name: p.read_bytes() for p in folder.iterdir()}


def test_pinned_apply_is_append_only_recoverable_and_independently_auditable(tmp_path):
    cfg, folder, blobs = setup(tmp_path)
    source_plan = tmp_path / "pinned.json"
    source_plan.write_text(json.dumps(survey(cfg, get=blobs.__getitem__)))
    protected = {p.name: p.read_bytes() for p in folder.iterdir()}
    report = apply(cfg, source_plan, get=blobs.__getitem__)
    assert [x["status"] for x in report["days"]] == ["committed", "committed"]
    assert audit(cfg, source_plan)["ok"]
    again = apply(cfg, source_plan, get=blobs.__getitem__)
    assert [x["status"] for x in again["days"]] == [
        "verified-existing", "verified-existing"
    ]
    assert protected == {name: (folder / name).read_bytes() for name in protected}
    frame = crash_data.load(
        cfg.parent / "data" / "crash", "klines", "BTCUSDT",
        before=pd.Timestamp("2026-05-03T00:00:00Z"),
    )
    assert len(frame) == 2880
    assert frame["ts"].iloc[-1] == pd.Timestamp("2026-05-03T00:00:00Z")

    # Missing sidecar after an interrupted commit is reconciled only
    # against the exact immutable source before acceptance.
    (folder / "2026-05-02.parquet.preholdout.json").unlink()
    assert not audit(cfg, source_plan)["ok"]
    again = apply(cfg, source_plan, get=blobs.__getitem__)
    assert again["days"][1]["status"] == "reconciled"
    assert audit(cfg, source_plan)["ok"]


def test_changed_source_archive_rejected_before_any_writes(tmp_path):
    cfg, folder, blobs = setup(tmp_path)
    source_plan = tmp_path / "pinned.json"
    source_plan.write_text(json.dumps(survey(cfg, get=blobs.__getitem__)))
    url = crash_data.url_of("klines", "BTCUSDT", "2026-05-02")
    changed = dict(blobs)
    changed[url] = zipped_day("2026-05-02", bad=True)
    with pytest.raises(ValueError, match="invalid candle OHLC"):
        apply(cfg, source_plan, get=changed.__getitem__)
    assert not (folder / "2026-05-01.parquet").exists()
    assert not (folder / "2026-05-02.parquet").exists()


def test_hash_changed_with_valid_candles_rejected_before_commit(tmp_path):
    cfg, folder, blobs = setup(tmp_path)
    source_plan = tmp_path / "pinned.json"
    source_plan.write_text(json.dumps(survey(cfg, get=blobs.__getitem__)))
    url = crash_data.url_of("klines", "BTCUSDT", "2026-05-02")
    changed = dict(blobs)
    # Same parsed content, different ZIP bytes, still fail source pin.
    changed[url] = blobs[url] + b"trailing archive bytes"
    with pytest.raises(ValueError, match="SHA-256"):
        apply(cfg, source_plan, get=changed.__getitem__)
    assert not (folder / "2026-05-01.parquet").exists()


def test_tampered_plan_cannot_access_holdout(tmp_path):
    cfg, folder, blobs = setup(tmp_path)
    plan = survey(cfg, get=blobs.__getitem__)
    plan["archives"][-1]["day"] = "2026-05-03"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(plan))
    calls = []

    def deny(url):
        calls.append(url)
        raise AssertionError("must never fetch protected archive")

    with pytest.raises(ValueError, match="protected dates"):
        apply(cfg, path, get=deny)
    assert calls == []
    assert (folder / "2026-05-03.parquet").read_bytes() == b"daily-holdout-is-protected"


def test_missing_cache_manifest_detected_by_read_only_audit(tmp_path):
    cfg, folder, blobs = setup(tmp_path)
    plan = tmp_path / "pinned.json"
    plan.write_text(json.dumps(survey(cfg, get=blobs.__getitem__)))
    assert not audit(cfg, plan)["ok"]
    apply(cfg, plan, get=blobs.__getitem__)
    target = folder / "2026-05-02.parquet"
    target.write_bytes(b"corrupt")
    r = audit(cfg, plan)
    assert not r["ok"] and any("2026-05-02" in issue for issue in r["issues"])


def test_plan_cli_and_require_pinning(tmp_path, capsys, monkeypatch):
    cfg, folder, blobs = setup(tmp_path)
    monkeypatch.setattr(crash_data, "get_bytes", blobs.__getitem__)
    plan_path = tmp_path / "source-plan.json"
    assert main(["--config", str(cfg), "--plan-out", str(plan_path)]) == 0
    assert json.loads(plan_path.read_text())["kind"].endswith("-v1")
    with pytest.raises(SystemExit):
        main(["--config", str(cfg), "--apply"])
    assert main(["--config", str(cfg), "--apply", "--plan", str(plan_path)]) == 0
    assert main(["--config", str(cfg), "--audit", "--plan", str(plan_path)]) == 0


def test_tail_boundary_candle_dropped_from_research_quality_report(tmp_path):
    cfg, folder, blobs = setup(tmp_path)
    plan = tmp_path / "pinned.json"
    plan.write_text(json.dumps(survey(cfg, get=blobs.__getitem__)))
    apply(cfg, plan, get=blobs.__getitem__)
    oi_dir = cfg.parent / "data" / "crash" / "oi" / "BTCUSDT"
    oi_dir.mkdir(parents=True)
    ts = pd.date_range("2026-05-01", periods=576, freq="5min", tz="UTC")
    pd.DataFrame({
        "ts": ts,
        "oi_qty": [1000.0] * len(ts),
        "oi_value": [500000.0] * len(ts),
    }).to_parquet(oi_dir / "2026-05-01.parquet", index=False)

    report = audit_quality(cfg, window_min=60)
    assert report["klines"]["boundary_stamped_source_rows_excluded"] == 1
    assert report["klines"]["trailing_to_boundary_minutes"] == 0
    assert report["klines"]["loaded_rows"] == 2879
    assert report["klines"]["last"] == "2026-05-02T23:59:00+00:00"
