"""Bounded, keyless prospective public-data collection.

Forward-only: no orders, keys, balances, backtests, risk promotion, or fills.
A crashed or suspended process leaves an observable gap; nothing is backfilled.
Network failures are reported without creating artificial observations.
"""

from __future__ import annotations

import argparse
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError

from lab.bearish_hybrid import MINUTE_NS
from lab.bearish_hybrid_forward_attempts import record_attempt_finish, record_attempt_start
from lab.bearish_hybrid_forward_price import PriceCapture, record_forward_price
from lab.bearish_hybrid_public_capture import FeatureCapture, _validate_symbol, record_public_feature

POLL_OFFSET_NS = 5_000_000_000
MAX_CYCLES = 120
MAX_CONSECUTIVE_PRICE_NETWORK_FAILURES = 3
NETWORK_ERRORS = (URLError, TimeoutError, ConnectionError, ssl.SSLError)


@dataclass(frozen=True)
class PublicMinuteCapture:
    """Independent receipts, never a qualified or atomic feature bundle."""

    price: PriceCapture | None
    oi: FeatureCapture | None
    funding: FeatureCapture | None
    network_failures: tuple[str, ...] = ()
    qualified: bool = False
    actual_notional: int = 0


def capture_public_minute(root: Path, symbol: str, index: int) -> PublicMinuteCapture:
    """Attempt independent channels; absent receipts remain explicitly absent.

    Only transport/network errors are tolerated for the next UTC sampling
    attempt. Invalid payloads, malformed timestamps and integrity violations
    still raise immediately and must be investigated rather than ignored.
    """
    _validate_symbol(symbol)
    if type(index) is not int or index < 0:
        raise ValueError("nonnegative minute index required")

    failures: list[str] = []
    price: PriceCapture | None = None
    oi: FeatureCapture | None = None
    funding: FeatureCapture | None = None

    try:
        price = record_forward_price(root, symbol)
    except NETWORK_ERRORS as exc:
        failures.append(f"price:{type(exc).__name__}")

    if index % 6 == 0:
        try:
            oi = record_public_feature(root, symbol, "open-interest")
        except NETWORK_ERRORS as exc:
            failures.append(f"open-interest:{type(exc).__name__}")

    if index % 60 == 0:
        try:
            funding = record_public_feature(root, symbol, "settled-funding")
        except NETWORK_ERRORS as exc:
            failures.append(f"settled-funding:{type(exc).__name__}")

    return PublicMinuteCapture(price, oi, funding, tuple(failures))


def run_bounded_capture(
    root: Path, symbol: str, cycles: int, *, write_attempts: bool = True
) -> tuple[PublicMinuteCapture, ...]:
    """Sample future UTC minutes, never backfill a missed price bar.

    After three consecutive failed price network attempts, stop and request
    diagnostics rather than repeating blocked endpoint requests indefinitely.
    """
    _validate_symbol(symbol)
    if type(cycles) is not int or not 1 <= cycles <= MAX_CYCLES:
        raise ValueError("cycles must be between 1 and 120")
    if type(write_attempts) is not bool:
        raise ValueError("write_attempts must be bool")
    samples = []
    last_end_ns = -1
    consecutive_price_network_failures = 0
    for index in range(cycles):
        now = time.time_ns()
        next_poll_ns = (now // MINUTE_NS + 1) * MINUTE_NS + POLL_OFFSET_NS
        time.sleep(max(0.0, (next_poll_ns - now) / 1_000_000_000))
        intent = None
        if write_attempts:
            intent = record_attempt_start(
                root, symbol, next_poll_ns - POLL_OFFSET_NS, time.time_ns(), index
            )
        try:
            snapshot = capture_public_minute(root, symbol, index)
        except Exception as exc:
            if intent is not None:
                record_attempt_finish(root, intent, aborted_error=type(exc).__name__)
            raise
        if intent is not None:
            record_attempt_finish(
                root,
                intent,
                price_received_ns=snapshot.price.received_ns if snapshot.price else None,
                price_bar_end_ns=snapshot.price.bar.end_ns if snapshot.price else None,
                oi_received_ns=snapshot.oi.received_ns if snapshot.oi else None,
                funding_received_ns=snapshot.funding.received_ns if snapshot.funding else None,
                network_failures=snapshot.network_failures,
            )

        if snapshot.price is None:
            consecutive_price_network_failures += 1
            print(
                f"WARNING: no public price receipt for attempted poll {index + 1}; "
                f"network errors={snapshot.network_failures}; no backfill",
                flush=True,
            )
        else:
            consecutive_price_network_failures = 0
            bar_end_ns = snapshot.price.bar.end_ns
            if last_end_ns != -1 and bar_end_ns - last_end_ns != MINUTE_NS:
                print("WARNING: missing or duplicate closed UTC price minute; no backfill", flush=True)
            last_end_ns = bar_end_ns

        print(
            f"poll={index + 1}; "
            f"bar_end_ns={snapshot.price.bar.end_ns if snapshot.price else 'missing'}; "
            f"price={'yes' if snapshot.price else 'no'}; "
            f"OI={'yes' if snapshot.oi else 'no'}; "
            f"funding={'yes' if snapshot.funding else 'no'}; "
            f"network_failures={snapshot.network_failures}; qualified=False",
            flush=True,
        )
        samples.append(snapshot)
        if consecutive_price_network_failures >= MAX_CONSECUTIVE_PRICE_NETWORK_FAILURES:
            raise ConnectionError(
                "three consecutive minute-price network failures; stop and check endpoint TLS/network access"
            )
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
