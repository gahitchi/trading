"""Synthetic-only forward price and collector tests. Never contact exchange."""

from __future__ import annotations

import base64
import json

import pytest

from lab.bearish_hybrid import MINUTE_NS
from lab.bearish_hybrid_forward_collector import (
    MAX_CYCLES,
    capture_public_minute,
    run_bounded_capture,
)
from lab.bearish_hybrid_forward_price import (
    load_forward_prices,
    record_forward_price,
)

S = "ETHUSDT"
T = 1_700_006_400_000_000_000


def candle(opening_ns: int, *, close: str = "205", quote: str = "4050"):
    opening_ms = opening_ns // 1_000_000
    return [
        opening_ms, "200", "210", "190", close, "20",
        opening_ms + 59_999, quote, 20, "10", "2000", "0",
    ]


def source(*, older=None, current=None):
    older = candle(T - MINUTE_NS) if older is None else older
    current = candle(T) if current is None else current
    return json.dumps([older, current]).encode()


def clock(start=T + 5_000_000_000):
    times = iter((start, start + 20_000_000))
    return lambda: next(times)


def capture(tmp_path, raw=None, start=T + 5_000_000_000):
    return record_forward_price(
        tmp_path, S, source=lambda _: source() if raw is None else raw, clock_ns=clock(start)
    )


def receipt_file(tmp_path):
    return next((tmp_path / "binance-usdm" / "forward-prices" / S).glob("*.json"))


def test_closed_minute_selected_not_in_progress_and_reloadable(tmp_path):
    p = capture(tmp_path)
    assert p.bar.end_ns == T
    assert p.bar.close == 205
    assert p.source == "injected-test-source"
    assert load_forward_prices(tmp_path, S) == (p,)
    saved = json.loads(receipt_file(tmp_path).read_text())
    assert base64.b64decode(saved["raw_base64"]) == source()
    assert saved["independently_authenticated"] is False
    assert saved["historical_publication_verified"] is False
    assert saved["price_history_complete"] is False
    assert saved["qualified"] is False
    assert saved["actual_notional"] == 0


def test_never_retroactively_promotes_kline_completion(tmp_path):
    with pytest.raises(ValueError, match="missing or stale"):
        capture(tmp_path, raw=json.dumps([candle(T)]).encode())
    assert not list(tmp_path.rglob("*.json"))


def test_reject_stale_close_even_if_source_payload_is_valid(tmp_path):
    raw = json.dumps([candle(T - 4 * MINUTE_NS), candle(T - 3 * MINUTE_NS)]).encode()
    with pytest.raises(ValueError, match="missing or stale"):
        capture(tmp_path, raw=raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b'{"x":1,"x":2}',
        b"[]",
        json.dumps([candle(T - MINUTE_NS)] * 3).encode(),
        json.dumps([candle(T), candle(T - MINUTE_NS)]).encode(),
        json.dumps([candle(T - 2 * MINUTE_NS), candle(T)]).encode(),
        json.dumps([candle(T - MINUTE_NS)[:-1], candle(T)]).encode(),
        json.dumps([candle(T - MINUTE_NS, quote="10"), candle(T)]).encode(),
        json.dumps([candle(T - MINUTE_NS, close="NaN"), candle(T)]).encode(),
        json.dumps([candle(T - MINUTE_NS, close="-1"), candle(T)]).encode(),
    ],
)
def test_reject_malformed_or_impossible_price_payload(tmp_path, raw):
    with pytest.raises(ValueError):
        capture(tmp_path, raw=raw)
    assert not list(tmp_path.rglob("*.json"))


def test_reject_unsafe_symbol_before_request(tmp_path):
    called = []

    def injected(url):
        called.append(url)
        raise AssertionError("must not request")

    for symbol in ("BTCUSDT", "../ETHUSDT", "ethusdt", "ETHUSD"):
        with pytest.raises(ValueError):
            record_forward_price(tmp_path, symbol, source=injected)
    assert called == []


def test_reject_unreliable_clock_and_oversized_payload(tmp_path):
    with pytest.raises(ValueError, match="clock"):
        capture(tmp_path, start=0)
    with pytest.raises(ValueError, match="clock"):
        record_forward_price(tmp_path, S, source=lambda _: source(), clock_ns=clock(T + 5_000_000_000))
    with pytest.raises(ValueError, match="oversized"):
        capture(tmp_path, raw=b"x" * 10_000)


def test_offline_rejects_hash_mutation_and_forged_trust(tmp_path):
    capture(tmp_path)
    path = receipt_file(tmp_path)
    saved = json.loads(path.read_text())
    saved["raw_base64"] = base64.b64encode(source(older=candle(T - MINUTE_NS, close="201"))).decode()
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="modified public price"):
        load_forward_prices(tmp_path, S)

    saved["raw_base64"] = base64.b64encode(source()).decode()
    saved["qualified"] = True
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="untrusted"):
        load_forward_prices(tmp_path, S)


def test_same_minute_unchanged_repeat_allowed_but_revision_fails(tmp_path):
    first = capture(tmp_path)
    second = capture(tmp_path, start=T + 20_000_000_000)
    assert second.bar == first.bar
    assert len(load_forward_prices(tmp_path, S)) == 2
    capture(
        tmp_path,
        raw=source(older=candle(T - MINUTE_NS, close="204")),
        start=T + 30_000_000_000,
    )
    with pytest.raises(ValueError, match="revised"):
        load_forward_prices(tmp_path, S)


def test_offline_rejects_symlink_and_unknown_file(tmp_path):
    capture(tmp_path)
    folder = receipt_file(tmp_path).parent
    (folder / "junk.txt").write_text("junk")
    with pytest.raises(ValueError, match="unexpected"):
        load_forward_prices(tmp_path, S)
    (folder / "junk.txt").unlink()
    p = receipt_file(tmp_path)
    p.unlink()
    p.symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="unexpected"):
        load_forward_prices(tmp_path, S)


def test_repeated_received_timestamp_never_overwrites(tmp_path):
    original = capture(tmp_path)
    with pytest.raises(ValueError, match="duplicate"):
        capture(tmp_path)
    assert load_forward_prices(tmp_path, S) == (original,)


def test_capture_minute_frequency_and_no_qualification(tmp_path, monkeypatch):
    from lab import bearish_hybrid_forward_collector as runner

    calls = []
    fake_price = capture(tmp_path)

    def fake_feature(root, symbol, kind):
        calls.append(kind)
        return object()

    monkeypatch.setattr(runner, "record_forward_price", lambda root, symbol: fake_price)
    monkeypatch.setattr(runner, "record_public_feature", fake_feature)

    first = capture_public_minute(tmp_path, S, 0)
    assert first.price == fake_price
    assert first.oi is not None and first.funding is not None
    assert first.qualified is False and first.actual_notional == 0
    assert calls == ["open-interest", "settled-funding"]

    middle = capture_public_minute(tmp_path, S, 1)
    assert middle.oi is None and middle.funding is None
    sixth = capture_public_minute(tmp_path, S, 6)
    assert sixth.oi is not None and sixth.funding is None
    assert calls == ["open-interest", "settled-funding", "open-interest"]


def test_bounded_collector_rejects_unsafe_duration_without_sleep(tmp_path, monkeypatch):
    from lab import bearish_hybrid_forward_collector as runner

    def forbidden(_seconds):
        raise AssertionError("must not sleep")

    monkeypatch.setattr(runner.time, "sleep", forbidden)
    for cycles in (0, -1, MAX_CYCLES + 1, 1.5, True):
        with pytest.raises(ValueError):
            run_bounded_capture(tmp_path, S, cycles)
    with pytest.raises(ValueError):
        capture_public_minute(tmp_path, S, -1)
