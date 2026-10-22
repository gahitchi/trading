"""Load and validate lab.toml into typed settings."""

from __future__ import annotations

import itertools
import os
import tomllib
from dataclasses import asdict, dataclass, field
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
    max_weight: float
    max_gross: float
    max_drawdown: float
    max_short_gross: float = 0.0
    min_net: float = 0.0
    max_net: float = 1.0
    profile: str = "balanced"


@dataclass(frozen=True)
class PortfolioSettings:
    target_vol: float
    vol_window: int


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
    portfolio: PortfolioSettings
    paper: PaperSettings
    research: ResearchSettings
    raw: dict = field(repr=False, default_factory=dict)

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


def load_config(path: str | Path | None = None) -> LabConfig:
    path = Path(path) if path else find_config()
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    root = path.parent.resolve()

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
        max_weight=float(rk["max_weight"]),
        max_gross=float(rk["max_gross"]),
        max_drawdown=float(rk["max_drawdown"]),
        max_short_gross=float(rk.get("max_short_gross", 0.0)),
        min_net=float(rk.get("min_net", 0.0)),
        max_net=float(rk.get("max_net", 1.0)),
        profile=str(rk.get("profile", "balanced")),
    )
    from math import isfinite

    vals = (
        risk.max_weight,
        risk.max_gross,
        risk.max_drawdown,
        risk.max_short_gross,
        risk.min_net,
        risk.max_net,
    )
    if not all(isfinite(x) for x in vals) or not (
        0 < risk.max_weight <= 1
        and risk.max_gross > 0
        and 0 < risk.max_drawdown < 1
        and 0 <= risk.max_short_gross <= risk.max_gross
        and -risk.max_gross <= risk.min_net <= risk.max_net <= risk.max_gross
    ):
        raise ConfigError("risk limits must be finite, with signed net and short budgets inside gross")
    if risk.profile not in {"calm", "cautious", "balanced", "dynamic", "aggressive"}:
        raise ConfigError("risk.profile must be calm, cautious, balanced, dynamic or aggressive")

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

    return LabConfig(
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
        portfolio=PortfolioSettings(
            float(raw.get("portfolio", {}).get("target_vol", 0.0)), int(raw.get("portfolio", {}).get("vol_window", 63))
        ),
        paper=PaperSettings(
            host=os.environ.get("LAB_IB_HOST", str(pp["host"])),
            port=int(os.environ.get("LAB_IB_PORT", pp["port"])),
            client_id=int(os.environ.get("LAB_IB_CLIENT_ID", pp["client_id"])),
            account_id=os.environ.get("LAB_PAPER_ACCOUNT", str(pp["account_id"])),
            market_data=str(pp["market_data"]).upper(),
            capital=float(pp["capital"]),
            rebalance_time=str(pp["rebalance_time"]),
            signal_time=str(pp["signal_time"]),
            max_order_value=float(pp["max_order_value"]),
            max_orders_per_day=int(pp["max_orders_per_day"]),
            max_daily_turnover=float(pp["max_daily_turnover"]),
            order_rate=str(pp["order_rate"]),
        ),
        research=research,
        raw=raw,
    )
