"""Offline Binance USD-M mark/funding evidence: exact bytes and no cash posting."""

import json
from dataclasses import replace
from decimal import Decimal

import pytest

import lab.public_market_evidence as evidence
from lab.public_market_evidence import (
    PublicMarketEvidenceJournal,
    fetch_public_market_sample,
    parse_public_market_sample,
)

T = 1_700_000_000_000_000_000
MS = 1_000_000
SYMBOL = "BTCUSDT"
SESSION = "paper-1"


def responses(start=T):
    base = start // MS
    mark = {
        "symbol": SYMBOL,
        "markPrice": "100.125000",
        "indexPrice": "100.00",
        "lastFundingRate": "-0.000325",
        "nextFundingTime": base + 4 * 3_600_000,
        "time": base + 1000,
    }
    history = [
        {
            "symbol": SYMBOL,
            "fundingTime": base - 8 * 3_600_000,
            "fundingRate": "0.00012500",
            "markPrice": "99.99900",
        }
    ]
    return mark, history


def parsed(start=T, mark=None, funding=None):
    m, f = responses(start)
    if mark is not None:
        m.update(mark)
    if funding is not None:
        f[0].update(funding)
    return parse_public_market_sample(
        session_id=SESSION,
        symbol=SYMBOL,
        mark_raw=json.dumps(m).encode(),
        funding_raw=json.dumps(f).encode(),
        mark_start_ns=start,
        mark_end_ns=start + 2_000_000_000,
        funding_start_ns=start + 2_000_000_000,
        funding_end_ns=start + 3_000_000_000,
    )


def store(tmp_path):
    return PublicMarketEvidenceJournal(
        tmp_path / "market.jsonl", session_id=SESSION, symbol=SYMBOL
    )


def test_source_fields_preserve_price_funding_rate_and_exact_responses():
    point = parsed()
    assert point.instrument_id == "BTCUSDT-PERP.BINANCE"
    assert point.mark_price == "100.125000"
    assert point.index_price == "100.00"
    assert point.latest_funding_rate == "-0.000325"
    assert point.historical_funding_rate == "0.00012500"
    assert Decimal(point.historical_funding_mark_price) == Decimal("99.999")
    assert point.mark_raw_json.startswith('{"symbol":')
    assert point.mark_sha256 != point.funding_sha256
    assert not hasattr(point, "cash_funding_payment")


def test_two_public_gets_are_fixed_and_ordered():
    seen = []
    mark, history = responses()
    clock_values = iter((T, T + 2_000_000_000, T + 2_000_000_000, T + 3_000_000_000))

    def get(url):
        seen.append(url)
        return json.dumps(mark if len(seen) == 1 else history).encode()

    point = fetch_public_market_sample(
        SESSION, SYMBOL, get=get, clock=lambda: next(clock_values)
    )
    assert seen == [
        "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
        "https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1",
    ]
    assert point.historical_funding_time_ms < point.venue_mark_time_ms


def test_durable_replay_and_append_only_restart(tmp_path):
    archive = store(tmp_path)
    a = parsed()
    archive.append(a)
    assert archive.samples() == (a,)
    archive.append(parsed(T + 20_000_000_000))
    reopened = store(tmp_path)
    assert len(reopened.samples()) == 2
    assert reopened.samples()[0] == a
    assert reopened.samples()[1].mark_start_ns > reopened.samples()[0].funding_end_ns


@pytest.mark.parametrize(
    ("mark", "funding", "error"),
    [
        ({"symbol": "ETHUSDT"}, {}, "symbol mismatch"),
        ({}, {"symbol": "ETHUSDT"}, "symbol mismatch"),
        ({"markPrice": "0"}, {}, "invalid mark price"),
        ({"indexPrice": "Infinity"}, {}, "invalid index price"),
        ({"markPrice": "-1"}, {}, "invalid mark price"),
        ({"markPrice": 100.125}, {}, "exact decimal string"),
        ({"markPrice": "NaN"}, {}, "invalid mark price"),
        ({"lastFundingRate": "1.5"}, {}, "unbounded latest funding"),
        ({"time": (T // MS) - 35_000}, {}, "stale"),
        ({"time": (T // MS) + 9_000}, {}, "venue clock is ahead"),
        ({"nextFundingTime": (T // MS)}, {}, "expired next-funding"),
        ({}, {"fundingTime": (T // MS) + 10_000}, "unobserved future"),
        ({}, {"fundingTime": (T // MS) - 9 * 86_400_000}, "too old"),
        ({}, {"markPrice": None}, "exact decimal string"),
        ({}, {"fundingRate": "nan"}, "invalid historical"),
    ],
)
def test_bad_public_payload_is_rejected_before_archiving(mark, funding, error):
    with pytest.raises(ValueError, match=error):
        parsed(mark=mark, funding=funding)


def test_no_arbitrary_symbols_or_sessions():
    for session, symbol in [(SESSION, "../BTCUSDT"), (SESSION, "ETHBTC"), ("../bad", SYMBOL)]:
        with pytest.raises(ValueError):
            fetch_public_market_sample(
                session, symbol,
                get=lambda _: (_ for _ in ()).throw(AssertionError("invalid symbol touched network")),
            )


def test_stale_and_reversed_capture_clock_fail_closed():
    mark, history = responses()
    for mark_before, mark_end, funding_before, funding_end in (
        (T, T - 1, T + 1, T + 2),
        (T, T + 11_000_000_000, T + 12_000_000_000, T + 13_000_000_000),
        (T, T + 1, T - 1, T + 1),
    ):
        with pytest.raises(ValueError, match=r"took too long|predates"):
            parse_public_market_sample(
                session_id=SESSION,
                symbol=SYMBOL,
                mark_raw=json.dumps(mark).encode(),
                funding_raw=json.dumps(history).encode(),
                mark_start_ns=mark_before,
                mark_end_ns=mark_end,
                funding_start_ns=funding_before,
                funding_end_ns=funding_end,
            )


def test_duplicate_source_keys_and_truncated_body_are_denied():
    mark, history = responses()
    raw = json.dumps(mark).replace('"markPrice":', '"markPrice": "99", "markPrice":')
    with pytest.raises(ValueError, match="invalid mark API JSON"):
        parse_public_market_sample(
            session_id=SESSION, symbol=SYMBOL,
            mark_raw=raw.encode(), funding_raw=json.dumps(history).encode(),
            mark_start_ns=T, mark_end_ns=T + 2_000_000_000,
            funding_start_ns=T + 2_000_000_000, funding_end_ns=T + 3_000_000_000,
        )


def test_missing_or_non_list_funding_response_is_rejected():
    mark, _history = responses()
    with pytest.raises(ValueError, match="unexpected public mark or historical funding response shape"):
        parse_public_market_sample(
            session_id=SESSION, symbol=SYMBOL,
            mark_raw=json.dumps(mark).encode(), funding_raw=b"{}",
            mark_start_ns=T, mark_end_ns=T + 2_000_000_000,
            funding_start_ns=T + 2_000_000_000, funding_end_ns=T + 3_000_000_000,
        )


def test_duplicate_or_reordered_samples_rejected(tmp_path):
    journal = store(tmp_path)
    point = parsed()
    journal.append(point)
    raw = journal.path.read_bytes()
    with pytest.raises(ValueError, match=r"overlapping|repeated"):
        journal.append(point)
    assert journal.path.read_bytes() == raw


def test_payload_mutation_even_with_valid_dataclass_fields_is_rejected(tmp_path):
    journal = store(tmp_path)
    point = parsed()
    with pytest.raises(ValueError, match="fields differ"):
        journal.append(replace(point, mark_price="200.0000"))
    assert journal.samples() == ()


def test_corrupted_journal_line_or_checkpoint_fail_closed(tmp_path):
    journal = store(tmp_path)
    journal.append(parsed())
    original = journal.path.read_bytes()
    journal.path.write_bytes(original.replace(b"100.125000", b"102.125000"))
    with pytest.raises(ValueError, match="hash mismatch"):
        journal.samples()
    journal.path.write_bytes(original.split(b"\n")[0] + b"\n")
    with pytest.raises(ValueError, match="checkpoint mismatch"):
        journal.samples()


def test_torn_suffix_and_checkpoint_write_failure_block_replay(tmp_path, monkeypatch):
    journal = store(tmp_path)
    old = journal.path.read_bytes()
    journal.path.write_bytes(old + b'{"broken":')
    with pytest.raises(ValueError, match="incomplete suffix"):
        journal.samples()
    journal.path.write_bytes(old)

    def broken(*_args, **_kwargs):
        raise OSError("storage failure")

    monkeypatch.setattr(evidence, "atomic_json", broken)
    with pytest.raises(OSError, match="storage failure"):
        journal.append(parsed())
    with pytest.raises(ValueError, match="checkpoint mismatch"):
        journal.samples()


def test_session_or_symlink_scope_fails_closed(tmp_path):
    journal = store(tmp_path)
    with pytest.raises(ValueError, match="session or source mismatch"):
        PublicMarketEvidenceJournal(journal.path, session_id="different", symbol=SYMBOL)
    link = tmp_path / "alias.jsonl"
    link.symlink_to(journal.path)
    with pytest.raises(ValueError, match="symbolic"):
        PublicMarketEvidenceJournal(link, session_id=SESSION, symbol=SYMBOL)


def test_repaired_record_digest_does_not_silently_authorize_cash(tmp_path):
    journal = store(tmp_path)
    journal.append(parsed())
    assert "funding_payment" not in json.dumps([s.__dict__ for s in journal.samples()])
    assert "realized_profit" not in json.dumps([s.__dict__ for s in journal.samples()])

def test_cli_archives_public_evidence_without_nautilus_trader(tmp_path, monkeypatch, capsys):
    pytest.importorskip("pandas")
    from types import SimpleNamespace

    from lab.cli import build_parser, cmd_public_market_sample

    monkeypatch.setattr(evidence, "fetch_public_market_sample", lambda session, symbol: parsed())
    args = build_parser().parse_args(["public-market-sample", SYMBOL, "--session", SESSION])
    assert cmd_public_market_sample(SimpleNamespace(results_dir=tmp_path), args) == 0
    archive = tmp_path / "fast" / f"public-mark-{SESSION}-{SYMBOL}.jsonl"
    journal = PublicMarketEvidenceJournal(archive, session_id=SESSION, symbol=SYMBOL)
    assert journal.samples() == (parsed(),)
    output = capsys.readouterr().out
    assert "no observed account payment" in output
    assert "NOT account/order evidence" in output


def test_failed_public_request_does_not_initialize_archive(tmp_path, monkeypatch):
    pytest.importorskip("pandas")
    from types import SimpleNamespace

    from lab.cli import build_parser, cmd_public_market_sample

    def unavailable(_session, _symbol):
        raise OSError("offline")

    monkeypatch.setattr(evidence, "fetch_public_market_sample", unavailable)
    args = build_parser().parse_args(["public-market-sample", SYMBOL, "--session", SESSION])
    with pytest.raises(SystemExit, match="BLOCKED: offline"):
        cmd_public_market_sample(SimpleNamespace(results_dir=tmp_path), args)
    assert not (tmp_path / "fast").exists()
