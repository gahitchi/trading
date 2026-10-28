"""The recorder monitor: reports a real recording's health and volume, flags what an operator must act on, and
serves it read-only on a fixed set of paths."""

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest
from nautilus_trader.model import CryptoPerpetual

from lab.monitor import NS, Monitor, MonitorServer, Rates, file_span, gaps
from lab.persistence import append_json
from lab.record import heartbeat_path
from lab.safety import set_kill_switch
from tests.test_record import T0, market_events, perpetual, record

MIN = 60 * NS


def iso(ns):
    return datetime.fromtimestamp(ns / NS, UTC).isoformat()


def heartbeat(out, at_ns, *, venue="binance", running=True, started_ns=None, stale=(), **counts):
    instruments = {
        iid: {"book": n, "trades": n, "mark": n, "funding": 1, "last_event": iso(at_ns)} for iid, n in counts.items()
    }
    heartbeat_path(out, venue).parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path(out, venue).write_text(
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
BYBIT_BTC = "BTCUSDT-LINEAR.BYBIT"


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
    assert s["events_per_s"] == pytest.approx(3 * 300 / 10)
    assert s["instruments"][0]["state"] == "live"
    assert s["recorders"][0]["uptime_s"] == 65  # started a minute before the second heartbeat


def test_an_old_heartbeat_means_the_recorder_is_down(tmp_path):
    heartbeat(tmp_path, T0, **{BTC: 1})
    s = Monitor(tmp_path).status(T0 + 3 * MIN)
    assert s["status"] == "down" and s["recorders"][0]["running"] is False
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
    catalog = next(c for c in s["checks"] if c["name"] == "binance catalog")
    assert s["status"] == "degraded" and catalog["state"] == "fail"
    # In its first hour nothing is due yet.
    heartbeat(tmp_path, T0, started_ns=T0 - 30 * MIN, **{BTC: 5})
    assert Monitor(tmp_path).status(T0 + NS)["status"] == "ok"


def test_each_venue_has_its_own_recorder_and_one_down_degrades(tmp_path):
    heartbeat(tmp_path, T0, **{BTC: 5})
    heartbeat(tmp_path, T0 - 5 * MIN, venue="bybit", **{BYBIT_BTC: 5})  # Bybit's recorder died five minutes ago

    s = Monitor(tmp_path).status(T0 + NS)

    assert s["status"] == "degraded"
    assert {r["venue"]: r["running"] for r in s["recorders"]} == {"BINANCE": True, "BYBIT": False}
    assert {c["name"]: c["state"] for c in s["checks"]} == {"binance": "ok", "bybit": "fail"}
    assert {i["id"]: (i["venue"], i["state"]) for i in s["instruments"]} == {
        BTC: ("BINANCE", "live"),
        BYBIT_BTC: ("BYBIT", "not recording"),
    }


def test_both_venues_recorded_into_one_catalog_are_inventoried(tmp_path):
    binance, bybit = perpetual("BTCUSDT"), perpetual("BTCUSDT", "bybit")
    record(tmp_path, [binance], market_events(binance.id, 6))
    record(tmp_path, [bybit], market_events(bybit.id, 4), venue="bybit")

    s = Monitor(tmp_path).status(T0 + MIN)

    assert {i["id"]: i["catalog"]["rows"] for i in s["instruments"]} == {BTC: 6 * 3 + 2, BYBIT_BTC: 4 * 3 + 2}
    assert [r["venue"] for r in s["recorders"]] == ["BINANCE", "BYBIT"]


def test_instrument_definitions_without_market_data_are_not_listed(tmp_path):
    # The Bybit adapter loads every linear instrument, and the stream saves each one's definition.
    btc, eth, sol = (perpetual(s, "bybit") for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"))
    record(tmp_path, [btc, eth, sol], market_events(btc.id, 4), venue="bybit")
    heartbeat(tmp_path, T0, venue="bybit", **{BYBIT_BTC: 4})

    s = Monitor(tmp_path).status(T0 + NS)

    assert [i["id"] for i in s["instruments"]] == [BYBIT_BTC]
    assert s["catalog"]["by_type"]["crypto_perpetual"]["files"] == 3  # still counted as stored data


def test_stream_files_left_by_a_run_that_stopped_without_saving_are_flagged(tmp_path):
    started = T0 - 30 * MIN
    heartbeat(tmp_path, T0, started_ns=started, **{BTC: 5})

    def stream_file(path, mtime_ns, size):
        f = tmp_path / "stream" / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * size)
        os.utime(f, ns=(mtime_ns, mtime_ns))

    stream_file("binance/live/run-now/trades/trades_1.feather", T0, 100)  # this run's, promoted within the hour
    stream_file("binance/live/run-killed/trades/trades_0.feather.partial", started - MIN, 2_000_000)
    stream_file("bybit/live/run-old/trades/trades_0.feather.partial", T0, 500_000)  # no Bybit recorder running

    s = Monitor(tmp_path).status(T0 + NS)

    assert s["stream"]["files"] == 3 and s["stream"]["bytes"] == 2_500_100
    assert s["stream"]["leftover"] == {"files": 2, "bytes": 2_500_000}
    stream = next(c for c in s["checks"] if c["name"] == "stream")
    assert s["status"] == "degraded" and stream["state"] == "warn" and stream["detail"].startswith("2.5 MB in 2")


def test_an_outage_between_files_shows_as_a_gap(tmp_path):
    btc = perpetual("BTCUSDT")
    later = T0 + 10 * MIN  # restarted ten minutes later; a live restart loads the instrument again, at that time
    reloaded = CryptoPerpetual.from_dict({**CryptoPerpetual.to_dict(btc), "ts_event": later, "ts_init": later})
    record(tmp_path / "a", [btc], market_events(btc.id, 5))
    record(tmp_path / "a", [reloaded], market_events(btc.id, 5, start_ns=later))

    c = Monitor(tmp_path / "a").status(T0 + 20 * MIN)["instruments"][0]["catalog"]

    assert len(c["gaps"]) == 1 and c["gaps"][0]["seconds"] == pytest.approx(600, abs=1)
    assert 0 < c["coverage_pct"] < 1


def paper_report(results, at_ns, *, running=True, state="ACTIVE", reason="", day_pnl=-5.0):
    (results / "fast").mkdir(parents=True, exist_ok=True)
    report = {
        "at": iso(at_ns), "running": running, "equity": 9995.0, "pnl": -5.0, "day_pnl": day_pnl,
        "risk": {"state": state, "reason": reason, "limits": {"max_daily_loss": 100.0}, "denials": {}},
    }  # fmt: skip
    (results / "fast" / "paper.json").write_text(json.dumps(report))


def test_paper_trading_is_reported_beside_the_recorders(tmp_path):
    results = tmp_path / "results"
    paper_report(results, T0)
    s = Monitor(tmp_path, results=results).status(T0 + 5 * NS)
    assert s["paper"]["status"] == "running" and s["paper"]["report"]["equity"] == 9995.0
    assert {c["name"]: c["state"] for c in s["paper"]["checks"]} == {"paper": "ok", "risk": "ok"}
    assert s["status"] == "down"  # the recorders' status is their own: none runs here


def test_paper_checks_flag_what_an_operator_must_act_on(tmp_path):
    results = tmp_path / "results"
    paper_report(results, T0, state="REDUCING", reason="daily loss 85.00 USDT reached the cap", day_pnl=-85.0)
    set_kill_switch(results / "KILL", "halt", "operator test")
    p = Monitor(tmp_path, results=results).status(T0 + 2 * MIN)["paper"]
    assert p["status"] == "stopped"  # no report for two minutes
    checks = {c["name"]: (c["state"], c["detail"]) for c in p["checks"]}
    assert checks["paper"][0] == "warn"
    assert checks["risk"] == ("warn", "REDUCING: daily loss 85.00 USDT reached the cap")
    assert checks["daily loss"] == ("warn", "85.00 of the 100.00 cap")
    assert checks["kill switch"] == ("fail", "halt: operator test") and p["kill"]["mode"] == "halt"


def test_no_paper_run_yet_and_no_results_directory(tmp_path):
    assert Monitor(tmp_path, results=tmp_path / "results").status(T0)["paper"] == {
        "status": "none", "report_age_s": None, "report": None, "kill": None, "checks": [], "events": []
    }  # fmt: skip
    assert Monitor(tmp_path).status(T0)["paper"] is None


def test_risk_events_are_the_latest_audit_entries_newest_first(tmp_path):
    audit = tmp_path / "results" / "fast" / "risk-audit.jsonl"
    audit.parent.mkdir(parents=True)
    for i in range(30):
        append_json(audit, {"ts_ns": T0 + i * NS, "kind": "denied", "reason": f"position_limit: {i}"})
    with audit.open("a") as f:
        f.write("{torn line")  # a write cut short is skipped, not fatal
    events = Monitor(tmp_path, results=tmp_path / "results").status(T0)["paper"]["events"]
    assert len(events) == 20 and events[0]["reason"] == "position_limit: 29"
    assert events[0]["at"] == iso(T0 + 29 * NS)


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
