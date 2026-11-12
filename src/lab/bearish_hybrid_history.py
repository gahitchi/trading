"""Digest-pinned, pre-holdout JSON adapter for hybrid offline research.

No network, authenticated exchange feed, trial registration, or live orders.
A SHA-256 pin proves byte identity only, NOT who produced the archive.
Market/OI/funding completeness remains an external audit obligation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from bisect import bisect_right
from math import isfinite

from lab.bearish_hybrid import HOUR_NS, MINUTE_NS, Bar, FundingEvent, MarketEligibility, OIObservation
from lab.bearish_hybrid_replay import DAY_NS, HybridObservation

MAX_BARS = 100_000


def _pairs_no_duplicates(items: list[tuple[str, object]]) -> dict:
    obj: dict = {}
    for name, value in items:
        if name in obj:
            raise ValueError("duplicate archive JSON key")
        obj[name] = value
    return obj


def _fail_constant(_value: str) -> None:
    raise ValueError("nonfinite JSON numeric constant")


def _shape(value: object, keys: set[str], name: str) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise ValueError(f"malformed {name} schema")
    return value


def _integer(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"invalid {name} timestamp")
    return value


def _number(value: object, name: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float) or not isfinite(value) or (positive and value <= 0):
        raise ValueError(f"invalid {name} value")
    return float(value)


def _before_boundary(value: int, boundary_ns: int) -> None:
    if value >= boundary_ns:
        raise ValueError("protected holdout timestamp present in archive")


def load_hybrid_archive(
    content: bytes,
    *,
    expected_sha256: str,
    boundary_ns: int,
    funding_verified: bool = False,
) -> tuple[HybridObservation, ...]:
    """Validate a closed-minute archive before constructing public-as-of inputs.

    All source records must predate the frozen UTC midnight boundary. The
    caller has to establish *external* origin, completeness and trial
    preregistration; an archive's venue label/hash is not authenticity.
    Funding verification is an explicit external assertion, false by default.
    """
    if type(boundary_ns) is not int or boundary_ns <= 0 or boundary_ns % DAY_NS:
        raise ValueError("protected boundary must be a fixed UTC midnight")
    if type(funding_verified) is not bool:
        raise ValueError("funding verification must be an explicit boolean")
    if type(content) is not bytes or len(content) > 128_000_000:
        raise ValueError("invalid or oversized archive bytes")
    if (
        type(expected_sha256) is not str
        or len(expected_sha256) != 64
        or any(c not in "0123456789abcdef" for c in expected_sha256)
        or not hmac.compare_digest(hashlib.sha256(content).hexdigest(), expected_sha256)
    ):
        raise ValueError("archive SHA-256 mismatch")
    try:
        decoded = json.loads(content, object_pairs_hook=_pairs_no_duplicates, parse_constant=_fail_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("invalid archive JSON") from error
    bundle = _shape(decoded, {"version", "venue", "symbol", "bars", "oi", "funding", "market"}, "archive")
    if bundle["version"] != 1 or type(bundle["version"]) is not int:
        raise ValueError("unsupported archive version")
    if (
        bundle["venue"] != "binance-usdm"
        or type(bundle["symbol"]) is not str
        or not (
            5 <= len(bundle["symbol"]) <= 20
            and bundle["symbol"].isalnum()
            and bundle["symbol"].endswith("USDT")
            and bundle["symbol"].isupper()
        )
    ):
        raise ValueError("unsupported archive market")
    if (
        type(bundle["bars"]) is not list
        or not 1 <= len(bundle["bars"]) <= MAX_BARS
        or type(bundle["oi"]) is not list
        or type(bundle["funding"]) is not list
        or type(bundle["market"]) is not list
    ):
        raise ValueError("invalid archive series")

    bars: list[tuple[Bar, bool]] = []
    previous = -1
    for row in bundle["bars"]:
        r = _shape(
            row,
            {"end_ns", "open", "high", "low", "close", "quote_volume", "taker_buy_quote", "source_complete"},
            "bar",
        )
        end_ns = _integer(r["end_ns"], "bar")
        _before_boundary(end_ns, boundary_ns)
        if end_ns % MINUTE_NS or end_ns <= previous:
            raise ValueError("nonchronological or unaligned closed minute")
        previous = end_ns
        if type(r["source_complete"]) is not bool:
            raise ValueError("source_complete must be a boolean")
        bar = Bar(
            end_ns,
            _number(r["open"], "open", positive=True),
            _number(r["high"], "high", positive=True),
            _number(r["low"], "low", positive=True),
            _number(r["close"], "close", positive=True),
            _number(r["quote_volume"], "quote volume"),
            _number(r["taker_buy_quote"], "taker buy quote"),
        )
        bar.check()
        bars.append((bar, r["source_complete"]))

    oi: list[tuple[int, int, float]] = []
    previous_pub = -1
    previous_measurement = -1
    for row in bundle["oi"]:
        r = _shape(row, {"measured_ns", "published_ns", "value"}, "open interest")
        measured = _integer(r["measured_ns"], "OI measurement")
        pub = _integer(r["published_ns"], "OI publication")
        _before_boundary(measured, boundary_ns)
        _before_boundary(pub, boundary_ns)
        if measured > pub or measured <= previous_measurement or pub <= previous_pub:
            raise ValueError("noncausal or nonchronological OI publication")
        oi.append((pub, measured, _number(r["value"], "open interest", positive=True)))
        previous_pub, previous_measurement = pub, measured

    funding: list[FundingEvent] = []
    previous_settle = previous_publish = -1
    for row in bundle["funding"]:
        r = _shape(row, {"settlement_ns", "published_ns", "rate"}, "funding")
        settlement = _integer(r["settlement_ns"], "funding settlement")
        pub = _integer(r["published_ns"], "funding publication")
        _before_boundary(settlement, boundary_ns)
        _before_boundary(pub, boundary_ns)
        if settlement > pub or settlement <= previous_settle or pub < previous_publish:
            raise ValueError("noncausal or nonchronological funding settlement")
        funding.append(FundingEvent(settlement, pub, _number(r["rate"], "funding")))
        previous_settle, previous_publish = settlement, pub

    markets: list[MarketEligibility] = []
    previous_market_ns = -1
    for row in bundle["market"]:
        r = _shape(row, {"asof_ns", "spot_listed", "usdm_listed", "mean_daily_turnover_usdt_30d"}, "market metadata")
        asof = _integer(r["asof_ns"], "market metadata")
        _before_boundary(asof, boundary_ns)
        if asof <= previous_market_ns:
            raise ValueError("duplicate or nonchronological market metadata")
        if type(r["spot_listed"]) is not bool or type(r["usdm_listed"]) is not bool:
            raise ValueError("market listing metadata must use booleans")
        markets.append(
            MarketEligibility(
                asof,
                r["spot_listed"],
                r["usdm_listed"],
                _number(r["mean_daily_turnover_usdt_30d"], "30-day turnover"),
            )
        )
        previous_market_ns = asof

    oi_times = [r[0] for r in oi]
    funding_times = [r.published_ns for r in funding]
    market_times = [r.asof_ns for r in markets]
    output: list[HybridObservation] = []
    for bar, complete in bars:
        t = bar.end_ns
        latest_i = bisect_right(oi_times, t) - 1
        oi_snapshot = None
        if latest_i >= 1:
            last_pub, _measured, last_value = oi[latest_i]
            # Pick the closest known observation at least 5 minutes earlier,
            # rather than inventing a fixed OI refresh schedule.
            for prior_i in range(latest_i - 1, -1, -1):
                prior_pub, _, prior_value = oi[prior_i]
                age = last_pub - prior_pub
                if age >= 5 * MINUTE_NS:
                    if age <= 60 * MINUTE_NS:
                        oi_snapshot = OIObservation(prior_pub, prior_value, last_pub, last_value)
                    break
        last_funding = bisect_right(funding_times, t)
        funding_window = tuple(e for e in funding[:last_funding] if e.settlement_ns > t - 72 * HOUR_NS)
        latest_market_i = bisect_right(market_times, t) - 1
        market = markets[latest_market_i] if latest_market_i >= 0 else None
        output.append(HybridObservation(bar, market, oi_snapshot, funding_window, funding_verified, complete))
    return tuple(output)
