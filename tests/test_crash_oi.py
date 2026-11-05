"""Open-interest quantity is distinct from mark-sensitive OI notional.

These tests are offline, never open the reserved holdout, and exercise
both the new parser and the explicit pre-holdout migration gate.
"""

import io
import zipfile
from datetime import date

import numpy as np
import pandas as pd
import pytest

from lab import crash_data as cd


def zipped(csv: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("metrics.csv", csv)
    return buf.getvalue()


HEADER = "create_time,symbol,sum_open_interest,sum_open_interest_value\n"
DATA = (
    "2024-01-01 00:05:00,BTCUSDT,100,4200000\n"
    "2024-01-01 00:10:00,BTCUSDT,105,3990000\n"
)


def legacy_file(root, day="2024-01-01"):
    folder = root / "oi" / "BTCUSDT"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (day + ".parquet")
    frame = cd.parse_metrics(zipped(HEADER + DATA)).drop(columns="oi_qty")
    frame.to_parquet(path, index=False)
    return path, frame


def test_contract_quantity_and_notional_differ_on_falling_mark():
    o = cd.parse_metrics(zipped(HEADER + DATA))
    assert list(o.columns) == ["ts", "oi_qty", "oi_value"]
    assert o["oi_qty"].tolist() == [100, 105]
    assert o["oi_value"].tolist() == [4.2e6, 3.99e6]
    assert o["oi_qty"].iloc[-1] > o["oi_qty"].iloc[0]
    assert o["oi_value"].iloc[-1] < o["oi_value"].iloc[0]


@pytest.mark.parametrize("qty", ["0", "-1", "NaN"])
def test_parser_rejects_invalid_quantities(qty):
    raw = HEADER + f"2024-01-01 00:05:00,BTCUSDT,{qty},4200000\n"
    with pytest.raises(ValueError, match=r"quantity|nonfinite"):
        cd.parse_metrics(zipped(raw))


def test_legacy_cache_blocks_study_instead_of_falling_back_to_notional(tmp_path):
    legacy_file(tmp_path)
    with pytest.raises(ValueError, match="contract quantity missing"):
        cd.load(tmp_path, "oi", "BTCUSDT")


def test_pre_holdout_migration_is_dry_run_by_default(tmp_path):
    path, frame = legacy_file(tmp_path)
    called = []

    def downloader(_url):
        called.append(_url)
        return zipped(HEADER + DATA)

    pending = cd.migrate_oi_quantity(
        tmp_path, "BTCUSDT", before=date(2024, 1, 2), get=downloader
    )
    assert pending == [path.name] and called == []
    assert pd.read_parquet(path).equals(frame)


def test_migration_preserves_original_notional_and_validates_archive(tmp_path):
    path, frame = legacy_file(tmp_path)
    updated = cd.migrate_oi_quantity(
        tmp_path, "BTCUSDT", before=date(2024, 1, 2),
        apply=True, get=lambda _url: zipped(HEADER + DATA),
    )
    assert updated == [path.name]
    out = pd.read_parquet(path)
    assert out["oi_value"].equals(frame["oi_value"])
    assert out["oi_qty"].tolist() == [100, 105]
    assert cd.load(tmp_path, "oi", "BTCUSDT")["oi_qty"].tolist() == [100, 105]
    assert cd.migrate_oi_quantity(
        tmp_path, "BTCUSDT", before=date(2024, 1, 2), get=lambda _: None
    ) == []


def test_post_boundary_file_never_downloaded_or_changed(tmp_path):
    path, before = legacy_file(tmp_path, "2024-01-01")
    # The date is attached to the filename, but raw record timestamps are
    # irrelevant to the pre-boundary selection for this negative test.
    called = []

    def get(url):
        called.append(url)
        raise AssertionError("holdout archive was downloaded")

    assert cd.migrate_oi_quantity(
        tmp_path, "BTCUSDT", before=date(2024, 1, 1), apply=True, get=get
    ) == []
    assert called == []
    assert pd.read_parquet(path).equals(before)


def test_archive_mismatch_preserves_legacy_cache(tmp_path):
    path, original = legacy_file(tmp_path)
    changed = (
        "2024-01-01 00:05:00,BTCUSDT,100,4200000\n"
        "2024-01-01 00:10:00,BTCUSDT,105,3000000\n"
    )
    with pytest.raises(ValueError, match="source notional"):
        cd.migrate_oi_quantity(
            tmp_path, "BTCUSDT", before=date(2024, 1, 2),
            apply=True, get=lambda _: zipped(HEADER + changed),
        )
    assert pd.read_parquet(path).equals(original)
    assert not list(path.parent.glob("*.tmp"))


def test_crash_study_rejects_notional_only_frame():
    from lab import crash_study as cs

    k = pd.DataFrame({
        "ts": pd.date_range("2024-01-01", periods=5, tz="UTC", freq="min"),
        "close": np.full(5, 100.0),
    })
    oi = cd.parse_metrics(zipped(HEADER + DATA)).drop(columns="oi_qty")
    with pytest.raises(ValueError, match="contract-quantity"):
        cs.bars(k, oi)



def test_research_only_oi_load_does_not_read_legacy_holdout(tmp_path):
    p, old = legacy_file(tmp_path, "2024-01-01")
    early = old.assign(oi_qty=[100.0, 105.0])
    early.to_parquet(p, index=False)
    holdout = p.parent / "2024-01-02.parquet"
    old.to_parquet(holdout, index=False)

    research = cd.load(
        tmp_path, "oi", "BTCUSDT",
        before=pd.Timestamp("2024-01-02", tz="UTC"),
    )
    assert research["oi_qty"].tolist() == [100, 105]
    # A real holdout evaluation still rejects the unmigrated file.
    with pytest.raises(ValueError, match="contract quantity missing"):
        cd.load(tmp_path, "oi", "BTCUSDT")


def test_oi_research_only_cutoff_requires_timezone_aware_timestamp(tmp_path):
    legacy_file(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        cd.load(tmp_path, "oi", "BTCUSDT", before=pd.Timestamp("2024-01-02"))
