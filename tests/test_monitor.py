"""The recorder monitor: reports a real recording's health and volume, flags what an operator must act on, and
serves it read-only on a fixed set of paths."""

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest
from nautilus_trader.model import CryptoPerpetual

from lab.monitor import NS, Monitor, MonitorServer, Rates, file_span, gaps
from tests.test_record import T0, market_events, perpetual, record

MIN = 60 * NS


def iso(ns):
    return datetime.fromtimestamp(ns / NS, UTC).isoformat()


def heartbeat(out, at_ns, *, running=True, started_ns=None, stale=(), **counts):
    instruments = {
        iid: {"book": n, "trades": n, "mark": n, "funding": 1, "last_event": iso(at_ns)} for iid, n in counts.items()
    }
    (out / "heartbeat.json").write_text(
        json.dumps(
            {
                "at": iso(at_ns),
                "started": iso(started_ns or at_ns - MIN),
                "running": running,
                "instruments": instruments,
                "stale": list(stale),
            }
        )
    )


BTC = "BTCUSDT-PERP.BINANCE"


def test_file_names_give_the_recorded_span():
    assert file_span("2025-10-20T22-40-00-001000000Z_2025-10-20T22-40-00-049000000Z_63736a17c5458205.parquet") == (
        T0 + 1_000_000,
        T0 + 49_000_000,
    )
    assert file_span("notes.parquet") is None


def test_gaps_are_holes_between_spans_longer_than_the_limit():
    spans = [(0, 10), (12, 20), (5, 15), (100, 110), (111, 120)]
    assert gaps(spans, 5) == [(20, 100)]
    assert gaps([], 5) == []


def test_rates_follow_successive_heartbeats_and_reset_on_restart():
    r = Rates()
    r.add(0, {BTC: 0})
    r.add(10 * NS, {BTC: 500})
    r.add(10 * NS, {BTC: 999})  # the same heartbeat read twice is not a sample
    assert r.current() == {BTC: 50.0}
    assert r.history() == {BTC: [["1970-01-01T00:00:10+00:00", 50.0]]}
    r.add(20 * NS, {BTC: 30})  # restarted: counters went back
    assert r.current() == {} and r.history() == {}


def test_a_real_recording_is_inventoried(tmp_path):
    btc, eth = perpetual("BTCUSDT"), perpetual("ETHUSDT")
    record(tmp_path, [btc, eth], market_events(btc.id, 10) + market_events(eth.id, 4))
    now = T0 + MIN

    s = Monitor(tmp_path).status(now)

    assert s["status"] == "down"  # the heartbeat says the recorder stopped
    assert s["checks"][0]["state"] == "fail" and "stopped" in s["checks"][0]["detail"]
    by_id = {i["id"]: i for i in s["instruments"]}
    assert set(by_id) == {BTC, "ETHUSDT-PERP.BINANCE"}
    assert by_id[BTC]["counts"] == {"book": 10, "trades": 10, "mark": 10, "funding": 1}
    assert by_id[BTC]["state"] == "not recording"
    assert by_id[BTC]["catalog"]["rows"] == 10 + 10 + 10 + 1 + 1  # book, trades, mark, funding, instrument
    assert by_id[BTC]["catalog"]["coverage_pct"] == 100
    assert s["catalog"]["by_type"]["trades"]["rows"] == 14
    assert s["catalog"]["files"] == sum(t["files"] for t in s["catalog"]["by_type"].values())
    assert s["stream"]["files"] == 0  # everything promoted at stop
    # The span is event time; instrument definitions (stamped 0 in a backtest) don't stretch it back to 1970.
    assert s["catalog"]["first"] == "2025-10-20T22:40:00+00:00"


def test_a_healthy_recorder_is_ok_and_reports_its_rate(tmp_path):
    m = Monitor(tmp_path)
    heartbeat(tmp_path, T0, **{BTC: 100})
    m.status(T0 + 5 * NS)
    heartbeat(tmp_path, T0 + 10 * NS, **{BTC: 400})

    s = m.status(T0 + 15 * NS)

    assert s["status"] == "ok", s["checks"]
    assert s["recorder"]["events_per_s"] == pytest.approx(3 * 300 / 10)
    assert s["instruments"][0]["state"] == "live"
    assert s["recorder"]["uptime_s"] == 65  # started a minute before the second heartbeat


def test_an_old_heartbeat_means_the_recorder_is_down(tmp_path):
    heartbeat(tmp_path, T0, **{BTC: 1})
    s = Monitor(tmp_path).status(T0 + 3 * MIN)
    assert s["status"] == "down" and s["recorder"]["running"] is False
    assert s["instruments"][0]["state"] == "not recording"
    assert "not running" in s["checks"][0]["detail"]


def test_no_heartbeat_at_all_is_down(tmp_path):
    s = Monitor(tmp_path).status(T0)
    assert s["status"] == "down" and s["instruments"] == []


def test_a_silent_feed_degrades(tmp_path):
    heartbeat(tmp_path, T0, stale=[BTC], **{BTC: 5, "ETHUSDT-PERP.BINANCE": 5})
    s = Monitor(tmp_path).status(T0 + NS)
    assert s["status"] == "degraded"
    assert {i["id"]: i["state"] for i in s["instruments"]} == {BTC: "stale", "ETHUSDT-PERP.BINANCE": "live"}


def test_data_not_reaching_the_catalog_fails(tmp_path):
    # Running for two hours with an empty catalog: promotion is broken (e.g. the writer failed to close).
    heartbeat(tmp_path, T0, started_ns=T0 - 120 * MIN, **{BTC: 5})
    s = Monitor(tmp_path).status(T0 + NS)
    catalog = next(c for c in s["checks"] if c["name"] == "catalog")
    assert s["status"] == "degraded" and catalog["state"] == "fail"
    # In its first hour nothing is due yet.
    heartbeat(tmp_path, T0, started_ns=T0 - 30 * MIN, **{BTC: 5})
    assert Monitor(tmp_path).status(T0 + NS)["status"] == "ok"


def test_an_outage_between_files_shows_as_a_gap(tmp_path):
    btc = perpetual("BTCUSDT")
    later = T0 + 10 * MIN  # restarted ten minutes later; a live restart loads the instrument again, at that time
    reloaded = CryptoPerpetual.from_dict({**CryptoPerpetual.to_dict(btc), "ts_event": later, "ts_init": later})
    record(tmp_path / "a", [btc], market_events(btc.id, 5))
    record(tmp_path / "a", [reloaded], market_events(btc.id, 5, start_ns=later))

    c = Monitor(tmp_path / "a").status(T0 + 20 * MIN)["instruments"][0]["catalog"]

    assert len(c["gaps"]) == 1 and c["gaps"][0]["seconds"] == pytest.approx(600, abs=1)
    assert 0 < c["coverage_pct"] < 1


@pytest.fixture
def server(tmp_path):
    heartbeat(tmp_path, T0, **{BTC: 1})  # long stale against the real clock: down
    srv = MonitorServer(("127.0.0.1", 0), Monitor(tmp_path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def get(url, host=None):
    req = urllib.request.Request(url, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_the_server_serves_the_dashboard_status_and_health(server):
    code, headers, body = get(server + "/")
    assert code == 200 and b"/monitor.js" in body and "script-src 'self'" in headers["Content-Security-Policy"]
    for path in ("/monitor.js", "/monitor.css", "/fonts/archivo-latin.woff2"):
        assert get(server + path)[0] == 200
    code, _, body = get(server + "/api/status")
    assert code == 200 and json.loads(body)["status"] == "down"
    assert get(server + "/health")[0] == 503


def test_the_server_refuses_other_hosts_and_paths(server):
    assert get(server + "/api/status", host="evil.example:80")[0] == 403  # DNS rebinding
    for path in ("/../heartbeat.json", "/heartbeat.json", "/app.js", "/api/setup"):
        assert get(server + path)[0] == 404
