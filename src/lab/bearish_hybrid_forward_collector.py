"""Bounded, keyless prospective public-data collection.

Forward-only: no orders, keys, balances, backtests, risk promotion, or fills.
A crashed or suspended process leaves an observable gap; nothing is backfilled.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path

from lab.bearish_hybrid import MINUTE_NS
from lab.bearish_hybrid_forward_price import PriceCapture, record_forward_price
from lab.bearish_hybrid_public_capture import FeatureCapture, _validate_symbol, record_public_feature

POLL_OFFSET_NS = 5_000_000_000
MAX_CYCLES = 120


@dataclass(frozen=True)
class PublicMinuteCapture:
    """Independent receipts, never a qualified or atomic feature bundle."""

    price: PriceCapture
    oi: FeatureCapture | None
    funding: FeatureCapture | None
    qualified: bool = False
    actual_notional: int = 0


def capture_public_minute(root: Path, symbol: str, index: int) -> PublicMinuteCapture:
    """Record one closed public minute, OI each 6 polls, funding each 60 polls.

    A partial failure aborts the call but does not erase immutable receipts.
    These are NOT logically atomic exchange snapshots or verified features.
    """
    _validate_symbol(symbol)
    if type(index) is not int or index < 0:
        raise ValueError("nonnegative minute index required")
    price = record_forward_price(root, symbol)
    oi = record_public_feature(root, symbol, "open-interest") if index % 6 == 0 else None
    funding = record_public_feature(root, symbol, "settled-funding") if index % 60 == 0 else None
    return PublicMinuteCapture(price, oi, funding)


def run_bounded_capture(root: Path, symbol: str, cycles: int) -> tuple[PublicMinuteCapture, ...]:
    """Sample future UTC minutes, recording all missed-minute gaps honestly.

    The process must remain active. No daemon, remote monitoring, recovery
    backfill, or future research qualification is implied.
    """
    _validate_symbol(symbol)
    if type(cycles) is not int or not 1 <= cycles <= MAX_CYCLES:
        raise ValueError("cycles must be between 1 and 120")
    samples = []
    last_end_ns = -1
    for index in range(cycles):
        now = time.time_ns()
        next_poll_ns = (now // MINUTE_NS + 1) * MINUTE_NS + POLL_OFFSET_NS
        time.sleep(max(0.0, (next_poll_ns - now) / 1_000_000_000))
        snapshot = capture_public_minute(root, symbol, index)
        bar_end_ns = snapshot.price.bar.end_ns
        if last_end_ns != -1 and bar_end_ns - last_end_ns != MINUTE_NS:
            print("WARNING: missing or duplicate closed UTC price minute; no backfill", flush=True)
        last_end_ns = bar_end_ns
        print(
            f"UTC bar end ns={bar_end_ns}; price received ns={snapshot.price.received_ns}; "
            f"OI={'yes' if snapshot.oi else 'no'}; "
            f"funding={'yes' if snapshot.funding else 'no'}; qualified=False",
            flush=True,
        )
        samples.append(snapshot)
    return tuple(samples)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded keyless Binance USD-M forward evidence (no trading)")
    parser.add_argument("--root", type=Path, default=Path.home() / "trading-forward-evidence")
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--minutes", type=int, required=True, help="Number of future minute polls (1-120)")
    args = parser.parse_args(argv)
    run_bounded_capture(args.root, args.symbol, args.minutes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
