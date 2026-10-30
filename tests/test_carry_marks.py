"""Mark bars are closed, coherent observations; trade candles and empty API answers are not substitutes."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from lab import carry_data as cd
from lab import carry_marks as cm
from tests.test_carry import T0, H


class MarkAPI:
    def __init__(self, hours=12, missing=()):
        self.opens = [int(T0.value // 1_000_000) + i * H for i in range(hours) if i not in missing]
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        query = parse_qs(urlparse(url).query)
        bybit = "bybit" in url
        assert ("mark-price-kline" if bybit else "markPriceKlines") in url
        assert query["interval"] == ["60" if bybit else "1h"]
        start = int(query["start" if bybit else "startTime"][0])
        end = int(query["end" if bybit else "endTime"][0])
        opens = sorted((t for t in self.opens if start <= t <= end), reverse=bybit)[: int(query["limit"][0])]
        rows = []
        for t in opens:
            price = 100 + (t - int(T0.value // 1_000_000)) / H
            row = [str(t) if bybit else t, str(price), str(price + 5), str(price - 5), str(price + 1)]
            rows.append(row if bybit else [*row, "0", t + H - 1, "0", 60, "0", "0", "0"])
        return {"retCode": 0, "result": {"symbol": "BTCUSDT", "category": "linear", "list": rows}} if bybit else rows


def fetch(root, api, *, venue="binance", start=0, end=12, **io):
    return cm.update(
        root,
        venue,
        "BTCUSDT",
        T0 + pd.Timedelta(hours=start),
        T0 + pd.Timedelta(hours=end),
        get=api,
        pause=lambda: None,
        sleep=lambda _: None,
        **io,
    )


def rewrite(root, mutate, *, venue="binance"):
    path = cm.path_of(root, venue, "BTCUSDT")
    table = pq.read_table(path)
    pq.write_table(mutate(table), path)


@pytest.mark.parametrize("venue", cd.VENUES)
def test_native_page_directions_and_closed_hour_boundaries(venue, tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "PAGE_LIMIT", 3)
    api = MarkAPI(hours=14)
    assert fetch(tmp_path, api, venue=venue, end=12.5) == (12, 12)
    frame = cm.load(tmp_path, venue, "BTCUSDT")
    assert frame.index.equals(pd.date_range(T0 + pd.Timedelta(hours=1), periods=12, freq="h", name="ts"))
    assert frame.iloc[0].tolist() == [100, 105, 95, 101]
    assert frame.iloc[-1].tolist() == [111, 116, 106, 112]
    assert len(api.calls) == 4
    assert cm.coverage(tmp_path, venue, "BTCUSDT", T0, T0 + pd.Timedelta(hours=12.5))["complete_requested_window"]


@pytest.mark.parametrize("venue", cd.VENUES)
def test_resume_does_not_skip_the_next_hour_and_up_to_date_cache_makes_no_request(venue, tmp_path):
    api = MarkAPI()
    assert fetch(tmp_path, api, venue=venue, end=5) == (5, 5)
    assert fetch(tmp_path, api, venue=venue, end=10) == (5, 10)
    before = len(api.calls)
    assert fetch(tmp_path, api, venue=venue, end=10.9) == (0, 10)
    assert len(api.calls) == before
    assert cm.load(tmp_path, venue, "BTCUSDT").index[-1] == T0 + pd.Timedelta(hours=10)


@pytest.mark.parametrize("venue", cd.VENUES)
def test_internal_tail_and_leading_gaps_remain_explicit_and_are_repaired(venue, tmp_path):
    sparse = MarkAPI(hours=12, missing=[0, 1, 4, 5, 11])
    assert fetch(tmp_path, sparse, venue=venue) == (7, 7)
    report = cm.coverage(tmp_path, venue, "BTCUSDT", T0, T0 + pd.Timedelta(hours=12))
    assert report["leading_unverified_hours"] == 2
    assert report["missing_hours"] == 5 and report["state"] == "gappy"
    assert not report["complete_requested_window"] and not report["listing_verified"]
    assert [g["hours"] for g in report["gaps"]] == [2, 2, 1]
    complete = MarkAPI()
    assert fetch(tmp_path, complete, venue=venue) == (5, 12)
    assert len(complete.calls) == 3
    assert cm.coverage(tmp_path, venue, "BTCUSDT", T0, T0 + pd.Timedelta(hours=12))["complete_requested_window"]


def test_empty_answers_do_not_invent_prices_or_availability_and_remain_retryable(tmp_path):
    api = MarkAPI(hours=0)
    assert fetch(tmp_path, api) == (0, 0)
    assert fetch(tmp_path, api) == (0, 0)
    assert len(api.calls) == 2 and cm.load(tmp_path, "binance", "BTCUSDT").empty
    report = cm.coverage(tmp_path, "binance", "BTCUSDT", T0, T0 + pd.Timedelta(hours=12))
    assert report["state"] == "unavailable" and report["missing_hours"] == 12
    assert not report["funding_event_marks_verified"] and not report["liquidation_qualified"]


def test_trade_price_cache_never_serves_as_a_mark_cache(tmp_path):
    from tests.test_carry import write

    write(tmp_path, "binance", "perp", "close", "BTCUSDT", pd.Series([100.0], index=pd.DatetimeIndex([T0])))
    assert cm.load(tmp_path, "binance", "BTCUSDT").empty


def test_newer_mark_caches_do_not_move_the_strategy_holdout_boundary(tmp_path):
    from lab import carry_study as cs
    from tests.test_carry import write

    trade_end = T0 + pd.Timedelta(hours=2)
    write(tmp_path, "binance", "perp", "close", "BTCUSDT", pd.Series([100.0], index=pd.DatetimeIndex([trade_end])))
    fetch(tmp_path, MarkAPI(hours=800), end=800)
    assert cs._last_cached(tmp_path) == trade_end
    assert cs.boundary(tmp_path / "results", tmp_path) == (trade_end - pd.Timedelta(days=cs.HOLDOUT_DAYS)).floor("D")


def test_mark_only_data_cannot_initialize_a_strategy_holdout_partition(tmp_path):
    from lab import carry_study as cs

    fetch(tmp_path, MarkAPI(), end=3)
    with pytest.raises(ValueError, match="no carry data"):
        cs.boundary(tmp_path / "results", tmp_path)
    assert not (tmp_path / "results" / cs.PARTITION).exists()


def test_unaligned_since_and_timezone_conversion_use_only_fully_requested_closed_hours(tmp_path):
    api = MarkAPI()
    start = (T0 + pd.Timedelta(minutes=1)).tz_convert("Europe/Rome")
    end = (T0 + pd.Timedelta(hours=3, minutes=30)).tz_convert("Europe/Rome")
    assert cm.update(tmp_path, "binance", "BTCUSDT", start, end, get=api) == (2, 2)
    assert cm.load(tmp_path, "binance", "BTCUSDT").index.tolist() == [T0 + pd.Timedelta(hours=h) for h in (2, 3)]


@pytest.mark.parametrize("venue", cd.VENUES)
@pytest.mark.parametrize(
    "damage",
    [
        "duplicate",
        "unordered",
        "outside",
        "unaligned",
        "boolean-time",
        "truncated",
        "bad-high",
        "bad-low",
        "nan",
        "zero",
        "boolean-price",
    ],
)
def test_malformed_native_pages_cannot_replace_an_existing_cache(venue, damage, tmp_path):
    fetch(tmp_path, MarkAPI(), venue=venue, end=3)
    path = cm.path_of(tmp_path, venue, "BTCUSDT")
    before = path.read_bytes()
    api = MarkAPI()

    def bad(url):
        response = api(url)
        page = response["result"]["list"] if venue == "bybit" else response
        if damage == "duplicate":
            page[1] = page[0][:]
        elif damage == "unordered":
            page.reverse()
        elif damage == "outside":
            page[0][0] = int(T0.value // 1_000_000) - H
        elif damage == "unaligned":
            page[0][0] = int(page[0][0]) + 1
        elif damage == "boolean-time":
            page[0][0] = True
        elif damage == "truncated":
            page[0] = page[0][:4]
        elif damage == "bad-high":
            page[0][2] = "1"
        elif damage == "bad-low":
            page[0][3] = "9999999"
        else:
            page[0][4] = {"nan": "NaN", "zero": "0", "boolean-price": True}[damage]
        return response

    with pytest.raises(ValueError, match=r"history|mark"):
        fetch(tmp_path, bad, venue=venue, end=8)
    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".tmp").exists()


@pytest.mark.parametrize("venue", cd.VENUES)
def test_repeated_full_pages_stop_instead_of_looping_or_publishing(venue, tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "PAGE_LIMIT", 3)
    fetch(tmp_path, MarkAPI(), venue=venue, end=2)
    path = cm.path_of(tmp_path, venue, "BTCUSDT")
    before = path.read_bytes()
    first = None
    calls = 0

    def stuck(url):
        nonlocal first, calls
        calls += 1
        if first is None:
            first = MarkAPI()(url)
        return first

    with pytest.raises(ValueError, match="out-of-window"):
        fetch(tmp_path, stuck, venue=venue, end=10)
    assert calls == 2 and path.read_bytes() == before


@pytest.mark.parametrize("failure", ["page", "write", "fsync", "rename"])
def test_prepublication_failure_keeps_all_old_columns_and_cleans_temporary_files(failure, tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "PAGE_LIMIT", 3)
    fetch(tmp_path, MarkAPI(), end=2)
    path = cm.path_of(tmp_path, "binance", "BTCUSDT")
    before = path.read_bytes()
    api = MarkAPI()

    def fail(*args, **kwargs):
        raise OSError("injected prepublication failure")

    if failure == "write":
        monkeypatch.setattr(cm.pq, "write_table", fail)
    elif failure == "fsync":
        monkeypatch.setattr(cm.os, "fsync", fail)
    elif failure == "rename":
        monkeypatch.setattr(Path, "replace", fail)
    else:
        get = api

        def api(url):
            if get.calls:
                fail()
            return get(url)

    with pytest.raises(OSError, match="injected"):
        fetch(tmp_path, api, end=10)
    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".tmp").exists()
    assert len(cm.load(tmp_path, "binance", "BTCUSDT")) == 2


@pytest.mark.parametrize("damage", ["missing", "wrong-venue", "wrong-symbol", "trade-price", "duplicate-key"])
def test_missing_conflicting_or_corrupt_cache_identity_cannot_be_relabelled(damage, tmp_path):
    fetch(tmp_path, MarkAPI(), end=3)

    def mutate(table):
        metadata = dict(table.schema.metadata)
        identity = json.loads(metadata[cm.METADATA_KEY])
        if damage == "missing":
            metadata.pop(cm.METADATA_KEY)
        elif damage == "duplicate-key":
            metadata[cm.METADATA_KEY] = b'{"model": "a", "model": "b"}'
        else:
            key, value = {
                "wrong-venue": ("venue", "bybit"),
                "wrong-symbol": ("symbol", "ETHUSDT"),
                "trade-price": ("price_kind", "trade"),
            }[damage]
            metadata[cm.METADATA_KEY] = json.dumps({**identity, key: value}).encode()
        return table.replace_schema_metadata(metadata)

    rewrite(tmp_path, mutate)
    with pytest.raises(ValueError, match=r"identity|duplicate JSON"):
        fetch(tmp_path, MarkAPI(), end=4)


@pytest.mark.parametrize(
    "damage", ["duplicate", "unordered", "naive", "unaligned", "nan", "negative", "bounds", "missing-column", "boolean"]
)
def test_invalid_cached_observations_block_update_instead_of_being_silently_fixed(damage, tmp_path):
    import pyarrow as pa

    fetch(tmp_path, MarkAPI(), end=3)

    def mutate(table):
        frame = table.to_pandas()
        if damage == "duplicate":
            frame.loc[1, "ts"] = frame.loc[0, "ts"]
        elif damage == "unordered":
            frame = frame.iloc[::-1]
        elif damage == "naive":
            frame["ts"] = frame.ts.dt.tz_localize(None)
        elif damage == "unaligned":
            frame["ts"] += pd.Timedelta(seconds=1)
        elif damage == "missing-column":
            frame = frame.drop(columns="high")
        elif damage == "boolean":
            frame["close"] = True
        else:
            frame.loc[1, "high"] = {"nan": np.nan, "negative": -1, "bounds": 1}[damage]
        return pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata(table.schema.metadata)

    rewrite(tmp_path, mutate)
    with pytest.raises(ValueError, match="mark history"):
        fetch(tmp_path, MarkAPI(), end=4)


def test_reader_bound_to_old_inode_cannot_mix_old_ohlc_with_new_snapshot(tmp_path, monkeypatch):
    fetch(tmp_path, MarkAPI(), end=3)
    original = cm.pq.read_table
    once = False

    def read(source, *args, **kwargs):
        nonlocal once
        if not once:
            once = True
            fetch(tmp_path, MarkAPI(), end=5)
        return original(source, *args, **kwargs)

    monkeypatch.setattr(cm.pq, "read_table", read)
    assert len(cm.load(tmp_path, "binance", "BTCUSDT")) == 3
    assert len(cm.load(tmp_path, "binance", "BTCUSDT")) == 5


def test_competing_writers_reload_the_current_snapshot_and_preserve_each_others_rows(tmp_path):
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(fetch, tmp_path, MarkAPI(), start=a, end=b) for a, b in ((0, 5), (3, 9))]
        results = [f.result() for f in futures]
    assert sum(added for added, _ in results) == 9
    frame = cm.load(tmp_path, "binance", "BTCUSDT")
    assert len(frame) == 9 and frame.index.is_unique


@pytest.mark.parametrize(
    "venue,symbol", [("spot", "BTCUSDT"), ("binance", "../BTCUSDT"), ("bybit", "BTCUSD"), ("binance", "btcusdt")]
)
def test_invalid_identities_fail_before_network_or_file_access(venue, symbol, tmp_path):
    with pytest.raises(ValueError, match="uppercase USDT"):
        cm.update(tmp_path, venue, symbol, T0, T0 + pd.Timedelta(hours=1), get=lambda _: pytest.fail("network"))


@pytest.mark.parametrize("start,end", [(T0.tz_localize(None), T0), (T0 + pd.Timedelta(hours=1), T0), (pd.NaT, T0)])
def test_invalid_windows_fail_before_network_or_cache_mutation(start, end, tmp_path):
    with pytest.raises(ValueError, match="window"):
        cm.update(tmp_path, "binance", "BTCUSDT", start, end, get=lambda _: pytest.fail("network"))


def test_bounded_coverage_ignores_future_cache_rows_and_empty_windows_do_not_claim_completeness(tmp_path):
    fetch(tmp_path, MarkAPI(), end=10)
    report = cm.coverage(tmp_path, "binance", "BTCUSDT", T0 + pd.Timedelta(hours=3), T0 + pd.Timedelta(hours=6))
    assert report["requested_hours"] == report["observed_hours"] == 3
    report = cm.coverage(tmp_path, "binance", "BTCUSDT", T0, T0)
    assert report["state"] == "empty-window" and not report["complete_requested_window"]


@pytest.mark.parametrize("damage", ["category", "symbol", "error", "close-time"])
def test_venue_specific_response_contracts_are_checked(damage, tmp_path):
    venue = "binance" if damage == "close-time" else "bybit"

    def bad(url):
        response = MarkAPI()(url)
        if damage == "close-time":
            response[0][6] += 1
        elif damage == "error":
            response["retCode"] = 10001
        else:
            response["result"][damage] = "invalid"
        return response

    with pytest.raises(ValueError, match="mark"):
        fetch(tmp_path, bad, venue=venue)


def test_transient_rate_limit_uses_existing_retry_policy_without_losing_data(tmp_path):
    api = MarkAPI()
    seen = []

    def limited(url):
        seen.append(url)
        if len(seen) == 1:
            raise HTTPError(url, 429, "limited", {}, None)
        return api(url)

    assert fetch(tmp_path, limited, end=3) == (3, 3)
    assert len(seen) == 2 and seen[0] == seen[1]


@pytest.mark.parametrize("missing", [(), (1,)])
def test_actual_cli_writes_bounded_coverage_and_returns_nonzero_for_gaps(missing, tmp_path, monkeypatch, capsys):
    from lab.cli import main
    from lab.persistence import read_json
    from tests.conftest import REPO

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    original = cm.download
    monkeypatch.setattr(
        cm,
        "download",
        lambda *args, **kwargs: original(*args, get=MarkAPI(hours=4, missing=missing), pause=lambda: None),
    )
    status = main(
        [
            "--config",
            str(tmp_path / "lab.toml"),
            "carry",
            "fetch-marks",
            "--symbols",
            "btcusdt",
            "--venues",
            "binance",
            "bybit",
            "--since",
            "2024-01-01",
            "--until",
            "2024-01-01T04:30:00Z",
        ]
    )
    assert status == bool(missing)
    for venue in cd.VENUES:
        report = read_json(tmp_path / "data" / "carry" / f"{venue}-perp-mark-coverage-BTCUSDT.json")
        assert report["requested_hours"] == 4 and report["missing_hours"] == len(missing)
    assert "studies use declared valuation settings (trade-close by default)" in capsys.readouterr().out
    assert not list((tmp_path / "results").glob("research_attempts/*.json"))  # fetching is not a strategy evaluation


def test_cli_rejects_future_cutoffs_before_any_request(tmp_path, monkeypatch):
    from lab.cli import main
    from tests.conftest import REPO

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    monkeypatch.setattr(cm, "update", lambda *args, **kwargs: pytest.fail("future cutoff must fail before download"))
    with pytest.raises(SystemExit, match="future"):
        main(["--config", str(tmp_path / "lab.toml"), "carry", "fetch-marks", "--until", "2200-01-01"])


def test_cli_failure_replaces_stale_complete_report_and_continues_other_venues(tmp_path, monkeypatch, capsys):
    from lab.cli import main
    from lab.persistence import atomic_json, read_json
    from tests.conftest import REPO

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    report_path = tmp_path / "data" / "carry" / "binance-perp-mark-coverage-BTCUSDT.json"
    atomic_json(report_path, {"state": "complete-requested-window", "complete_requested_window": True})
    original = cm.download

    def download(venue, *args, **kwargs):
        if venue == "binance":
            raise HTTPError("https://fapi.binance.com", 451, "unavailable", {}, None)
        return original(venue, *args, get=MarkAPI(hours=4), pause=lambda: None)

    monkeypatch.setattr(cm, "download", download)
    assert (
        main(
            [
                "--config",
                str(tmp_path / "lab.toml"),
                "carry",
                "fetch-marks",
                "--symbols",
                "BTCUSDT",
                "--since",
                "2024-01-01",
                "--until",
                "2024-01-01T04:00:00Z",
            ]
        )
        == 1
    )
    report = read_json(report_path)
    assert report["state"] == "fetch-error" and not report["complete_requested_window"]
    assert "451" in report["error"]
    assert len(cm.load(tmp_path / "data" / "carry", "bybit", "BTCUSDT")) == 4
    assert "unavailable" in capsys.readouterr().out
