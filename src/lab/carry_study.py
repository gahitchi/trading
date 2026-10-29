"""
Is there a funding-carry edge after costs? A rules-based, delta-neutral carry backtest on hourly history
(lab.carry_data), with a time holdout, the trial log, and a Sharpe ratio deflated for every configuration tried.

Two trades, each a pair of legs of equal notional:

- `cash`: long Binance spot, short the Binance USD-M perpetual. Earns the perpetual's funding while it is positive
  (shorts are paid), plus the change in basis (spot return minus perpetual return). Long spot only: shorting spot
  needs a margin loan whose cost isn't modelled, so negative funding is never traded.
- `cross`: short the perpetual on the venue whose funding is higher, long it on the other (Binance vs Bybit).
  Earns the funding difference plus the change in the cross-venue price gap.

Signal, decided at each rebalance hour T (every `rebalance_h` hours from 00:00 UTC, default the 8-hour funding
times) from data up to T only: the funding the trade earned over the last `lookback_h` hours, annualized. A
position opens when it exceeds `entry_apr` and closes when it falls below `exit_apr` (hysteresis keeps turnover
down). The new position trades at the close of the hour ending at T, after T's funding has settled, so it first
earns the next settlement. Each change of position pays both legs' taker fees and slippage on the notional changed.

With `top` K (cash only), the symbols compete for K equal slots instead: at each rebalance hour, positions whose
signal fell below `exit_apr` close, and free slots go to the highest signals above `entry_apr` among symbols whose
perpetual traded at least `min_turnover_m` M USDT a day over the last 30 days (also known at T). This is the test
on a whole universe, where the highest funding is usually on small altcoins.

Per symbol and hour T the trade earns w(T-1h) x (long leg return - short leg return + funding difference at T),
minus |w(T) - w(T-1h)| x cost. Returns are per unit of notional per leg; the portfolio gives every symbol an
equal share of notional whether or not its trade is open. The cash trade also ties up the perpetual's margin
beside the spot notional: divide by (1 + 1/leverage) for a return on capital. With `top`, each slot has 1/K of the
notional and an empty slot earns nothing. Not modelled: rebalancing the legs
as prices drift, liquidation of the short perpetual on a rally (the spot leg covers it only if collateral can be
moved in time), borrow costs, funding paid on mark rather than last price, and fees that depend on volume tier.

The holdout is a time boundary shared by every carry study: the first run reserves the last HOLDOUT_DAYS of the
history fetched by then plus everything fetched later. Research runs drop all data from the boundary on before
anything is computed; `--holdout` evaluates a finished configuration once on the data after it (its signal still
warms up on the data before). The boundary only moves later (`lab carry release`), never earlier.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from lab import carry_data
from lab.fast_research import HOLDOUT_LOCK, HoldoutGuard
from lab.persistence import atomic_json, file_lock, read_json

MODES = ("cash", "cross")
HOURS_PER_YEAR = 24 * 365
HOLDOUT_DAYS = 180
PARTITION = "carry_partition.json"
FILL_GAP_H = 3  # prices carry forward this far; a held position beyond it invalidates the study
EULER_GAMMA = 0.5772156649015329
# Liquid USDT pairs with a Binance spot market and a Bybit perpetual. Chosen today: they survived, which flatters
# the past. Pass --symbols for others (the recorded ones without spot only work in `cross`).
DEFAULT_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT",
)  # fmt: skip


@dataclass(frozen=True)
class CarrySettings:
    mode: str = "cash"
    lookback_h: int = 72
    entry_apr: float = 10.0  # % a year
    exit_apr: float = 0.0
    rebalance_h: int = 8
    perp_fee_bps: float = 5.0  # taker, per side
    spot_fee_bps: float = 10.0  # Binance spot taker base tier
    slippage_bps: float = 1.0  # per leg and side
    top: int = 0  # 0: every symbol trades on its own; K: the K highest signals share the notional
    min_turnover_m: float = 0.0  # M USDT a day on the perpetual, 30-day mean, to open a position

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"unknown mode: {self.mode!r} (expected one of {', '.join(MODES)})")
        if self.lookback_h < 1 or self.rebalance_h < 1 or 24 % self.rebalance_h:
            raise ValueError("lookback_h must be >= 1 and rebalance_h must divide 24")
        if self.exit_apr > self.entry_apr:
            raise ValueError("exit_apr must not exceed entry_apr")
        if self.top < 0 or (self.top and self.mode != "cash"):
            raise ValueError("top applies to the cash trade only, and must be positive")

    @property
    def cost(self) -> float:
        """Cost of changing one unit of position (both legs, one side), as a fraction of notional."""
        other = self.spot_fee_bps if self.mode == "cash" else self.perp_fee_bps
        return (self.perp_fee_bps + other + 2 * self.slippage_bps) / 10_000


@dataclass(frozen=True)
class Legs:
    """One symbol's trade: hourly closes of the leg held long and the leg held short at w = +1, and the funding
    the trade receives per settlement at w = +1 (short leg's funding minus long leg's)."""

    long: pd.Series
    short: pd.Series
    funding: pd.Series
    turnover: pd.Series | None = None  # the Binance perpetual's traded value per hour, USDT


def legs(root: Path, mode: str, symbol: str) -> Legs | None:
    """The cached series of a symbol's trade, or None if any is missing."""
    perp = carry_data.load(root, "binance", "perp", "close", symbol)
    turnover = carry_data.load(root, "binance", "perp", "turnover", symbol)
    f_bin = carry_data.load(root, "binance", "perp", "funding", symbol)
    if mode == "cash":
        spot = carry_data.load(root, "binance", "spot", "close", symbol)
        out = Legs(long=spot, short=perp, funding=f_bin, turnover=turnover)
    else:
        byb = carry_data.load(root, "bybit", "perp", "close", symbol)
        f_byb = carry_data.load(root, "bybit", "perp", "funding", symbol)
        if f_byb.empty or f_bin.empty:
            return None
        both = max(f_bin.index[0], f_byb.index[0])  # before both venues pay funding there is no difference
        diff = f_bin.sub(f_byb, fill_value=0.0)
        out = Legs(long=byb, short=perp, funding=diff[diff.index >= both], turnover=turnover)
    return None if out.long.empty or out.short.empty or f_bin.empty else out


def truncate(lg: Legs, end: pd.Timestamp) -> Legs:
    """Only what was known before `end`."""
    return Legs(*(None if s is None else s[s.index < end] for s in (lg.long, lg.short, lg.funding, lg.turnover)))


def hourly(lg: Legs) -> pd.DataFrame | None:
    """On the hourly grid where both legs trade: each leg's return over the hour ending at T, the funding settled
    in it, whether both legs printed a close at T (a trade may only change there), and the perpetual's mean daily
    turnover over the 30 days up to T (NaN until 30 days of it are known)."""
    start = max(lg.long.index[0], lg.short.index[0]).ceil("h")
    end = min(lg.long.index[-1], lg.short.index[-1]).floor("h")
    if end <= start:
        return None
    grid = pd.date_range(start, end, freq="h")
    closes = [s.groupby(s.index.floor("h")).last().reindex(grid) for s in (lg.long, lg.short)]
    rets = [c.ffill(limit=FILL_GAP_H).pct_change(fill_method=None) for c in closes]
    funding = lg.funding.groupby(lg.funding.index.floor("h")).sum().reindex(grid, fill_value=0.0)
    if lg.turnover is not None and len(lg.turnover):
        hourly_value = lg.turnover.groupby(lg.turnover.index.floor("h")).sum().reindex(grid, fill_value=0.0)
        turnover = hourly_value.rolling(30 * 24, min_periods=30 * 24).sum() / 30
    else:
        turnover = pd.Series(np.nan, index=grid)
    return pd.DataFrame(
        {
            "r_long": rets[0],
            "r_short": rets[1],
            "funding": funding,
            "tradable": closes[0].notna() & closes[1].notna() & rets[0].notna() & rets[1].notna(),
            "turnover_d": turnover,
        }
    )


def signal(h: pd.DataFrame, s: CarrySettings) -> np.ndarray:
    """The funding the trade earned over the last lookback_h hours up to and including T, % a year."""
    x = h["funding"].rolling(s.lookback_h, min_periods=s.lookback_h).sum() * (HOURS_PER_YEAR / s.lookback_h)
    return (x * 100).to_numpy()


def liquid(h: pd.DataFrame, s: CarrySettings) -> np.ndarray:
    if s.min_turnover_m <= 0:
        return np.ones(len(h), dtype=bool)
    return (h["turnover_d"] >= s.min_turnover_m * 1e6).to_numpy()  # NaN (not yet 30 days) is not liquid


def positions(h: pd.DataFrame, s: CarrySettings) -> pd.Series:
    """w(T), held from T to T + 1h: 0 or 1 (cash), -1, 0 or 1 (cross). Uses data up to and including T."""
    signal_ = signal(h, s)
    can_open = liquid(h, s)
    decide = (h.index.hour % s.rebalance_h == 0) & h["tradable"].to_numpy()
    sides = (1,) if s.mode == "cash" else (1, -1)
    w = np.zeros(len(h))
    cur = 0.0
    for i in range(len(h)):
        if decide[i] and not math.isnan(signal_[i]):
            x = signal_[i]
            if cur and cur * x < s.exit_apr:
                cur = 0.0
            if not cur and can_open[i]:
                cur = next((float(d) for d in sides if d * x > s.entry_apr), 0.0)
        w[i] = cur
    return pd.Series(w, index=h.index, name="w")


def top_positions(hs: dict[str, pd.DataFrame], s: CarrySettings) -> dict[str, pd.Series]:
    """w(T) per symbol (0 or 1) when the symbols compete for s.top slots; uses data up to and including T only."""
    syms = list(hs)
    grid = hs[syms[0]].index
    for h in hs.values():
        grid = grid.union(h.index)
    on_grid = [h.reindex(grid) for h in hs.values()]
    x = np.column_stack([signal(h, s) for h in on_grid])
    tradable = np.column_stack([h["tradable"].fillna(False).to_numpy(dtype=bool) for h in on_grid])
    ok = np.column_stack(
        [
            h["tradable"].fillna(False).to_numpy(dtype=bool) & liquid(h, s) & ~np.isnan(sig)
            for h, sig in zip(on_grid, x.T, strict=True)
        ]
    )
    decide = grid.hour % s.rebalance_h == 0
    ends = np.array([h.index[-1] for h in hs.values()])
    w = np.zeros((len(grid), len(syms)))
    held = np.zeros(len(syms), dtype=bool)
    for i in range(len(grid)):
        ended = np.flatnonzero(held & (ends < grid[i]))
        if len(ended):
            raise ValueError(f"price history ended for held carry position {syms[ended[0]]} at {grid[i]}")
        if decide[i]:
            row = x[i]
            held &= ~(tradable[i] & (row < s.exit_apr))  # exits need prices, but not the entry liquidity filter
            free = s.top - int(held.sum())
            if free > 0:
                cand = np.flatnonzero(ok[i] & ~held & (row > s.entry_apr))
                held[cand[np.argsort(-row[cand], kind="stable")][:free]] = True
        w[i] = held
    return {sym: pd.Series(w[:, j], index=grid, name="w").reindex(hs[sym].index) for j, sym in enumerate(syms)}


def backtest(
    h: pd.DataFrame, s: CarrySettings, eval_from: pd.Timestamp | None = None, w: pd.Series | None = None
) -> pd.DataFrame:
    """Hourly P&L as a fraction of notional: basis, funding, cost, net, and the position w(T) (computed here
    unless given). With eval_from, only hours from it on, as if the trade started flat just before it."""
    w = positions(h, s) if w is None else w
    prev = w.shift(1, fill_value=0.0)
    if eval_from is not None:
        w = w[w.index >= eval_from]
        prev = prev.reindex(w.index).copy()
        h = h.reindex(w.index)
        if len(prev):
            prev.iloc[0] = 0.0  # evaluation starts flat; no earlier exposure or settlement is earned
    invalid = (prev != 0) & ~np.isfinite(h[["r_long", "r_short"]]).all(axis=1)
    if invalid.any():
        first = invalid.index[invalid][0]
        raise ValueError(
            f"cannot value held carry position at {first}: leg prices exceed the {FILL_GAP_H}-hour carry-forward limit"
        )
    out = pd.DataFrame(
        {
            "w": w,
            "basis": (prev * (h["r_long"] - h["r_short"])).where(prev != 0, 0.0),
            "funding": prev * h["funding"],
            "cost": (w - prev).abs() * s.cost,
        }
    )
    out["net"] = out["basis"] + out["funding"] - out["cost"]
    return out


def newey_west_t(x: np.ndarray, lags: int | None = None) -> float:
    """t-statistic of the mean with Newey-West (Bartlett) standard errors: carry positions last days, so
    daily returns are autocorrelated and a plain t-statistic overstates."""
    n = len(x)
    if n < 3:
        return math.nan
    lags = int(4 * (n / 100) ** (2 / 9)) if lags is None else lags
    e = x - x.mean()
    var = e @ e / n
    for k in range(1, min(lags, n - 1) + 1):
        var += 2 * (1 - k / (lags + 1)) * (e[k:] @ e[:-k]) / n
    return float(x.mean() / math.sqrt(var / n)) if var > 0 else math.nan


def probabilistic_sharpe(sr: float, n: int, skew: float, kurt: float, sr0: float = 0.0) -> float:
    """P(true Sharpe > sr0) given an observed per-period Sharpe over n periods (Bailey & Lopez de Prado);
    kurt is the raw (not excess) kurtosis."""
    if n < 3 or not math.isfinite(sr):
        return math.nan
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if denom <= 0:
        return math.nan
    return NormalDist().cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom))


def expected_max_sharpe(trial_srs: list[float]) -> float:
    """The best per-period Sharpe expected from len(trial_srs) tries with no edge at all (deflated Sharpe's
    benchmark): it grows with the number of trials and how much their Sharpes vary."""
    n = len(trial_srs)
    if n < 2:
        return 0.0
    z = NormalDist().inv_cdf
    sd = float(np.std(trial_srs, ddof=1))
    return sd * ((1 - EULER_GAMMA) * z(1 - 1 / n) + EULER_GAMMA * z(1 - 1 / (n * math.e)))


def summary(pnl: dict[str, pd.DataFrame], slots: int = 0) -> dict:
    """Portfolio statistics on daily net returns. Each symbol with data has an equal share of the notional (a
    symbol requested without data is left out, not counted as idle notional), or with `slots`, each slot has."""
    if not pnl:
        return {"days": 0}
    n_share = slots or len(pnl)
    net = pd.concat({k: v["net"] for k, v in pnl.items()}, axis=1).fillna(0.0).sum(axis=1) / n_share
    daily = net.resample("D").sum()
    x = daily.to_numpy()
    n = len(x)
    sd = float(x.std(ddof=1)) if n > 1 else math.nan
    sr = float(x.mean() / sd) if sd and sd > 0 else math.nan
    eq = np.cumsum(x)
    dd = float((np.maximum.accumulate(np.maximum(eq, 0)) - eq).max()) if n else 0.0
    part = {k: sum(v[k].sum() for v in pnl.values()) / n_share for k in ("funding", "basis", "cost")}
    years = n / 365 if n else math.nan
    skew = float(pd.Series(x).skew()) if n > 2 else 0.0
    kurt = float(pd.Series(x).kurt()) + 3 if n > 3 else 3.0
    return {
        "start": str(daily.index[0].date()),
        "end": str(daily.index[-1].date()),
        "days": n,
        "apr": float(x.sum() / years * 100),
        "vol": float(sd * math.sqrt(365) * 100) if n > 1 else math.nan,
        "sharpe": float(sr * math.sqrt(365)) if math.isfinite(sr) else math.nan,
        "sr_daily": sr,
        "t_nw": newey_west_t(x),
        "max_dd": dd * 100,
        "skew": skew,
        "kurt": kurt,
        "funding_apr": part["funding"] / years * 100,
        "basis_apr": part["basis"] / years * 100,
        "cost_apr": part["cost"] / years * 100,
        "in_market": float(
            (pd.concat({k: v["w"] != 0 for k, v in pnl.items()}, axis=1).fillna(False).sum(axis=1) / n_share).mean()
        )
        * 100,
    }


def per_symbol(pnl: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for sym, v in pnl.items():
        years = len(v) / HOURS_PER_YEAR
        opened = int(((v["w"] != 0) & (v["w"].shift(1, fill_value=0.0) != v["w"])).sum())
        rows.append(
            {
                "symbol": sym,
                "from": str(v.index[0].date()),
                "in_market_%": (v["w"] != 0).mean() * 100,
                "trades": opened,
                "funding_bps": v["funding"].sum() * 1e4,
                "basis_bps": v["basis"].sum() * 1e4,
                "cost_bps": v["cost"].sum() * 1e4,
                "net_bps": v["net"].sum() * 1e4,
                "net_apr_%": v["net"].sum() / years * 100 if years else math.nan,
            }
        )
    return pd.DataFrame(rows)


def run(
    root: Path,
    symbols: list[str],
    s: CarrySettings,
    boundary: pd.Timestamp,
    holdout: bool = False,
    end: pd.Timestamp | None = None,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Hourly P&L per symbol on research data (before the boundary) or, with holdout, on the data from it on.
    Also returns the symbols left out for lack of data."""
    hs, missing = {}, []
    for sym in symbols:
        lg = legs(root, s.mode, sym)
        if lg is not None and end is not None:
            lg = truncate(lg, end + pd.Timedelta(nanoseconds=1))
        if lg is not None and not holdout:
            lg = truncate(lg, boundary)
        h = hourly(lg) if lg is not None and len(lg.long) and len(lg.short) else None
        if h is None:
            missing.append(sym)
        else:
            hs[sym] = h
    ws = top_positions(hs, s) if s.top and hs else {}
    pnl = {}
    for sym, h in hs.items():
        try:
            r = backtest(h, s, boundary if holdout else None, ws.get(sym))
        except ValueError as e:
            raise ValueError(f"{sym}: {e}") from e
        if r.empty:
            missing.append(sym)
        else:
            pnl[sym] = r
    return pnl, missing


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _last_cached(root: Path) -> pd.Timestamp | None:
    last = None
    for p in Path(root).glob("*.parquet"):
        ts = pd.to_datetime(pd.read_parquet(p, columns=["ts"])["ts"], utc=True)
        if len(ts):
            last = ts.max() if last is None else max(last, ts.max())
    return last


def boundary(results_dir: Path, root: Path) -> pd.Timestamp:
    """The carry holdout start. Reserved on first use: the last HOLDOUT_DAYS of what is cached, and later data."""
    path = Path(results_dir) / PARTITION
    with file_lock(path.with_name(path.name + ".lock")):
        if path.exists():
            try:
                return pd.Timestamp(read_json(path)["holdout_start"])
            except (OSError, ValueError, KeyError) as e:
                raise HoldoutGuard(f"unreadable {path}; restore it, the holdout depends on it: {e}") from e
        last = _last_cached(root)
        if last is None:
            raise ValueError(f"no carry data in {root}; run `lab carry fetch` first")
        start = (last - pd.Timedelta(days=HOLDOUT_DAYS)).floor("D")
        atomic_json(
            path,
            {
                "version": 1,
                "holdout_start": start.isoformat(),
                "history": [{"at": _now(), "holdout_start": start.isoformat(), "reason": "first carry study"}],
            },
        )
        return start


def release(results_dir: Path, root: Path, reason: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Moves the boundary to just after what is cached now: the holdout becomes research data, and data fetched
    from here on is the new holdout. Returns (old, new)."""
    if not reason.strip():
        raise ValueError("a reason is required: it is recorded with the new boundary")
    with file_lock(Path(results_dir) / HOLDOUT_LOCK):
        old = boundary(results_dir, root)
        last = _last_cached(root)
        new = (last + pd.Timedelta(hours=1)).floor("h") if last is not None else old
        if new <= old:
            raise HoldoutGuard(f"nothing has been fetched since the holdout began ({old})")
        path = Path(results_dir) / PARTITION
        with file_lock(path.with_name(path.name + ".lock")):
            part = read_json(path)
            if pd.Timestamp(part["holdout_start"]) != old:
                raise HoldoutGuard("the boundary changed while releasing; run it again")
            part["holdout_start"] = new.isoformat()
            part["history"].append({"at": _now(), "holdout_start": new.isoformat(), "reason": reason})
            atomic_json(path, part)
        return old, new


def trial_sharpes(results_dir: Path, kind: str, instrument: str) -> list[float]:
    """The latest daily Sharpe of every distinct configuration tried on the research data."""
    path = Path(results_dir) / "fast_trials.jsonl"
    if not path.exists():
        return []
    latest = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r["kind"] == kind and r["instrument"] == instrument and not r["holdout"]:
            sr = r["result"].get("sr_daily")
            if sr is not None and math.isfinite(sr):
                latest[r["fingerprint"]] = sr
    return list(latest.values())


def params_of(s: CarrySettings, symbols: list[str]) -> dict:
    p = {**asdict(s), "symbols": sorted(symbols)}
    for k in ("top", "min_turnover_m"):  # left out at their defaults: earlier trials keep their fingerprints
        if p[k] == CarrySettings.__dataclass_fields__[k].default:
            del p[k]
    return p
