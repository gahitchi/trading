"""REST fixtures filter real candle opening timestamps, while cache rows use hour ends."""

from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

from lab import carry_data as cd
from tests.test_carry import T0, H, write


class CandleAPI:
    def __init__(self, hours=12, missing=()):
        self.opens = [int(T0.value // 1_000_000) + i * H for i in range(hours) if i not in missing]
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        query = parse_qs(urlparse(url).query)
        bybit = "bybit" in url
        start = int(query["start" if bybit else "startTime"][0])
        end = int(query.get("end" if bybit else "endTime", [2**63 - 1])[0])
        opens = sorted((t for t in self.opens if start <= t <= end), reverse=bybit)[: int(query["limit"][0])]
        if bybit:
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        [str(t), "100", "101", "99", str(100 + (t - self.opens[0]) / H), "10", "1000"] for t in opens
                    ]
                },
            }
        return [[t, "100", "101", "99", str(100 + (t - self.opens[0]) / H), "10", t + H - 1, "1000"] for t in opens]


@pytest.mark.parametrize("venue,market", [("binance", "perp"), ("binance", "spot"), ("bybit", "perp")])
def test_repeated_updates_do_not_skip_the_candle_immediately_after_the_cache(venue, market, tmp_path):
    api = CandleAPI()
    io = {"get": api, "pause": lambda: None}
    assert cd.update(tmp_path, venue, market, "close", "X", T0, T0 + pd.Timedelta(hours=5), **io) == (5, 5)
    assert cd.update(tmp_path, venue, market, "close", "X", T0, T0 + pd.Timedelta(hours=10), **io) == (5, 10)
    expected = pd.date_range(T0 + pd.Timedelta(hours=1), periods=10, freq="h")
    for kind in ("close", "turnover"):
        assert cd.load(tmp_path, venue, market, kind, "X").index.equals(expected)


@pytest.mark.parametrize("venue,market", [("binance", "perp"), ("binance", "spot"), ("bybit", "perp")])
def test_updates_repair_existing_internal_holes_in_both_cached_columns(venue, market, tmp_path):
    idx = pd.date_range(T0 + pd.Timedelta(hours=1), periods=10, freq="h")
    for kind, missing in (("close", [2, 3]), ("turnover", [6])):
        write(tmp_path, venue, market, kind, "X", pd.Series(100.0, index=idx).drop(idx[missing]))
    api = CandleAPI(hours=10)
    cd.update(tmp_path, venue, market, "close", "X", T0, T0 + pd.Timedelta(hours=10), get=api, pause=lambda: None)
    assert cd.load(tmp_path, venue, market, "close", "X").index.equals(idx)
    assert cd.load(tmp_path, venue, market, "turnover", "X").index.equals(idx)
    bounds = [parse_qs(urlparse(url).query) for url in api.calls]
    assert len(bounds) == 2  # repair the two missing ranges, not the years of intact history after them


@pytest.mark.parametrize("venue,market", [("binance", "perp"), ("binance", "spot"), ("bybit", "perp")])
def test_an_up_to_date_cache_needs_no_request_and_never_stores_an_open_candle(venue, market, tmp_path):
    api = CandleAPI()
    now = T0 + pd.Timedelta(hours=5, minutes=30)
    args = (tmp_path, venue, market, "close", "X", T0, now)
    assert cd.update(*args, get=api, pause=lambda: None) == (5, 5)
    before = len(api.calls)
    assert cd.update(*args, get=api, pause=lambda: None) == (0, 5)
    assert len(api.calls) == before
    assert cd.load(tmp_path, venue, market, "close", "X").index[-1] == T0 + pd.Timedelta(hours=5)


def test_an_unavailable_internal_hour_stays_missing_and_is_retried(tmp_path):
    api = CandleAPI(hours=10, missing=[3, 4])
    now = T0 + pd.Timedelta(hours=10)
    args = (tmp_path, "binance", "perp", "close", "X", T0, now)
    assert cd.update(*args, get=api, pause=lambda: None) == (8, 8)
    report = cd.close_coverage(tmp_path, "binance", "perp", "X", T0, now)
    assert report["state"] == "gappy" and report["funding_schedule_verified"] is False
    assert report["series"]["close"]["gaps"] == [
        {
            "from": (T0 + pd.Timedelta(hours=4)).isoformat(),
            "through": (T0 + pd.Timedelta(hours=5)).isoformat(),
            "hours": 2,
        }
    ]
    assert cd.update(*args, get=api, pause=lambda: None) == (0, 8)
    assert len(api.calls) == 2  # the second request is only the unresolved two-hour interval
    recovered = CandleAPI(hours=10)
    assert cd.update(*args, get=recovered, pause=lambda: None) == (2, 10)
    assert cd.close_coverage(tmp_path, "binance", "perp", "X", T0, now)["state"] == "continuous-observed-window"


def test_unknown_prelisting_history_is_labelled_and_not_repeatedly_downloaded(tmp_path):
    api = CandleAPI(hours=10, missing=range(5))
    now = T0 + pd.Timedelta(hours=10)
    args = (tmp_path, "binance", "perp", "close", "X", T0, now)
    assert cd.update(*args, get=api, pause=lambda: None) == (5, 5)
    report = cd.close_coverage(tmp_path, "binance", "perp", "X", T0, now)
    assert report["leading_unverified_hours"] == 5
    assert (
        report["state"] == "continuous-observed-window"
        and report["observed_from"] == (T0 + pd.Timedelta(hours=6)).isoformat()
    )
    cd.update(*args, get=api, pause=lambda: None)
    assert len(api.calls) == 1


def test_missing_companion_file_is_rebuilt_and_reported_unavailable_until_then(tmp_path):
    api = CandleAPI(hours=10)
    now = T0 + pd.Timedelta(hours=10)
    args = (tmp_path, "binance", "perp", "close", "X", T0, now)
    cd.update(*args, get=api, pause=lambda: None)
    cd.path_of(tmp_path, "binance", "perp", "turnover", "X").unlink()
    report = cd.close_coverage(tmp_path, "binance", "perp", "X", T0, now)
    assert report["state"] == "gappy" and report["series"]["turnover"]["missing_hours"] == 10
    assert cd.update(*args, get=api, pause=lambda: None) == (0, 10)
    assert len(cd.load(tmp_path, "binance", "perp", "turnover", "X")) == 10


def test_interrupted_second_column_write_is_repaired_on_the_next_update(tmp_path, monkeypatch):
    api = CandleAPI(hours=10)
    args = (tmp_path, "binance", "perp", "close", "X", T0)
    cd.update(*args, T0 + pd.Timedelta(hours=5), get=api, pause=lambda: None)
    original = pd.DataFrame.to_parquet

    def fail_turnover(df, path, **kw):
        if "turnover" in path.name:
            raise OSError("interrupted turnover write")
        return original(df, path, **kw)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", fail_turnover)
    now = T0 + pd.Timedelta(hours=10)
    with pytest.raises(OSError, match="interrupted"):
        cd.update(*args, now, get=api, pause=lambda: None)
    assert len(cd.load(tmp_path, "binance", "perp", "close", "X")) == 10
    assert len(cd.load(tmp_path, "binance", "perp", "turnover", "X")) == 5
    monkeypatch.setattr(pd.DataFrame, "to_parquet", original)
    assert cd.update(*args, now, get=api, pause=lambda: None) == (0, 10)
    assert cd.close_coverage(tmp_path, "binance", "perp", "X", T0, now)["state"] == "continuous-observed-window"


@pytest.mark.parametrize(
    "damage", ["duplicate", "unordered", "future", "noninteger", "zero", "nan", "negative-turnover"]
)
@pytest.mark.parametrize("venue", ["binance", "bybit"])
def test_invalid_response_cannot_replace_cached_values(tmp_path, damage, venue):
    api = CandleAPI(hours=10)
    args = (tmp_path, venue, "perp", "close", "X", T0)
    cd.update(*args, T0 + pd.Timedelta(hours=5), get=api, pause=lambda: None)
    paths = [cd.path_of(tmp_path, venue, "perp", kind, "X") for kind in ("close", "turnover")]
    before = [path.read_bytes() for path in paths]

    def corrupt(url):
        response = api(url)
        rows = response["result"]["list"] if venue == "bybit" else response
        if damage == "duplicate":
            rows.append(rows[-1])
        elif damage == "unordered":
            rows.reverse()
        elif damage == "future":
            rows[0][0] = int(T0.value // 1_000_000) + 11 * H
        elif damage == "noninteger":
            rows[0][0] = float(rows[0][0]) + 0.5
        elif damage == "zero":
            rows[0][4] = "0"
        elif damage == "nan":
            rows[0][4] = "nan"
        else:
            rows[0][6 if venue == "bybit" else 7] = "-1"
        return response

    with pytest.raises(ValueError, match="invalid history"):
        cd.update(*args, T0 + pd.Timedelta(hours=10), get=corrupt, pause=lambda: None)
    assert [path.read_bytes() for path in paths] == before


@pytest.mark.parametrize("venue", ["binance", "bybit"])
def test_funding_resume_uses_event_times_and_respects_the_frozen_cutoff(tmp_path, venue):
    t0 = int(T0.value // 1_000_000)
    events = [t0 + i * 8 * H for i in range(5)]
    calls = []

    def get(url):
        q = parse_qs(urlparse(url).query)
        calls.append(q)
        subset = [t for t in events if int(q["startTime"][0]) <= t <= int(q["endTime"][0])]
        rows = [
            {"fundingRateTimestamp": str(t), "fundingRate": "0.0001"}
            if venue == "bybit"
            else {"fundingTime": t, "fundingRate": "0.0001"}
            for t in sorted(subset, reverse=venue == "bybit")
        ]
        return {"retCode": 0, "result": {"list": rows}} if venue == "bybit" else rows

    args = (tmp_path, venue, "perp", "funding", "X", T0)
    assert cd.update(*args, T0 + pd.Timedelta(hours=8), get=get) == (2, 2)
    assert cd.update(*args, T0 + pd.Timedelta(hours=16), get=get) == (1, 3)
    assert int(calls[-1]["startTime"][0]) == t0 + 8 * H + 1
    assert cd.load(tmp_path, venue, "perp", "funding", "X").index[-1] == T0 + pd.Timedelta(hours=16)


@pytest.mark.parametrize("venue", ["binance", "bybit"])
def test_a_repeated_full_page_fails_instead_of_looping_forever(tmp_path, venue):
    api = CandleAPI(hours=2000)
    first = None
    calls = 0

    def repeating(url):
        nonlocal first, calls
        calls += 1
        if first is None:
            first = api(url)
        return first

    with pytest.raises(ValueError, match="out-of-window"):
        cd.update(
            tmp_path, venue, "perp", "close", "X", T0, T0 + pd.Timedelta(hours=2000), get=repeating, pause=lambda: None
        )
    assert calls == 2 and not cd.path_of(tmp_path, venue, "perp", "close", "X").exists()


@pytest.mark.parametrize("venue", ["binance", "bybit"])
def test_invalid_funding_cannot_enter_the_cache(tmp_path, venue):
    def get(url):
        row = (
            {"fundingRateTimestamp": str(T0.value // 1_000_000), "fundingRate": "nan"}
            if venue == "bybit"
            else {"fundingTime": T0.value // 1_000_000, "fundingRate": "nan"}
        )
        return {"retCode": 0, "result": {"list": [row]}} if venue == "bybit" else [row]

    with pytest.raises(ValueError, match="invalid history value"):
        cd.update(tmp_path, venue, "perp", "funding", "X", T0, T0 + pd.Timedelta(hours=10), get=get)
    assert not cd.path_of(tmp_path, venue, "perp", "funding", "X").exists()


@pytest.mark.parametrize("damage", ["duplicate", "unordered", "missing-time", "unaligned", "nan", "zero"])
def test_invalid_cache_is_not_silently_rewritten(tmp_path, damage):
    index = pd.date_range(T0 + pd.Timedelta(hours=1), periods=3, freq="h")
    values = [100.0, 100.0, 100.0]
    if damage == "duplicate":
        index = pd.DatetimeIndex([index[0], index[0], index[2]])
    elif damage == "unordered":
        index = index[::-1]
    elif damage == "missing-time":
        index = pd.DatetimeIndex([index[0], pd.NaT, index[2]])
    elif damage == "unaligned":
        index += pd.Timedelta(minutes=1)
    else:
        values[1] = float("nan") if damage == "nan" else 0.0
    write(tmp_path, "binance", "perp", "close", "X", pd.Series(values, index=index))
    path = cd.path_of(tmp_path, "binance", "perp", "close", "X")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="invalid history"):
        cd.update(
            tmp_path,
            "binance",
            "perp",
            "close",
            "X",
            T0,
            T0 + pd.Timedelta(hours=10),
            get=lambda _: pytest.fail("invalid cache must be checked first"),
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize("since,now", [(T0, T0 - pd.Timedelta(hours=1)), (T0.tz_localize(None), T0), (pd.NaT, T0)])
def test_invalid_windows_do_not_start_a_download(tmp_path, since, now):
    with pytest.raises(ValueError, match="history window"):
        cd.update(
            tmp_path,
            "binance",
            "perp",
            "close",
            "X",
            since,
            now,
            get=lambda _: pytest.fail("invalid window downloaded"),
        )


def test_aware_input_windows_are_normalized_to_utc(tmp_path):
    api = CandleAPI()
    cd.update(
        tmp_path,
        "binance",
        "perp",
        "close",
        "X",
        T0.tz_convert("Asia/Kolkata"),
        (T0 + pd.Timedelta(hours=5)).tz_convert("Asia/Kolkata"),
        get=api,
        pause=lambda: None,
    )
    report = cd.close_coverage(tmp_path, "binance", "perp", "X", T0, T0 + pd.Timedelta(hours=5))
    assert report["state"] == "continuous-observed-window" and report["series"]["close"]["rows"] == 5


def test_empty_history_does_not_claim_zero_gaps_means_coverage(tmp_path):
    report = cd.close_coverage(tmp_path, "binance", "perp", "X", T0, T0 + pd.Timedelta(hours=5))
    assert report["state"] == "unavailable" and report["observed_from"] is None
    assert report["leading_unverified_hours"] == 5


@pytest.mark.parametrize("gappy,unavailable", [(False, False), (True, False), (False, "error"), (False, "empty")])
def test_cli_retains_exact_gap_report_and_returns_nonzero_when_incomplete(
    tmp_path, monkeypatch, capsys, gappy, unavailable
):
    from lab import cli
    from lab.config import LabConfig
    from lab.persistence import read_json

    now = T0 + pd.Timedelta(hours=10)

    def stamp(*args, **kwargs):
        return pd.Timestamp(*args, **kwargs)

    stamp.now = lambda **_: now
    monkeypatch.setattr(cli, "pd", SimpleNamespace(Timestamp=stamp))
    api = CandleAPI(hours=10, missing=[3] if gappy else [])
    original = cd.download

    def download(venue, market, kind, symbol, start, end, **_):
        if kind == "funding":
            if unavailable == "error":
                raise ValueError("funding unavailable")
            if unavailable == "empty":
                return []
            return [(int(T0.value // 1_000_000), 0.0001)]
        return original(venue, market, kind, symbol, start, end, get=api, pause=lambda: None)

    monkeypatch.setattr(cd, "download", download)
    cfg = LabConfig(tmp_path, tmp_path / "data", tmp_path / "results")
    args = SimpleNamespace(since="2024-01-01", venues=["binance"], all=False, symbols=["X"])
    assert cli.cmd_carry_fetch(cfg, args) == int(bool(gappy or unavailable))
    path = cd.path_of(cfg.data_dir / "carry", "binance", "perp", "coverage", "X").with_suffix(".json")
    report = read_json(path)
    assert report["state"] == ("gappy" if gappy else "continuous-observed-window")
    assert report["series"]["close"]["missing_hours"] == int(gappy)
    out = capsys.readouterr().out
    assert "funding settlement schedule coverage is unverified" in out
    assert ("fetch incomplete" in out) == bool(gappy or unavailable)
