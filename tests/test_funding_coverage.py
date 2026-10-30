"""Unknown retrieval periods cannot silently turn into zero funding or earlier information."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pyarrow.parquet as pq
import pytest

from lab import carry_data as cd
from lab import carry_study as cs
from lab.trials import InputEvidence, history
from tests.test_carry import T0, H, flat_legs, market, write


class FundingAPI:
    def __init__(self, hours=(0, 8, 16, 24), rate=0.0001):
        self.events = {int(T0.value // 1_000_000) + h * H: rate for h in hours}
        self.calls = []

    def __call__(self, url):
        q = parse_qs(urlparse(url).query)
        a, b = (int(q[k][0]) for k in ("startTime", "endTime"))
        self.calls.append((a, b))
        bybit = "bybit" in url
        events = sorted((t for t in self.events if a <= t <= b), reverse=bybit)[: int(q["limit"][0])]
        rows = [
            {"fundingRateTimestamp" if bybit else "fundingTime": str(t), "fundingRate": str(self.events[t])}
            for t in events
        ]
        return {"retCode": 0, "result": {"list": rows}} if bybit else rows


def fetch(root, api, *, venue="binance", start=0, end=24, **kw):
    return cd.update(
        root,
        venue,
        "perp",
        "funding",
        "BTCUSDT",
        T0 + pd.Timedelta(hours=start),
        T0 + pd.Timedelta(hours=end),
        get=api,
        pause=lambda: None,
        **kw,
    )


def change_metadata(root, venue="binance", mutate=None):
    path = cd.path_of(root, venue, "perp", "funding", "BTCUSDT")
    table = pq.read_table(path)
    metadata = dict(table.schema.metadata or {})
    value = json.loads(metadata[cd.FUNDING_COVERAGE_KEY]) if cd.FUNDING_COVERAGE_KEY in metadata else None
    value = mutate(value)
    if value is None:
        metadata.pop(cd.FUNDING_COVERAGE_KEY, None)
    else:
        metadata[cd.FUNDING_COVERAGE_KEY] = value if isinstance(value, bytes) else json.dumps(value).encode()
    pq.write_table(table.replace_schema_metadata(metadata), path)


def test_legacy_funding_without_query_coverage_cannot_report_a_study(tmp_path):
    market(tmp_path, days=10)
    path = cd.path_of(tmp_path, "binance", "perp", "funding", "BTCUSDT")
    frame = pd.read_parquet(path)
    frame.to_parquet(path, index=False)  # strip the synthetic fixture's declared source coverage
    # The old cache format has rates but no evidence of which zero-event periods were queried.
    with pytest.raises(ValueError, match="funding retrieval coverage"):
        cs.run(tmp_path, ["BTCUSDT"], cs.CarrySettings(lookback_h=24), T0 + pd.Timedelta(days=11))


def test_delayed_settlement_is_unavailable_to_the_preceding_hour_decision():
    lg = flat_legs()
    event = T0 + pd.Timedelta(hours=8, minutes=30)
    h = cs.hourly(replace(lg, funding=pd.Series([0.01], index=pd.DatetimeIndex([event]))))
    assert h.loc[T0 + pd.Timedelta(hours=8), "funding"] == 0
    assert h.loc[T0 + pd.Timedelta(hours=9), "funding"] == pytest.approx(0.01)


@pytest.mark.parametrize("delay_ms,bucket", [(0, 8), (1, 9)])
def test_exact_hour_and_millisecond_delayed_events_have_distinct_causal_buckets(delay_ms, bucket):
    lg = flat_legs()
    event = T0 + pd.Timedelta(hours=8, milliseconds=delay_ms)
    h = cs.hourly(replace(lg, funding=pd.Series([0.01], index=pd.DatetimeIndex([event]))))
    assert h["funding"].sum() == pytest.approx(0.01)
    assert h.loc[T0 + pd.Timedelta(hours=bucket), "funding"] == pytest.approx(0.01)


def test_a_delayed_event_can_trigger_an_entry_but_cannot_be_earned_retroactively():
    lg = flat_legs()
    events = pd.DatetimeIndex([T0 + pd.Timedelta(hours=h, minutes=30) for h in (8, 9)])
    h = cs.hourly(replace(lg, funding=pd.Series(0.01, index=events)))
    result = cs.backtest(h, cs.CarrySettings(lookback_h=1, rebalance_h=1, entry_apr=0))
    assert result.loc[T0 + pd.Timedelta(hours=8), "w"] == 0
    assert result.loc[T0 + pd.Timedelta(hours=9), "w"] == 1
    assert result.loc[T0 + pd.Timedelta(hours=9), "funding"] == 0
    assert result["funding"].sum() == pytest.approx(0.01)


@pytest.mark.parametrize("venue", ["binance", "bybit"])
def test_query_tail_advances_past_zero_event_hours_and_up_to_date_cache_needs_no_request(tmp_path, venue):
    api = FundingAPI()
    assert fetch(tmp_path, api, venue=venue, end=5) == (1, 1)
    assert fetch(tmp_path, api, venue=venue, end=10) == (1, 2)
    assert api.calls[1][0] == int((T0 + pd.Timedelta(hours=5)).value // 1_000_000) + 1
    rates, coverage = cd.load_funding(tmp_path, venue, "BTCUSDT")
    assert len(rates) == 2 and coverage == ((int(T0.value), int((T0 + pd.Timedelta(hours=10)).value)),)
    assert fetch(tmp_path, api, venue=venue, end=10) == (0, 2)
    assert len(api.calls) == 2


def test_prefix_and_tail_queries_fill_only_unqueried_intervals(tmp_path):
    api = FundingAPI()
    assert fetch(tmp_path, api, start=8, end=16) == (2, 2)
    assert fetch(tmp_path, api) == (2, 4)
    base = int(T0.value // 1_000_000)
    assert api.calls[1:] == [(base, base + 8 * H - 1), (base + 16 * H + 1, base + 24 * H)]
    assert cd.load_funding(tmp_path, "binance", "BTCUSDT")[1] == (
        (int(T0.value), int((T0 + pd.Timedelta(hours=24)).value)),
    )


def test_legacy_cache_is_requeried_instead_of_inventing_coverage_and_a_hole_is_recovered(tmp_path):
    api = FundingAPI()
    idx = pd.DatetimeIndex([T0, T0 + pd.Timedelta(hours=16)])
    write(tmp_path, "binance", "perp", "funding", "BTCUSDT", pd.Series(0.0001, index=idx))
    assert cd.load_funding(tmp_path, "binance", "BTCUSDT")[1] is None
    assert fetch(tmp_path, api, end=16) == (1, 3)
    assert api.calls == [(int(T0.value // 1_000_000), int((T0 + pd.Timedelta(hours=16)).value // 1_000_000))]
    assert cd.load_funding(tmp_path, "binance", "BTCUSDT")[1] is not None


def test_a_cached_event_missing_from_the_api_response_is_retained_as_a_conflict(tmp_path):
    write(tmp_path, "binance", "perp", "funding", "BTCUSDT", pd.Series([0.0001], index=pd.DatetimeIndex([T0])))
    path = cd.path_of(tmp_path, "binance", "perp", "funding", "BTCUSDT")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="contradicts cached settlements"):
        fetch(tmp_path, FundingAPI(hours=()))
    assert path.read_bytes() == before and cd.load_funding(tmp_path, "binance", "BTCUSDT")[1] is None


@pytest.mark.parametrize("venue,limit", [("binance", 1000), ("bybit", 200)])
def test_interrupted_pagination_does_not_publish_partial_rates_or_coverage(tmp_path, venue, limit):
    api = FundingAPI(hours=range(limit + 10))
    fetch(tmp_path, api, venue=venue, end=1)
    path = cd.path_of(tmp_path, venue, "perp", "funding", "BTCUSDT")
    before = path.read_bytes()
    calls = 0

    def interrupted(url):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected interruption")
        return api(url)

    with pytest.raises(RuntimeError, match="interruption"):
        fetch(tmp_path, interrupted, venue=venue, end=limit + 9)
    assert calls == 2 and path.read_bytes() == before


def test_failed_atomic_publication_retains_the_previous_query_boundary(tmp_path, monkeypatch):
    api = FundingAPI()
    fetch(tmp_path, api, end=5)
    path = cd.path_of(tmp_path, "binance", "perp", "funding", "BTCUSDT")
    before = path.read_bytes()
    original = cd.pq.write_table

    def fail(table, output):
        original(table, output)
        raise OSError("injected disk failure")

    monkeypatch.setattr(cd.pq, "write_table", fail)
    with pytest.raises(OSError, match="disk failure"):
        fetch(tmp_path, api, end=10)
    assert path.read_bytes() == before and not path.with_name(path.name + ".tmp").exists()
    monkeypatch.setattr(cd.pq, "write_table", original)
    fetch(tmp_path, api, end=10)
    assert api.calls[-1][0] == int((T0 + pd.Timedelta(hours=5)).value // 1_000_000) + 1


def test_concurrent_updates_preserve_a_single_union_of_rates_and_query_coverage(tmp_path):
    api = FundingAPI()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(fetch, tmp_path, api, start=a, end=b) for a, b in ((0, 10), (8, 24))]
        for future in futures:
            future.result(timeout=10)
    rates, coverage = cd.load_funding(tmp_path, "binance", "BTCUSDT")
    assert rates.index.is_unique and len(rates) == 4
    assert coverage == ((int(T0.value), int((T0 + pd.Timedelta(hours=24)).value)),)


def test_reader_keeps_one_snapshot_if_an_update_renames_the_cache_after_open(tmp_path, monkeypatch):
    api = FundingAPI()
    fetch(tmp_path, api, end=5)
    original = cd.pq.read_table
    armed = True

    def concurrent_update(source, *args, **kwargs):
        nonlocal armed
        if armed:
            armed = False
            assert hasattr(source, "fileno")
            fetch(tmp_path, api, end=10)
        return original(source, *args, **kwargs)

    monkeypatch.setattr(cd.pq, "read_table", concurrent_update)
    rates, coverage = cd.load_funding(tmp_path, "binance", "BTCUSDT")
    assert len(rates) == 1 and coverage[-1][1] == (T0 + pd.Timedelta(hours=5)).value
    newer_rates, newer_coverage = cd.load_funding(tmp_path, "binance", "BTCUSDT")
    assert len(newer_rates) == 2 and newer_coverage[-1][1] == (T0 + pd.Timedelta(hours=10)).value


@pytest.mark.parametrize(
    "damage", ["version", "identity", "overlap", "boolean", "overflow", "duplicate-json", "nonfinite"]
)
def test_corrupt_query_metadata_blocks_reads_and_updates(tmp_path, damage):
    api = FundingAPI()
    fetch(tmp_path, api)

    def corrupt(value):
        if damage == "version":
            value["version"] = True
        elif damage == "identity":
            value["symbol"] = "OTHER"
        elif damage == "overlap":
            value["intervals_ms"].append(value["intervals_ms"][0])
        elif damage == "boolean":
            value["intervals_ms"] = [[False, 1]]
        elif damage == "overflow":
            value["intervals_ms"] = [[0, 10**100]]
        elif damage == "duplicate-json":
            return b'{"version":1,"version":1}'
        else:
            return b'{"version":NaN}'
        return value

    change_metadata(tmp_path, mutate=corrupt)
    before = len(api.calls)
    with pytest.raises(ValueError, match="funding retrieval coverage"):
        cd.load_funding(tmp_path, "binance", "BTCUSDT")
    with pytest.raises(ValueError, match="funding retrieval coverage"):
        fetch(tmp_path, api, end=30)
    assert len(api.calls) == before


def test_successfully_queried_empty_interval_is_distinct_from_an_unqueried_period(tmp_path):
    fetch(tmp_path, FundingAPI(hours=()))
    rates, coverage = cd.load_funding(tmp_path, "binance", "BTCUSDT")
    assert rates.empty and coverage is not None
    lg = flat_legs(hours=24)
    h = cs.hourly(replace(lg, funding=rates, funding_coverage=coverage))
    assert h["funding_queried"].all() and (h["funding"] == 0).all()
    unqueried = cs.hourly(replace(lg, funding=rates, funding_coverage=None))
    assert not unqueried["funding_queried"].any() and unqueried["funding"].isna().all()


@pytest.mark.parametrize("venue", ["binance", "bybit"])
def test_cross_venue_study_cannot_treat_one_unknown_venue_as_zero(tmp_path, venue):
    market(tmp_path, days=10)
    change_metadata(tmp_path, venue=venue, mutate=lambda _: None)
    with pytest.raises(ValueError, match="funding retrieval coverage"):
        cs.run(tmp_path, ["BTCUSDT"], cs.CarrySettings(mode="cross", lookback_h=24), T0 + pd.Timedelta(days=11))


def test_one_millisecond_hole_is_not_bridged_by_inventing_coverage():
    lg = flat_legs()
    hole = int((T0 + pd.Timedelta(hours=40)).value)
    h = cs.hourly(
        replace(
            lg, funding_coverage=((int(T0.value), hole - 1_000_000), (hole + 1_000_000, int(lg.long.index[-1].value)))
        )
    )
    assert not h.loc[pd.Timestamp(hole, tz="UTC"), "funding_queried"]
    w = pd.Series(1.0, index=h.index)
    w.iloc[:2] = 0
    with pytest.raises(ValueError, match="funding retrieval coverage"):
        cs.backtest(h, cs.CarrySettings(), w=w)


@pytest.mark.parametrize("mode", ["cash", "cross"])
def test_held_out_funding_values_and_query_extensions_do_not_change_research_inputs(tmp_path, mode):
    market(tmp_path, days=40)
    boundary = T0 + pd.Timedelta(days=30)
    settings = cs.CarrySettings(mode=mode, lookback_h=24)
    evidence = InputEvidence()
    before, _ = cs.run(tmp_path, ["BTCUSDT"], settings, boundary, evidence=evidence)
    for venue in ("binance", "bybit"):
        rates = cd.load(tmp_path, venue, "perp", "funding", "BTCUSDT")
        rates.loc[rates.index >= boundary] = float("nan")
        write(tmp_path, venue, "perp", "funding", "BTCUSDT", rates)
        change_metadata(
            tmp_path,
            venue=venue,
            mutate=lambda value: {
                **value,
                "intervals_ms": [[int(T0.value // 1_000_000), int((T0 + pd.Timedelta(days=50)).value // 1_000_000)]],
            },
        )
    later = InputEvidence()
    after, _ = cs.run(tmp_path, ["BTCUSDT"], settings, boundary, evidence=later)
    pd.testing.assert_frame_equal(before["BTCUSDT"], after["BTCUSDT"])
    assert evidence.snapshot() == later.snapshot()


def test_frozen_holdout_cutoff_does_not_require_later_query_coverage(tmp_path):
    market(tmp_path, days=40)
    start, end = T0 + pd.Timedelta(days=20), T0 + pd.Timedelta(days=30)
    change_metadata(
        tmp_path,
        mutate=lambda value: {**value, "intervals_ms": [[int(T0.value // 1_000_000), int(end.value // 1_000_000)]]},
    )
    pnl, _ = cs.run(tmp_path, ["BTCUSDT"], cs.CarrySettings(lookback_h=24), start, holdout=True, end=end)
    assert pnl["BTCUSDT"].index[-1] == end
    with pytest.raises(ValueError, match="funding retrieval coverage"):
        cs.run(tmp_path, ["BTCUSDT"], cs.CarrySettings(lookback_h=24), start, holdout=True)


def test_cli_failure_is_reserved_before_reading_legacy_funding(tmp_path, capsys):
    from lab.cli import main
    from tests.conftest import REPO

    (tmp_path / "lab.toml").write_text((REPO / "lab.toml").read_text())
    root = tmp_path / "data" / "carry"
    market(root, days=400)
    change_metadata(root, mutate=lambda _: None)
    with pytest.raises(SystemExit, match="funding retrieval coverage"):
        main(["--config", str(tmp_path / "lab.toml"), "carry", "study", "--symbols", "BTCUSDT"])
    rows = history(tmp_path / "results")
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert rows[0]["params"]["funding_policy"] == cs.FUNDING_POLICY
    assert not list((tmp_path / "results" / "inference").glob("*.json"))
    assert "net " not in capsys.readouterr().out
