"""Fundamental invariants: delisting is a corporate action, never fictional liquidity.

The test deliberately does not claim settlement is complete. It proves we fail
closed until Nautilus account/position state and our independent ledger can
reconcile without emitting an ordinary market fill.
"""

from datetime import date

import pandas as pd
import pytest

from lab import backtest, data


def _bars():
    idx = pd.bdate_range("2026-01-05", periods=4)
    return pd.DataFrame(
        {
            "open": [100.0] * 4,
            "high": [101.0] * 4,
            "low": [99.0] * 4,
            "close": [100.0] * 4,
            "volume": [10.0, 100.0, 1000.0, 10000.0],
        },
        index=idx,
    )


@pytest.mark.parametrize("recovery", [0.0, 0.01, 50.0, 100.0])
def test_no_phantom_exchange_print_even_with_recorded_recovery(recovery):
    bars = _bars()
    frame = {"D": bars}
    delist_date = bars.index[-1].date()
    evidence = {
        "D": {"delist_date": delist_date, "settlement_price": recovery, "source": "vendor", "assumed": False}
    }
    with pytest.raises(data.DataError, match="post-delisting market fills are forbidden"):
        backtest.settle_delistings(frame, None, evidence)
    # Failure cannot mutate the caller's bars into a synthetic executable market.
    assert frame["D"].equals(bars)
    assert frame["D"].index.max().date() == delist_date


def test_future_delisting_is_not_treated_as_earlier_executable_event():
    bars = _bars()
    frame = {"D": bars}
    membership = {"D": [(date(2026, 1, 1), None)]}
    out, windows, settlements = backtest.settle_delistings(
        frame,
        membership,
        {"D": {"delist_date": date(2027, 1, 1), "settlement_price": 0.0}},
    )
    assert out is frame
    assert windows is membership
    assert settlements == {}


def test_price_history_after_recorded_delisting_is_a_provenance_error():
    bars = _bars()
    with pytest.raises(data.DataError, match="prices continue past"):
        backtest.settle_delistings(
            {"D": bars},
            None,
            {"D": {"delist_date": bars.index[-2].date(), "settlement_price": 0.0}},
        )


@pytest.mark.parametrize(
    ("delist_return", "settlement_price"),
    [
        ("nan", ""),
        ("inf", ""),
        ("-inf", ""),
        ("", "nan"),
        ("", "inf"),
        ("", "-inf"),
        ("", "-1"),
    ],
)
def test_delisting_import_rejects_nonfinite_or_negative_recoveries(goog_lab, tmp_path, delist_return, settlement_price):
    path = tmp_path / "untrusted.csv"
    path.write_text(
        "ticker,delist_date,delist_type,delist_return,settlement_price,source\n"
        f"D,2026-01-08,merger,{delist_return},{settlement_price},vendor\n"
    )
    with pytest.raises(data.DataError, match="finite"):
        data.save_delistings(goog_lab, path)


@pytest.mark.parametrize(
    "tamper",
    [
        "D,2026-01-08,merger,nan,,vendor",
        "D,2026-01-08,merger,,-inf,vendor",
        "D,2026-01-08,merger,-0.5,,",
        "D,2026-01-08,merger,-0.5,,vendor\nD,2026-01-08,merger,-0.5,,vendor",
    ],
)
def test_stored_delisting_data_is_revalidated_after_import(goog_lab, tmp_path, tamper):
    header = "ticker,delist_date,delist_type,delist_return,settlement_price,source\n"
    path = tmp_path / "source.csv"
    path.write_text(header + "D,2026-01-08,merger,-0.5,,vendor\n")
    data.save_delistings(goog_lab, path)
    # An imported CSV is not a permanently trusted artifact.
    data.delistings_path(goog_lab).write_text(header + tamper + "\n")
    with pytest.raises(data.DataError):
        data.delistings(goog_lab)


def test_delisting_evidence_revision_creates_new_research_identity(goog_lab, tmp_path):
    from lab import experiments

    path = tmp_path / "revision.csv"
    header = "ticker,delist_date,delist_type,delist_return,settlement_price,source\n"
    before = experiments.config_fingerprint(goog_lab)
    path.write_text(header + "D,2026-01-08,merger,-0.5,,vendor-a\n")
    data.save_delistings(goog_lab, path)
    first = experiments.config_fingerprint(goog_lab)
    path.write_text(header + "D,2026-01-08,merger,-0.5,,vendor-b\n")
    data.save_delistings(goog_lab, path)
    second = experiments.config_fingerprint(goog_lab)
    assert len({before, first, second}) == 3
