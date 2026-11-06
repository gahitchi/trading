"""Portable and replay-verified sandbox/public-market temporal alignment."""

import json

import pytest

from lab.paper_account_snapshot import PaperAccountSnapshotJournal
from lab.paper_mark_alignment import align_paper_account_mark
from lab.public_market_evidence import PublicMarketEvidenceJournal, parse_public_market_sample
from lab.sandbox_capture import PaperOrderEventCapture
from tests.test_paper_account_snapshot import snapshot
from tests.test_sandbox_capture import IID, NS, event

SYMBOL = "BTCUSDT"
SESSION = "session-1"
SECOND = 1_000_000_000


def public_sample(
    start: int,
    *,
    session: str = SESSION,
    symbol: str = SYMBOL,
    mark_time_ms: int | None = None,
    mark_price: str = "101.5000",
):
    t = start // 1_000_000
    mark = {
        "symbol": symbol,
        "markPrice": mark_price,
        "indexPrice": "101.25",
        "lastFundingRate": "-0.0002",
        "nextFundingTime": t + 4 * 3_600_000,
        "time": t + 1000 if mark_time_ms is None else mark_time_ms,
    }
    history = [
        {
            "symbol": symbol,
            "fundingTime": t - 8 * 3_600_000,
            "fundingRate": "0.00015",
            "markPrice": "100.00",
        }
    ]
    return parse_public_market_sample(
        session_id=session,
        symbol=symbol,
        mark_raw=json.dumps(mark).encode(),
        funding_raw=json.dumps(history).encode(),
        mark_start_ns=start,
        mark_end_ns=start + 2 * SECOND,
        funding_start_ns=start + 2 * SECOND,
        funding_end_ns=start + 3 * SECOND,
    )


def evidence_pair(tmp_path, *, account_ns=NS + 10 * SECOND, sample_ns=NS + 4 * SECOND):
    orders = PaperOrderEventCapture(tmp_path / "orders.jsonl", session_id=SESSION, instrument_id=IID)
    orders.append(event(), received_ns=NS + 2)
    account = PaperAccountSnapshotJournal(tmp_path / "account.jsonl", orders, account_id="sandbox-1")
    account.append(
        snapshot(
            orders,
            identifier="snap-a",
            captured_ns=account_ns,
            positions={IID: "-0.25"},
        )
    )
    public = PublicMarketEvidenceJournal(tmp_path / "marks.jsonl", session_id=SESSION, symbol=SYMBOL)
    if sample_ns is not None:
        public.append(public_sample(sample_ns))
    return orders, account, public


def test_recent_mark_is_diagnostic_and_never_posts_funding_or_equity(tmp_path):
    _orders, account, public = evidence_pair(tmp_path)
    result = align_paper_account_mark(account, public)
    assert result["status"] == "temporally_aligned_unqualified"
    assert result["mark_price"] == "101.5000"
    assert result["index_price"] == "101.25"
    assert result["mark_age_ns"] == 5 * SECOND
    assert result["collection_age_ns"] == 4 * SECOND
    assert result["historical_funding_rate"] == "0.00015"
    assert result["account_observed_ns"] == NS + 10 * SECOND
    assert result["capture_count"] == 1
    assert result["funding_payment_verified"] is False
    assert result["economic_posting_allowed"] is False
    assert result["risk_ready"] is False
    assert result["equity_reconciled"] is False
    assert result["clock_domain_independently_verified"] is False


def test_archives_can_reopen_and_produce_same_mark_anchor(tmp_path):
    orders, account, public = evidence_pair(tmp_path)
    first = align_paper_account_mark(account, public)
    recreated_account = PaperAccountSnapshotJournal(account.path, orders, account_id="sandbox-1")
    recreated_public = PublicMarketEvidenceJournal(public.path, session_id=SESSION, symbol=SYMBOL)
    assert align_paper_account_mark(recreated_account, recreated_public) == first


def test_empty_market_or_empty_account_blocks(tmp_path):
    _orders, account, public = evidence_pair(tmp_path, sample_ns=None)
    with pytest.raises(ValueError, match="no validated mark"):
        align_paper_account_mark(account, public)
    orders = PaperOrderEventCapture(tmp_path / "blank-orders.jsonl", session_id=SESSION, instrument_id=IID)
    empty_account = PaperAccountSnapshotJournal(tmp_path / "blank-account.jsonl", orders, account_id="sandbox-1")
    with pytest.raises(ValueError, match="no validated snapshot"):
        align_paper_account_mark(empty_account, public)


def test_market_session_and_instrument_must_match(tmp_path):
    _orders, account, _public = evidence_pair(tmp_path)
    wrong_session = PublicMarketEvidenceJournal(
        tmp_path / "different-session.jsonl", session_id="different", symbol=SYMBOL
    )
    wrong_session.append(public_sample(NS + 4 * SECOND, session="different"))
    with pytest.raises(ValueError, match="session mismatch"):
        align_paper_account_mark(account, wrong_session)
    wrong_symbol = PublicMarketEvidenceJournal(
        tmp_path / "different-instrument.jsonl", session_id=SESSION, symbol="ETHUSDT"
    )
    wrong_symbol.append(public_sample(NS + 4 * SECOND, symbol="ETHUSDT"))
    with pytest.raises(ValueError, match="outside the account scope"):
        align_paper_account_mark(account, wrong_symbol)


def test_historical_rate_fetch_after_snapshot_must_not_be_used(tmp_path):
    _orders, account, public = evidence_pair(tmp_path, account_ns=NS + 6 * SECOND, sample_ns=NS + 4 * SECOND)
    with pytest.raises(ValueError, match="no fully observed"):
        align_paper_account_mark(account, public)


def test_future_venue_mark_timestamp_never_anchors_earlier_account(tmp_path):
    _orders, account, public = evidence_pair(tmp_path, account_ns=NS + 7 * SECOND + 200_000_000, sample_ns=None)
    public.append(
        public_sample(
            NS + 4 * SECOND,
            mark_time_ms=(NS + 7 * SECOND + 500_000_000) // 1_000_000,
        )
    )
    with pytest.raises(ValueError, match="no fully observed"):
        align_paper_account_mark(account, public)


@pytest.mark.parametrize(
    "start",
    [
        NS - 50 * SECOND,
        NS - 10 * SECOND,
        NS - 2 * SECOND,
    ],
)
def test_old_market_mark_fails_explicit_freshness(tmp_path, start):
    _orders, account, public = evidence_pair(tmp_path, sample_ns=start)
    with pytest.raises(ValueError, match="stale"):
        align_paper_account_mark(account, public)


def test_explicit_bounded_age_can_relax_only_to_one_minute(tmp_path):
    _orders, account, public = evidence_pair(tmp_path, sample_ns=NS - 10 * SECOND)
    report = align_paper_account_mark(account, public, max_age_ns=30 * SECOND)
    assert report["mark_age_ns"] == 19 * SECOND
    assert report["risk_ready"] is False
    with pytest.raises(ValueError, match="bounded"):
        align_paper_account_mark(account, public, max_age_ns=61 * SECOND)
    with pytest.raises(ValueError, match="bounded"):
        align_paper_account_mark(account, public, max_age_ns=True)


def test_newer_market_sample_not_available_to_earlier_account_is_ignored(tmp_path):
    _orders, account, public = evidence_pair(tmp_path)
    public.append(public_sample(NS + 13 * SECOND, mark_price="999.99"))
    report = align_paper_account_mark(account, public)
    assert report["mark_price"] == "101.5000"
    assert report["mark_age_ns"] == 5 * SECOND


def test_newer_eligible_public_sample_is_selected(tmp_path):
    _orders, account, public = evidence_pair(tmp_path, account_ns=NS + 35 * SECOND, sample_ns=NS + 4 * SECOND)
    public.append(public_sample(NS + 29 * SECOND, mark_price="99.9900"))
    assert align_paper_account_mark(account, public)["mark_price"] == "99.9900"


def test_stale_older_mark_cannot_rescue_a_too_new_mark(tmp_path):
    _orders, account, public = evidence_pair(tmp_path, account_ns=NS + 35 * SECOND, sample_ns=NS + 4 * SECOND)
    public.append(public_sample(NS + 34 * SECOND, mark_price="99.9900"))
    with pytest.raises(ValueError, match="stale"):
        align_paper_account_mark(account, public)


def test_corrupted_public_archive_blocks_even_if_current_mark_was_valid(tmp_path):
    _orders, account, public = evidence_pair(tmp_path)
    raw = public.path.read_bytes()
    public.path.write_bytes(raw.replace(b"101.5000", b"101.6000"))
    with pytest.raises(ValueError, match="hash mismatch"):
        align_paper_account_mark(account, public)


def test_corrupted_account_receipts_block_mark_alignment(tmp_path):
    orders, account, public = evidence_pair(tmp_path)
    raw = orders.path.read_bytes()
    orders.path.write_bytes(raw.replace(b"trade-1", b"trade-X"))
    with pytest.raises(ValueError, match="hash mismatch"):
        align_paper_account_mark(account, public)


def test_altered_account_snapshot_checkpoint_blocks(tmp_path):
    _orders, account, public = evidence_pair(tmp_path)
    account._head.unlink()
    with pytest.raises(ValueError, match="checkpoint missing"):
        align_paper_account_mark(account, public)


def test_replacing_historical_funding_rate_cannot_create_payment(tmp_path):
    _orders, account, public = evidence_pair(tmp_path)
    assert align_paper_account_mark(account, public)["funding_payment_verified"] is False
    sample = public.samples()[0]
    assert sample.historical_funding_rate == "0.00015"
    assert not hasattr(sample, "account_funding_payment")


def test_readonly_cli_aligns_without_modifying_evidence(tmp_path, capsys):
    pytest.importorskip("pandas")
    from lab.cli import build_parser, cmd_paper_mark_align

    orders, account, public = evidence_pair(tmp_path)
    originals = {p: p.read_bytes() for p in (orders.path, account.path, public.path)}
    args = build_parser().parse_args(
        [
            "paper-mark-align",
            "--orders",
            str(orders.path),
            "--snapshots",
            str(account.path),
            "--market",
            str(public.path),
            "--session",
            SESSION,
            "--symbol",
            SYMBOL,
            "--account",
            "sandbox-1",
        ]
    )
    assert cmd_paper_mark_align(None, args) == 0
    assert '"economic_posting_allowed": false' in capsys.readouterr().out
    assert {p: p.read_bytes() for p in originals} == originals


def test_readonly_cli_rejects_missing_account_and_market_journals(tmp_path):
    pytest.importorskip("pandas")
    from lab.cli import build_parser, cmd_paper_mark_align

    orders, account, public = evidence_pair(tmp_path)
    missing = tmp_path / "missing.jsonl"
    paths = [
        "--orders",
        str(orders.path),
        "--snapshots",
        str(account.path),
        "--market",
        str(missing),
        "--session",
        SESSION,
        "--symbol",
        SYMBOL,
        "--account",
        "sandbox-1",
    ]
    with pytest.raises(SystemExit, match="preexisting"):
        cmd_paper_mark_align(None, build_parser().parse_args(["paper-mark-align", *paths]))
    assert not missing.exists()
    assert not missing.with_name(missing.name + ".head.json").exists()
    paths[paths.index(str(missing))] = str(public.path)
    account._head.unlink()
    with pytest.raises(SystemExit, match="preexisting"):
        cmd_paper_mark_align(None, build_parser().parse_args(["paper-mark-align", *paths]))


def test_cli_rejects_clock_domain_mismatch_with_simulated_epoch(tmp_path):
    pytest.importorskip("pandas")
    from lab.cli import build_parser, cmd_paper_mark_align

    orders, account, public = evidence_pair(tmp_path, sample_ns=None)
    public.append(public_sample(1_700_000_000_000_000_000))
    args = build_parser().parse_args(
        [
            "paper-mark-align",
            "--orders",
            str(orders.path),
            "--snapshots",
            str(account.path),
            "--market",
            str(public.path),
            "--session",
            SESSION,
            "--symbol",
            SYMBOL,
            "--account",
            "sandbox-1",
        ]
    )
    with pytest.raises(SystemExit, match="stale"):
        cmd_paper_mark_align(None, args)
