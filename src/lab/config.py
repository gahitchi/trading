"""Load and validate lab.toml into typed settings."""

from __future__ import annotations

import itertools
import math
import os
import tomllib
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path

VALID_VENUES = {"XNAS", "XNYS", "ARCX", "BATS", "XASE"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Period:
    name: str
    start: date
    end: date | None  # None = up to the latest available data


@dataclass(frozen=True)
class CostSettings:
    commission_per_share: float
    commission_min: float
    commission_max_pct: float
    slippage: bool
    open_print_fraction: float
    min_trade_value: float
    rebalance_band: float
    cash_buffer: float


@dataclass(frozen=True)
class RiskSettings:
    """Exposure limits as fractions of equity. Weights are signed: negative = short."""

    max_weight: float  # per stock, |weight|
    max_gross: float  # sum of |weights|; above 1 = leverage
    max_drawdown: float
    max_short_gross: float = 0.0  # sum of short |weights|; 0 = long-only
    min_net: float = 0.0  # net exposure (longs - shorts) range
    max_net: float = 1.0
    profile: str = "custom"  # custom preserves pre-profile settings


@dataclass(frozen=True)
class MarginSettings:
    """Broker margin rules (Reg T / FINRA minimums by default; IBKR's house rules can be stricter) and the
    financing assumptions used where no point-in-time data exists. Requirements are fractions of position value."""

    initial_long: float = 0.50
    initial_short: float = 0.50
    maintenance_long: float = 0.25
    maintenance_short: float = 0.30
    cushion: float = 0.10  # stay this far above the requirement; deleverage before the broker would
    borrow_apr: float = 0.005  # ASSUMED stock-borrow fee for easy-to-borrow names (no historical fee data)
    debit_apr: float = 0.06  # ASSUMED interest on borrowed cash (IBKR: benchmark rate + spread; set the current one)


@dataclass(frozen=True)
class PortfolioSettings:
    target_vol: float
    vol_window: int
    max_leverage: float = 1.0  # ceiling on the volatility-targeting scale (1 = scales down only)
    leverage: float = 1.0  # fixed exposure multiplier when volatility targeting is off


@dataclass(frozen=True)
class PaperSettings:
    host: str
    port: int
    client_id: int
    account_id: str
    market_data: str
    capital: float
    rebalance_time: str
    signal_time: str
    max_order_value: float
    max_orders_per_day: int
    max_daily_turnover: float
    order_rate: str
    # The lab's book is an ALLOCATION of the paper account (capital) unless dedicated_account: then it owns
    # the account and its cash must match the broker's (small dividend/interest moves are booked, audited).
    dedicated_account: bool = False
    reservation_gap: float = 0.03  # adverse price stress for entries, not a bound on real market gaps
    cash_tolerance: float = 1.0  # USD, plus 0.1% of equity
    max_external_cash: float = 0.02  # dedicated: largest unexplained cash move booked automatically (of equity)


@dataclass(frozen=True)
class ResearchSettings:
    provider: str
    model: str
    ollama_url: str
    call_timeout_s: int
    max_llm_calls_per_cycle: int
    max_fix_attempts: int
    null_samples: int
    min_sharpe_edge: float
    max_p_value: float
    min_dsr: float


@dataclass(frozen=True)
class TradingSettings:
    """How fast the agents trade ([trading] in lab.toml; lab/intraday.py)."""

    speed: str = "daily"  # daily | 1h | 15m | 5m; quick speeds need the dynamic or aggressive profile
    flat_at_close: bool = True  # quick speeds: exit everything before the close, hold nothing overnight
    data_delay_min: int = 15  # minutes the live data lags (IBKR paper without a subscription: 15; real-time: 0)
    limit_collar: float = 0.01  # quick speeds, live: marketable limit at last price +/- this fraction
    intraday_holdout: float = 0.2  # quick speeds: the last 20% of the stored sessions are the holdout
    # The operator's explicit intraday holdout start (ISO date), overriding the boundary persisted at first use.
    # Never later than the start of any intraday holdout already run (experiments.intraday_split).
    intraday_holdout_start: str | None = None


@dataclass(frozen=True)
class LimitSettings:
    """Absolute ceilings for every named risk profile ([limits] in lab.toml). Defaults: the broker's maximum
    (gross 1 / initial margin, i.e. 2x at Reg T, shorts up to that). The custom profile uses [risk] instead."""

    max_gross: float
    max_short_gross: float
    max_weight: float
    min_net: float
    max_net: float
    max_drawdown: float
    max_leverage: float


@dataclass(frozen=True)
class BorrowSettings:
    history: Path | None = None  # historical research only; never a paper-broker locate
    max_age_days: int = 3


@dataclass(frozen=True)
class LabConfig:
    root: Path
    data_dir: Path
    results_dir: Path
    research_dir: Path
    universe: dict[str, str]
    warmup_start: date
    periods: dict[str, Period]
    capital: float
    costs: CostSettings
    risk: RiskSettings
    margin: MarginSettings
    portfolio: PortfolioSettings
    paper: PaperSettings
    research: ResearchSettings
    raw: dict = field(repr=False, default_factory=dict)
    borrow: BorrowSettings = field(default_factory=BorrowSettings)
    limits: LimitSettings | None = None
    trading: TradingSettings = field(default_factory=TradingSettings)

    def period(self, name: str) -> Period:
        if name not in self.periods:
            raise ConfigError(f"unknown period {name!r}; choose from {sorted(self.periods)}")
        return self.periods[name]

    def snapshot(self) -> dict:
        """JSON-serializable copy of the settings, stored with every run."""
        d = asdict(self)
        d.pop("raw", None)
        return _jsonable(d)


def _jsonable(x):
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (Path, date)):
        return str(x)
    return x


def _date(value: str, what: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise ConfigError(f"{what}: invalid date {value!r} (use YYYY-MM-DD)") from e


def find_config(start: Path | None = None) -> Path:
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / "lab.toml").is_file():
            return d / "lab.toml"
    raise ConfigError("lab.toml not found in this directory or any parent")


def _trading(raw: dict, profile: str) -> TradingSettings:
    if not isinstance(raw, dict):
        raise ConfigError("[trading] must be a table")
    known = {"speed", "flat_at_close", "data_delay_min", "limit_collar", "intraday_holdout", "intraday_holdout_start"}
    if set(raw) - known:
        raise ConfigError(f"[trading]: unknown keys {sorted(set(raw) - known)}")
    t = TradingSettings(
        speed=str(raw.get("speed", "daily")),
        flat_at_close=bool(raw.get("flat_at_close", True)),
        data_delay_min=int(raw.get("data_delay_min", 15)),
        limit_collar=float(raw.get("limit_collar", 0.01)),
        intraday_holdout=float(raw.get("intraday_holdout", 0.2)),
        intraday_holdout_start=str(raw["intraday_holdout_start"]) if raw.get("intraday_holdout_start") else None,
    )
    if t.intraday_holdout_start is not None:
        try:
            date.fromisoformat(t.intraday_holdout_start)
        except ValueError as e:
            raise ConfigError(f"[trading] intraday_holdout_start must be an ISO date: {e}") from e
    from lab.intraday import check_speed_allowed

    try:
        check_speed_allowed(t.speed, profile)
    except ValueError as e:
        raise ConfigError(f"[trading] {e}") from e
    if not (0 <= t.data_delay_min <= 60 and 0 < t.limit_collar <= 0.1 and 0 < t.intraday_holdout < 1):
        raise ConfigError(
            "[trading]: need 0 <= data_delay_min <= 60, 0 < limit_collar <= 0.1, 0 < intraday_holdout < 1"
        )
    return t


def _limits(raw: dict, m: MarginSettings) -> LimitSettings:
    if not isinstance(raw, dict):
        raise ConfigError("[limits] must be a table")
    unknown = set(raw) - {
        "max_gross",
        "max_short_gross",
        "max_weight",
        "min_net",
        "max_net",
        "max_drawdown",
        "max_leverage",
    }
    if unknown:
        raise ConfigError(f"[limits]: unknown keys {sorted(unknown)}")
    broker = 1.0 / max(m.initial_long, m.initial_short)
    gross = float(raw.get("max_gross", broker))
    short = float(raw.get("max_short_gross", gross))
    lim = LimitSettings(
        max_gross=gross,
        max_short_gross=short,
        max_weight=float(raw.get("max_weight", 1.0)),
        min_net=float(raw.get("min_net", -short)),
        max_net=float(raw.get("max_net", gross)),
        max_drawdown=float(raw.get("max_drawdown", 0.99)),
        max_leverage=float(raw.get("max_leverage", gross)),
    )
    _check_risk(
        RiskSettings(lim.max_weight, lim.max_gross, lim.max_drawdown, lim.max_short_gross, lim.min_net, lim.max_net),
        m,
    )
    if not 0 < lim.max_leverage <= lim.max_gross:
        raise ConfigError("[limits]: need 0 < max_leverage <= max_gross")
    return lim


def _check_risk(r: RiskSettings, m: MarginSettings) -> None:
    import math

    vals = (r.max_weight, r.max_gross, r.max_drawdown, r.max_short_gross, r.min_net, r.max_net, *vars(m).values())
    if not all(math.isfinite(v) for v in vals):
        raise ConfigError("risk and margin settings must be finite numbers")
    if not (0 < r.max_weight <= 1 and r.max_gross > 0 and 0 < r.max_drawdown < 1):
        raise ConfigError("risk: need 0 < max_weight <= 1, max_gross > 0, 0 < max_drawdown < 1")
    if not 0 <= r.max_short_gross <= r.max_gross:
        raise ConfigError("risk: need 0 <= max_short_gross <= max_gross")
    if not (-r.max_short_gross <= r.min_net <= r.max_net <= r.max_gross):
        raise ConfigError("risk: need -max_short_gross <= min_net <= max_net <= max_gross")
    if r.max_short_gross == 0 and r.min_net < 0:
        raise ConfigError("risk: a negative min_net needs max_short_gross > 0")
    for k in ("initial_long", "initial_short", "maintenance_long", "maintenance_short"):
        if not 0 < getattr(m, k) <= 1:
            raise ConfigError(f"margin.{k} must be in (0, 1]")
    if m.maintenance_long > m.initial_long or m.maintenance_short > m.initial_short:
        raise ConfigError("margin: maintenance requirements cannot exceed initial requirements")
    if not (0 <= m.cushion < 1 and m.borrow_apr >= 0 and m.debit_apr >= 0):
        raise ConfigError("margin: need 0 <= cushion < 1 and non-negative financing rates")
    # The broker's initial requirement caps gross exposure: at Reg T's 50% that is 2x. No setting may promise more.
    worst = max(m.initial_long, m.initial_short) * r.max_gross
    if worst > 1 + 1e-9:
        raise ConfigError(
            f"risk.max_gross {r.max_gross} needs {worst:.0%} of equity as initial margin at the configured "
            f"requirements (max {1 / max(m.initial_long, m.initial_short):.2f}x); the broker would refuse it"
        )


def load_config(path: str | Path | None = None, *, environ=None) -> LabConfig:
    try:
        return _load_config(path, environ=environ)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc


def _load_config(path: str | Path | None = None, *, environ=None) -> LabConfig:
    env = os.environ if environ is None else environ
    path = Path(path) if path else find_config()
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    _validate_raw_types(raw)
    root = path.parent.resolve()
    br = raw.get("borrow", {})
    if not isinstance(br, dict) or set(br) - {"history", "max_age_days"}:
        raise ConfigError("borrow: expected history and max_age_days only")
    history, age = br.get("history", ""), br.get("max_age_days", 3)
    if not isinstance(history, str) or type(age) is not int or age < 0:
        raise ConfigError("borrow: history must be a path string and max_age_days a nonnegative integer")
    borrow = BorrowSettings(root / history if history else None, age)

    try:
        paths = raw["paths"]
        universe = {str(k).upper(): str(v).upper() for k, v in raw["universe"].items()}
        per = raw["periods"]
        bt = raw["backtest"]
        rk = raw["risk"]
        pp = raw["paper"]
        rs = raw["research"]
    except KeyError as e:
        raise ConfigError(f"{path}: missing section {e}") from e

    for t, v in universe.items():
        if v not in VALID_VENUES:
            raise ConfigError(f"universe: {t} has unknown venue {v!r}; use one of {sorted(VALID_VENUES)}")
    if not universe:
        raise ConfigError("universe is empty")

    periods = {}
    for name in ("train", "test", "holdout"):
        start, end = per[name]
        periods[name] = Period(name, _date(start, name), _date(end, name) if end else None)
    names = ["train", "test", "holdout"]
    for a, b in itertools.pairwise(names):
        if periods[a].end is None or periods[a].end >= periods[b].start:
            raise ConfigError(f"periods overlap or are out of order: {a} must end before {b} starts")
    warmup = _date(per["warmup_start"], "warmup_start")
    if warmup >= periods["train"].start:
        raise ConfigError("warmup_start must be before the train period")

    risk = RiskSettings(
        float(rk["max_weight"]),
        float(rk["max_gross"]),
        float(rk["max_drawdown"]),
        max_short_gross=float(rk.get("max_short_gross", 0.0)),
        min_net=float(rk.get("min_net", 0.0)),
        max_net=float(rk.get("max_net", 1.0)),
        profile=str(rk.get("profile", "custom")),
    )
    mg = raw.get("margin", {})
    try:
        margin = MarginSettings(**{k: float(v) for k, v in mg.items()})
    except (TypeError, ValueError) as e:
        raise ConfigError(f"[margin]: {e}") from e
    _check_risk(risk, margin)
    pf = raw.get("portfolio", {})
    portfolio = PortfolioSettings(
        float(pf.get("target_vol", 0.0)),
        int(pf.get("vol_window", 63)),
        max_leverage=float(pf.get("max_leverage", 1.0)),
        leverage=float(pf.get("leverage", 1.0)),
    )
    if not (portfolio.target_vol >= 0 and portfolio.vol_window >= 2):
        raise ConfigError("portfolio: target_vol must be >= 0 and vol_window >= 2")
    if not (0 < portfolio.leverage <= portfolio.max_leverage):
        raise ConfigError("portfolio: need 0 < leverage <= max_leverage")
    if portfolio.max_leverage > risk.max_gross:
        raise ConfigError(
            f"portfolio.max_leverage {portfolio.max_leverage} exceeds risk.max_gross {risk.max_gross}: "
            "raise the gross limit deliberately or lower the leverage"
        )
    # Apply operator-selected risk units once, before constructing the node,
    # so backtests and paper trading use the same effective limits.
    from lab.risk_profiles import apply_profile

    limits = _limits(raw.get("limits", {}), margin)
    if risk.profile != "custom":
        # A named profile is intersected with the absolute ceilings in [limits] (default: the broker's maximum),
        # not with [risk], which holds the custom profile's own limits (Gabriel: the profile decides, C003).
        ceiling = RiskSettings(
            limits.max_weight,
            limits.max_gross,
            limits.max_drawdown,
            limits.max_short_gross,
            limits.min_net,
            limits.max_net,
            profile=risk.profile,
        )
        risk, portfolio = apply_profile(
            ceiling, replace(portfolio, max_leverage=limits.max_leverage, leverage=1.0), risk.profile
        )
    _check_risk(risk, margin)
    trading = _trading(raw.get("trading", {}), risk.profile)

    research = ResearchSettings(
        provider=str(rs["provider"]),
        model=str(rs["model"]),
        ollama_url=str(rs["ollama_url"]),
        call_timeout_s=int(rs["call_timeout_s"]),
        max_llm_calls_per_cycle=int(rs["max_llm_calls_per_cycle"]),
        max_fix_attempts=int(rs["max_fix_attempts"]),
        null_samples=int(rs["null_samples"]),
        min_sharpe_edge=float(rs["min_sharpe_edge"]),
        max_p_value=float(rs["max_p_value"]),
        min_dsr=float(rs.get("min_dsr", 0.95)),
    )
    if research.provider not in ("claude_code", "ollama"):
        raise ConfigError("research.provider must be 'claude_code' or 'ollama'")

    cfg = LabConfig(
        root=root,
        data_dir=root / paths["data"],
        results_dir=root / paths["results"],
        research_dir=root / paths["research"],
        universe=universe,
        warmup_start=warmup,
        periods=periods,
        capital=float(bt["capital"]),
        costs=CostSettings(
            commission_per_share=float(bt["commission_per_share"]),
            commission_min=float(bt["commission_min"]),
            commission_max_pct=float(bt["commission_max_pct"]),
            slippage=bool(bt["slippage"]),
            open_print_fraction=float(bt["open_print_fraction"]),
            min_trade_value=float(bt["min_trade_value"]),
            rebalance_band=float(bt["rebalance_band"]),
            cash_buffer=float(bt["cash_buffer"]),
        ),
        risk=risk,
        margin=margin,
        portfolio=portfolio,
        paper=PaperSettings(
            host=env.get("LAB_IB_HOST", str(pp["host"])),
            port=int(env.get("LAB_IB_PORT", pp["port"])),
            client_id=int(env.get("LAB_IB_CLIENT_ID", pp["client_id"])),
            account_id=env.get("LAB_PAPER_ACCOUNT", str(pp["account_id"])),
            market_data=str(pp["market_data"]).upper(),
            capital=float(pp["capital"]),
            rebalance_time=str(pp["rebalance_time"]),
            signal_time=str(pp["signal_time"]),
            max_order_value=float(pp["max_order_value"]),
            max_orders_per_day=int(pp["max_orders_per_day"]),
            max_daily_turnover=float(pp["max_daily_turnover"]),
            order_rate=str(pp["order_rate"]),
            dedicated_account=bool(pp.get("dedicated_account", False)),
            cash_tolerance=float(pp.get("cash_tolerance", 1.0)),
            reservation_gap=float(pp.get("reservation_gap", 0.03)),
            max_external_cash=float(pp.get("max_external_cash", 0.02)),
        ),
        research=research,
        raw=raw,
        borrow=borrow,
        limits=limits,
        trading=trading,
    )

    validate_effective(cfg)
    return cfg


def _check_settings(cfg: LabConfig) -> None:
    """Reject monetary and gate settings that can disable controls through NaN or signs."""

    def bounded(name, value, low, high=math.inf, *, strict_low=False, strict_high=False):
        if (
            not math.isfinite(value)
            or value < low
            or value > high
            or (strict_low and value == low)
            or (strict_high and value == high)
        ):
            raise ConfigError(f"{name}: invalid value {value!r}")

    bounded("backtest.capital", cfg.capital, 0, strict_low=True)
    bounded("paper.capital", cfg.paper.capital, 0, strict_low=True)
    for key in ("commission_per_share", "commission_min", "min_trade_value"):
        bounded(f"backtest.{key}", getattr(cfg.costs, key), 0)
    for key in ("commission_max_pct", "rebalance_band", "cash_buffer"):
        bounded(f"backtest.{key}", getattr(cfg.costs, key), 0, 1, strict_high=True)
    bounded("backtest.open_print_fraction", cfg.costs.open_print_fraction, 0, 1, strict_low=True)
    bounded("portfolio.target_vol", cfg.portfolio.target_vol, 0)
    for key in ("max_order_value", "max_orders_per_day", "max_daily_turnover", "cash_tolerance"):
        bounded(f"paper.{key}", getattr(cfg.paper, key), 0)
    bounded("paper.reservation_gap", cfg.paper.reservation_gap, 0, 0.5)
    bounded("paper.max_external_cash", cfg.paper.max_external_cash, 0, 1)
    for key in ("call_timeout_s", "max_llm_calls_per_cycle", "null_samples"):
        bounded(f"research.{key}", getattr(cfg.research, key), 0, strict_low=True)
    bounded("research.max_fix_attempts", cfg.research.max_fix_attempts, 0)
    for key in ("max_p_value", "min_dsr"):
        bounded(f"research.{key}", getattr(cfg.research, key), 0, 1)
    if not math.isfinite(cfg.research.min_sharpe_edge):
        raise ConfigError("research.min_sharpe_edge must be finite")
    for p in cfg.periods.values():
        if p.end is not None and p.end < p.start:
            raise ConfigError(f"{p.name}: end precedes start")


def validate_effective(cfg: LabConfig) -> None:
    _check_settings(cfg)
    _check_risk(cfg.risk, cfg.margin)
    p = cfg.portfolio
    if (
        not all(math.isfinite(v) for v in (p.target_vol, p.leverage, p.max_leverage))
        or not 0 < p.leverage <= p.max_leverage <= cfg.risk.max_gross
    ):
        raise ConfigError("invalid effective portfolio leverage")
    if type(p.vol_window) is not int or p.vol_window < 2:
        raise ConfigError("vol_window must be an integer >= 2")
    _trading(vars(cfg.trading), cfg.risk.profile)


def _validate_raw_types(raw):
    from dataclasses import fields

    schemas = {
        "risk": RiskSettings,
        "margin": MarginSettings,
        "portfolio": PortfolioSettings,
        "paper": PaperSettings,
        "research": ResearchSettings,
        "trading": TradingSettings,
        "limits": LimitSettings,
        "backtest": CostSettings,
    }
    allowed = set(schemas) | {"paths", "universe", "periods", "borrow"}
    if set(raw) - allowed:
        raise ConfigError(f"unknown sections: {sorted(set(raw) - allowed)}")
    for section, values in raw.items():
        if not isinstance(values, dict):
            raise ConfigError(f"[{section}] must be a table")
        if section not in schemas:
            continue
        types = {f.name: f.type for f in fields(schemas[section])}
        if section == "backtest":
            types["capital"] = "float"
        for key, value in values.items():
            if key not in types:
                raise ConfigError(f"unknown setting {section}.{key}")
            kind = types[key]
            valid = (
                type(value) is bool
                if kind == "bool"
                else type(value) is int
                if kind == "int"
                else type(value) in (int, float) and math.isfinite(value)
                if kind == "float"
                else isinstance(value, str)
                if kind in ("str", "str | None")
                else True
            )
            if not valid:
                raise ConfigError(f"{section}.{key} must have type {kind}")
