"""Strict research diagnostic for Binance USD-M, one-way isolated short margin.

A pinned leverageBracket response supplies the venue's notional tiers, while
pinned *mark-price* minute candles supply the observed risk path. Mark, not
last-trade, is the liquidation reference. Hashes prove local identity only;
this module DOES NOT authenticate Binance or replicate its liquidation engine.
No API credentials, network, account mutation or orders.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

MODEL = "binance-usdm-isolated-oneway-short-margin-diagnostic-v1"
MINUTE_MS = 60_000
MAX_MARK_BARS = 10_080
MAX_CAPTURE_LAG_MS = 60_000
MAX_MARK_LAG_MS = 60_000


def source_sha256(data: object) -> str:
    """Pin a JSON-compatible source before invoking a diagnostic."""
    raw = json.dumps(data, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _digest(digest: str) -> None:
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("source SHA-256 must be a lowercase hexadecimal digest")


def _decimal(raw: object, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
        raise ValueError(f"{field} requires a finite decimal value")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError(f"invalid {field} decimal") from exc
    if not value.is_finite() or (positive and value <= 0):
        raise ValueError(f"invalid {field} decimal")
    return value


@dataclass(frozen=True)
class NotionalTier:
    bracket: int
    floor: Decimal
    cap: Decimal
    maintenance_rate: Decimal
    cum: Decimal
    max_initial_leverage: int


@dataclass(frozen=True)
class MarginSchedule:
    symbol: str
    source_sha256: str
    tiers: tuple[NotionalTier, ...]

    def tier(self, notional: Decimal) -> NotionalTier:
        if notional < 0 or not notional.is_finite():
            raise ValueError("invalid position notional")
        for item in self.tiers:
            if item.floor <= notional < item.cap:
                return item
        raise ValueError("position notional exceeds pinned leverage-bracket coverage")


def parse_leverage_brackets(raw: object, *, symbol: str, expected_sha256: str) -> MarginSchedule:
    """Validate one account's complete source-pinned GET /fapi/v1/leverageBracket item.

    The account's notionalCoef is not safely interpretable unless it is 1.
    Only isolated one-way USDT-margined contracts are supported by the caller.
    Never substitute exchangeInfo maintMarginPercent or a generic BTC tier.
    """
    _digest(expected_sha256)
    if source_sha256(raw) != expected_sha256:
        raise ValueError("leverage-bracket source SHA-256 mismatch")
    if isinstance(raw, list):
        if len(raw) != 1:
            raise ValueError("need exactly one symbol's leverage brackets")
        raw = raw[0]
    if not isinstance(raw, dict) or not {"symbol", "brackets"} <= set(raw):
        raise ValueError("invalid leverage-bracket response")
    if set(raw) - {"symbol", "brackets", "notionalCoef"}:
        raise ValueError("unknown leverage-bracket response fields")
    if not isinstance(symbol, str) or not symbol.endswith("USDT") or not symbol[:-4].isalnum():
        raise ValueError("only explicit USDT-margined USD-M symbol supported")
    if raw["symbol"] != symbol or _decimal(raw.get("notionalCoef", "1"), "notionalCoef") != 1:
        raise ValueError("symbol mismatch or unsupported account notional coefficient")
    rows = raw["brackets"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("missing or excessive notional tiers")
    tiers: list[NotionalTier] = []
    previous: NotionalTier | None = None
    with localcontext() as ctx:
        ctx.prec = 70
        for i, row in enumerate(rows, 1):
            if not isinstance(row, dict) or set(row) != {
                "bracket", "initialLeverage", "notionalCap", "notionalFloor", "maintMarginRatio", "cum"
            }:
                raise ValueError("incomplete or unexpected leverage tier fields")
            if type(row["bracket"]) is not int or row["bracket"] != i:
                raise ValueError("unordered notional bracket IDs")
            leverage = row["initialLeverage"]
            if type(leverage) is not int or not 1 <= leverage <= 1000:
                raise ValueError("invalid maximum bracket leverage")
            floor = _decimal(row["notionalFloor"], "notionalFloor")
            cap = _decimal(row["notionalCap"], "notionalCap")
            ratio = _decimal(row["maintMarginRatio"], "maintMarginRatio", positive=True)
            cum = _decimal(row["cum"], "cum")
            if floor < 0 or cap <= floor or not 0 < ratio < 1 or cum < 0:
                raise ValueError("invalid margin tier bounds or maintenance rate")
            tier = NotionalTier(i, floor, cap, ratio, cum, leverage)
            if previous is None:
                if floor != 0 or cum != 0:
                    raise ValueError("first notional bracket must begin at zero with zero cum")
            else:
                if (
                    floor != previous.cap
                    or ratio < previous.maintenance_rate
                    or leverage > previous.max_initial_leverage
                ):
                    raise ValueError("tier coverage, rate or leverage discontinuity")
                expected_cum = previous.cum + floor * (ratio - previous.maintenance_rate)
                if abs(cum - expected_cum) > Decimal("0.000001"):
                    raise ValueError("maintenance cum is inconsistent at tier transition")
            tiers.append(tier)
            previous = tier
    return MarginSchedule(symbol, expected_sha256, tuple(tiers))


@dataclass(frozen=True)
class IsolatedShort:
    symbol: str
    quantity: Decimal
    entry_fill_price: Decimal
    isolated_wallet_after_entry: Decimal
    leverage: int
    funding_debit_reserve: Decimal = Decimal("0")
    close_fee_bps: Decimal = Decimal("0")
    buffer_bps: Decimal = Decimal("0")
    margin_type: str = "ISOLATED"
    position_side: str = "BOTH"
    multi_assets: bool = False


@dataclass(frozen=True)
class MarkEvidence:
    raw_candles: list
    source_sha256: str
    entry_minute_start_ms: int
    bracket_capture_ms: int
    assessment_ms: int


@dataclass(frozen=True)
class IsolatedShortRisk:
    model: str
    symbol: str
    bracket_source_sha256: str
    mark_source_sha256: str
    mark_price: Decimal
    worst_observed_mark: Decimal
    current_maintenance: Decimal
    current_margin_balance: Decimal
    current_excess: Decimal
    worst_maintenance: Decimal
    worst_margin_balance: Decimal
    worst_excess: Decimal
    modeled_liquidation_mark: Decimal
    status: str
    authenticated_exchange_state: bool = False
    execution_authorized: bool = False
    qualified: bool = False


def _mark_prices(data: MarkEvidence, *, expected_sha256: str) -> tuple[Decimal, Decimal]:
    _digest(expected_sha256)
    if expected_sha256 != data.source_sha256 or source_sha256(data.raw_candles) != expected_sha256:
        raise ValueError("mark-price source SHA-256 mismatch")
    if (
        type(data.entry_minute_start_ms) is not int
        or type(data.bracket_capture_ms) is not int
        or type(data.assessment_ms) is not int
        or data.entry_minute_start_ms <= 0
        or data.entry_minute_start_ms % MINUTE_MS
        or data.bracket_capture_ms > data.assessment_ms
        or data.bracket_capture_ms < data.assessment_ms - MAX_CAPTURE_LAG_MS
    ):
        raise ValueError("missing, stale or unordered risk snapshot timestamps")
    bars = data.raw_candles
    if not isinstance(bars, list) or not 1 <= len(bars) <= MAX_MARK_BARS:
        raise ValueError("missing or excessive mark-price minute coverage")
    highest = Decimal(0)
    last_close = Decimal(0)
    for i, bar in enumerate(bars):
        if not isinstance(bar, (list, tuple)) or len(bar) != 12:
            raise ValueError("invalid mark-price kline schema")
        opening, close_time = bar[0], bar[6]
        if (
            type(opening) is not int
            or type(close_time) is not int
            or opening != data.entry_minute_start_ms + i * MINUTE_MS
            or close_time != opening + MINUTE_MS - 1
        ):
            raise ValueError("mark-price evidence contains missing, overlapping or misaligned minutes")
        open_px = _decimal(bar[1], "mark open", positive=True)
        high_px = _decimal(bar[2], "mark high", positive=True)
        low_px = _decimal(bar[3], "mark low", positive=True)
        last_close = _decimal(bar[4], "mark close", positive=True)
        if high_px < max(open_px, last_close) or low_px > min(open_px, last_close) or low_px > high_px:
            raise ValueError("inconsistent mark-price OHLC")
        highest = max(highest, high_px)
    if (
        close_time > data.assessment_ms
        or data.assessment_ms - close_time > MAX_MARK_LAG_MS
        or data.bracket_capture_ms < data.entry_minute_start_ms
    ):
        raise ValueError("stale, future or prematurely captured risk evidence")
    return last_close, highest


def _maintenance(schedule: MarginSchedule, quantity: Decimal, mark: Decimal) -> tuple[Decimal, NotionalTier]:
    notion = quantity * mark
    tier = schedule.tier(notion)
    mm = notion * tier.maintenance_rate - tier.cum
    if mm < 0:
        raise ValueError("invalid negative maintenance calculation")
    return mm, tier


def _liquidation_mark(
    schedule: MarginSchedule,
    *,
    quantity: Decimal,
    entry_price: Decimal,
    isolated_wallet: Decimal,
    reserved_debit: Decimal,
    adverse_exit_fraction: Decimal,
) -> Decimal:
    """Solve tier-consistent one-way isolated threshold; not exchange liquidation output."""
    with localcontext() as ctx:
        ctx.prec = 70
        solutions = []
        for tier in schedule.tiers:
            point = (isolated_wallet - reserved_debit + quantity * entry_price + tier.cum) / (
                quantity * (1 + tier.maintenance_rate + adverse_exit_fraction)
            )
            if point > 0 and tier.floor <= quantity * point < tier.cap:
                solutions.append(point)
        if len(solutions) != 1:
            raise ValueError("unable to establish unique tier-consistent margin threshold")
        return solutions[0]


def assess_isolated_short(
    raw_brackets: object,
    position: IsolatedShort,
    mark_data: MarkEvidence,
    *,
    expected_bracket_sha256: str,
    expected_mark_sha256: str,
) -> IsolatedShortRisk:
    """Conservatively veto apparent liquidation exposure; never assert survival.

    Requires a single one-way ISOLATED USDT margin position and complete
    Binance mark-price 1m candles from position entry through assessment.
    Does NOT accept last-price klines as a substitute for mark-price klines;
    provenance still requires external authentication beyond a digest.
    """
    if position.margin_type != "ISOLATED" or position.position_side != "BOTH" or position.multi_assets is not False:
        raise ValueError("only one-way isolated single-asset USDT margin supported")
    if type(position.leverage) is not int or position.leverage < 1:
        raise ValueError("invalid isolated initial leverage")
    q = _decimal(position.quantity, "quantity", positive=True)
    entry = _decimal(position.entry_fill_price, "entry fill", positive=True)
    wallet = _decimal(position.isolated_wallet_after_entry, "isolated wallet", positive=True)
    reserve = _decimal(position.funding_debit_reserve, "funding debit")
    fee = _decimal(position.close_fee_bps, "exit fee bps")
    buffer = _decimal(position.buffer_bps, "risk buffer bps")
    if reserve < 0 or fee < 0 or buffer < 0 or fee + buffer >= 10_000:
        raise ValueError("invalid adverse funding or liquidation fee/buffer assumptions")
    schedule = parse_leverage_brackets(raw_brackets, symbol=position.symbol, expected_sha256=expected_bracket_sha256)
    mark, high = _mark_prices(mark_data, expected_sha256=expected_mark_sha256)
    with localcontext() as ctx:
        ctx.prec = 70
        entry_notional = q * entry
        initial_tier = schedule.tier(entry_notional)
        if position.leverage > initial_tier.max_initial_leverage:
            raise ValueError("requested leverage exceeds account notional bracket maximum")
        if wallet - reserve < entry_notional / Decimal(position.leverage):
            raise ValueError("insufficient isolated wallet for pinned initial margin")
        mm_now, tier_now = _maintenance(schedule, q, mark)
        mm_high, tier_high = _maintenance(schedule, q, high)
        if position.leverage > min(tier_now.max_initial_leverage, tier_high.max_initial_leverage):
            raise ValueError("position leverage exceeds stressed notional bracket maximum")
        adverse_fraction = (fee + buffer) / Decimal(10_000)
        eq_now = wallet - reserve + q * (entry - mark)
        eq_high = wallet - reserve + q * (entry - high)
        excess_now = eq_now - mm_now - q * mark * adverse_fraction
        excess_high = eq_high - mm_high - q * high * adverse_fraction
        threshold = _liquidation_mark(
            schedule,
            quantity=q,
            entry_price=entry,
            isolated_wallet=wallet,
            reserved_debit=reserve,
            adverse_exit_fraction=adverse_fraction,
        )
        breached = excess_now <= 0 or excess_high <= 0 or high >= threshold
        return IsolatedShortRisk(
            model=MODEL,
            symbol=position.symbol,
            bracket_source_sha256=expected_bracket_sha256,
            mark_source_sha256=expected_mark_sha256,
            mark_price=mark,
            worst_observed_mark=high,
            current_maintenance=mm_now,
            current_margin_balance=eq_now,
            current_excess=excess_now,
            worst_maintenance=mm_high,
            worst_margin_balance=eq_high,
            worst_excess=excess_high,
            modeled_liquidation_mark=threshold,
            status="MARK_ENVELOPE_BREACH_POSSIBLE" if breached else "NO_MARK_ENVELOPE_BREACH_OBSERVED_UNQUALIFIED",
        )
