"""
Does trading crashes pay after costs? An event backtest on 1-minute history (lab.crash_data): find fast sell-offs,
then trade them one of two ways, with a time holdout, the trial log and a deflated Sharpe ratio.

Where each trade comes from:
  - `wick` is liquidity provision to forced sellers. Those who buy from investors who must sell earn a premium
    for it (Grossman & Miller 1988; Coval & Stafford 2007, fire sales), and the reversal premium is largest
    exactly when markets are stressed (Nagel 2012, "Evaporating Liquidity"). In crypto perpetuals the forced
    sellers are liquidated longs: open interest falling during the drop is their footprint.
  - `breakdown` is the crisis alpha of trend following: time-series momentum has earned its best returns in the
    worst equity markets for a century (Hurst, Ooi & Pedersen 2017), because losses force deleveraging that
    feeds on itself (Brunnermeier & Pedersen 2009, liquidity spirals). Rising open interest in the drop means
    the positions that will be forced out later are still being built.

A crash at minute T (the end of a 1-minute bar), from data up to T only:
  - the price fell at least `drop_pct` % over the last `window_min` minutes, and
  - the quote volume of those minutes was at least `vol_mult` times its usual level (the median minute of the
    7 days before, times window_min), and
  - open interest moved over the same window by the mode's rule. Open interest is a 5-minute snapshot, used only
    from 5 minutes after its time (the archive's stamp is when it was taken, not when it was public).

Two trades on the same detector:
  - `wick` (long): forced selling overshoots. Open interest has fallen at least `oi_pct` % (positions were
    liquidated or cut, not opened), so buy the flush and sell the bounce.
  - `breakdown` (short): a sell-off on rising open interest (new shorts, or longs adding into the fall) tends to
    continue as those positions are later forced out. Short it and ride it.

A signal at T enters at the open of the bar `delay_min` minutes after T's bar (delay 0: the next bar). The trade
exits at the first of: the stop (`sl_pct` % against the entry; at the bar's open if it gaps through), the target
(`tp_pct` % in favour, filled at the target), or the close of the `hold_min`-th bar. A bar that touches both the stop
and the target counts as a stop. Both sides are taker fills paying the fee and `slippage_bps` (crash books are
thin: 5 bps a side by default, to be stressed at 10 and 20). Queue position and square-root market impact (Almgren
& Chriss 2000) are not modelled, which is why slippage is a parameter to stress rather than a guess. One position
per symbol; after an exit the symbol waits `window_min` minutes before a new signal counts, so one crash is one
trade, not a string of re-entries.

Sizing is volatility-targeted, as in time-series momentum (Moskowitz, Ooi & Pedersen 2012): each trade's notional
is vol_target / sigma of the symbol's share, sigma the annualized volatility of its 1-minute returns over the 7 days
before the crash window (so the crash itself doesn't shrink the bet), capped at MAX_WEIGHT. A quiet BTC and a wild
altcoin then risk about the same. Every symbol has an equal share of the portfolio; vol_target 0 gives every trade
weight 1. P&L is booked on the exit day. Crashes hit every coin at once, so trades are not independent: the
t-statistic is on daily P&L (Newey-West), not on trades.

As a baseline the same exits are applied to an entry at every 00:00 UTC, in the trade's direction: an edge has to
come from the crash, not from the exits or from the market's drift.

The holdout is a time boundary shared by every crash study (results/crash_partition.json): the first run reserves
the last HOLDOUT_DAYS of what is cached and everything fetched later. Research runs drop all data from the boundary
on; `--holdout` tests a finished configuration once on the trades signalled from the boundary on (its volume
baseline still warms up on the data before). The boundary only moves later (`lab crash release`).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from lab import crash_data
from lab.carry_study import (
    DEFAULT_SYMBOLS,
    expected_max_sharpe,
    newey_west_t,
    probabilistic_sharpe,
)
from lab.fast_research import HoldoutGuard
from lab.persistence import atomic_json, file_lock, read_json

MODES = ("wick", "breakdown")
HOLDOUT_DAYS = 180
PARTITION = "crash_partition.json"
BASE_DAYS = 7
OI_DELAY = pd.Timedelta(minutes=5)
OI_STALE_MIN = 15  # an open-interest snapshot older than this (a gap in the archive) is not used
MAX_WEIGHT = 2.0
MINUTES_PER_YEAR = 365 * 1440
FORWARD_MIN = (5, 15, 60, 240, 1440)
SYMBOLS = DEFAULT_SYMBOLS
# Per mode: the first configuration, registered before any data was looked at (ROADMAP.md, Track D).
PRESETS = {
    "wick": {"drop_pct": 5.0, "window_min": 60, "vol_mult": 3.0, "oi_pct": 1.0, "tp_pct": 3.0, "sl_pct": 4.0, "hold_min": 240},
    "breakdown": {"drop_pct": 3.0, "window_min": 60, "vol_mult": 3.0, "oi_pct": 0.0, "tp_pct": 8.0, "sl_pct": 3.0, "hold_min": 1440},
}  # fmt: skip


@dataclass(frozen=True)
class CrashSettings:
    mode: str = "wick"
    drop_pct: float = 5.0
    window_min: int = 60
    vol_mult: float = 3.0
    oi_pct: float = 1.0  # wick: OI fell at least this %; breakdown: OI rose at least this % (0: rose at all)
    tp_pct: float = 3.0  # 0: no target
    sl_pct: float = 4.0  # 0: no stop
    hold_min: int = 240
    delay_min: int = 1
    fee_bps: float = 5.0  # taker, per side
    slippage_bps: float = 5.0  # per side
    vol_target: float = 0.5  # annualized volatility each trade is sized to (0: weight 1)
    side: int = field(init=False)

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if self.drop_pct <= 0 or self.window_min < 1 or self.hold_min < 1 or self.delay_min < 0:
            raise ValueError("drop_pct > 0, window_min >= 1, hold_min >= 1 and delay_min >= 0 are required")
        if (
            min(self.vol_target, self.vol_mult, self.oi_pct, self.tp_pct, self.sl_pct, self.fee_bps, self.slippage_bps)
            < 0
        ):
            raise ValueError("thresholds, fees and slippage can't be negative")
        object.__setattr__(self, "side", 1 if self.mode == "wick" else -1)

    @classmethod
    def preset(cls, mode: str, **overrides) -> CrashSettings:
        return cls(mode=mode, **{**PRESETS.get(mode, {}), **{k: v for k, v in overrides.items() if v is not None}})

    @property
    def cost(self) -> float:
        """Round trip, fraction of notional."""
        return 2 * (self.fee_bps + self.slippage_bps) / 1e4


def bars(klines: pd.DataFrame, oi: pd.DataFrame | None) -> pd.DataFrame:
    """On a complete 1-minute grid (missing minutes are NaN rows): the kline columns and `oi`, the latest open
    interest public at the end of each minute (NaN when there is none from the last OI_STALE_MIN minutes)."""
    k = klines.set_index("ts")
    grid = pd.date_range(k.index[0], k.index[-1], freq="min")
    b = k.reindex(grid)
    if oi is not None and len(oi):
        o = oi.set_index("ts")["oi_value"]
        o = o[~o.index.duplicated(keep="last")].sort_index()
        o.index = o.index + OI_DELAY
        b["oi"] = o.reindex(grid, method="ffill", tolerance=pd.Timedelta(minutes=OI_STALE_MIN))
    else:
        b["oi"] = np.nan
    return b


def signals(b: pd.DataFrame, s: CrashSettings) -> np.ndarray:
    """True at the end of every minute where the crash rule holds, from data up to that minute only."""
    w = s.window_min
    close = b["close"]
    ret = close / close.shift(w) - 1
    vol = b["quote_volume"].rolling(w, min_periods=w).sum()
    base = b["quote_volume"].rolling(BASE_DAYS * 1440, min_periods=1440).median().shift(w) * w
    oi_chg = b["oi"] / b["oi"].shift(w) - 1
    if s.mode == "wick":
        oi_ok = oi_chg <= -s.oi_pct / 100
    else:
        oi_ok = oi_chg >= s.oi_pct / 100 if s.oi_pct else oi_chg > 0
    hit = (ret <= -s.drop_pct / 100) & (vol >= s.vol_mult * base) & oi_ok
    return hit.fillna(False).to_numpy(dtype=bool)


def _exit(o, h, lo, c, j: int, s: CrashSettings) -> tuple[int, float, str] | None:
    """Simulates one trade entered at the open of bar j. Returns (exit bar, gross return, reason)."""
    n = len(o)
    entry = o[j]
    if not np.isfinite(entry):
        return None
    side = s.side
    stop = entry * (1 - side * s.sl_pct / 100) if s.sl_pct else None
    target = entry * (1 + side * s.tp_pct / 100) if s.tp_pct else None
    last = min(j + s.hold_min, n) - 1
    for k in range(j, last + 1):
        if not np.isfinite(c[k]):
            continue
        if stop is not None and side * (lo[k] if side > 0 else h[k]) <= side * stop:
            fill = stop if k == j or side * o[k] >= side * stop else o[k]  # gapped through: the open
            return k, side * (fill / entry - 1), "stop"
        if target is not None and side * (h[k] if side > 0 else lo[k]) >= side * target:
            return k, side * (target / entry - 1), "target"
    for k in range(last, n):  # the first close at or after the time exit (a hole in the data delays it)
        if np.isfinite(c[k]):
            return k, side * (c[k] / entry - 1), "time"
    return None


def trades(b: pd.DataFrame, s: CrashSettings, entries: np.ndarray | None = None) -> pd.DataFrame:
    """One row per trade: signal, entry and exit times, gross and net return, exit reason. `entries` replaces the
    crash signal (the baseline); a trade's signal needs the symbol flat and the cooldown passed."""
    sig = signals(b, s) if entries is None else entries
    wt = weights(b, s)
    o, h, lo, c = (b[x].to_numpy(dtype=float) for x in ("open", "high", "low", "close"))
    rows, free_from = [], 0
    for i in np.flatnonzero(sig):
        if i < free_from or wt[i] <= 0:
            continue
        j = i + 1 + s.delay_min
        if j >= len(o):
            break
        done = _exit(o, h, lo, c, j, s)
        if done is None:
            continue
        k, gross, why = done
        rows.append((b.index[i], b.index[j] - pd.Timedelta(minutes=1), b.index[k], gross, gross - s.cost, wt[i], why))
        free_from = k + s.window_min
    return pd.DataFrame(rows, columns=["signal", "entry", "exit", "gross", "net", "weight", "reason"])


def weights(b: pd.DataFrame, s: CrashSettings) -> np.ndarray:
    """Per minute T: the notional a trade signalled at T gets, vol_target / sigma, sigma from the BASE_DAYS before
    the crash window. NaN sigma (not enough history) is weight 0: no trade is sized blind."""
    if not s.vol_target:
        return np.ones(len(b))
    r = np.log(b["close"]).diff()
    sigma = r.rolling(BASE_DAYS * 1440, min_periods=1440).std().shift(s.window_min) * math.sqrt(MINUTES_PER_YEAR)
    return np.minimum(s.vol_target / sigma, MAX_WEIGHT).fillna(0.0).to_numpy()


def daily_entries(b: pd.DataFrame) -> np.ndarray:
    """The baseline's signals: the minute ending at 00:00 UTC, every day."""
    return np.asarray((b.index.hour == 0) & (b.index.minute == 0))


def forward_returns(b: pd.DataFrame, sig: np.ndarray, side: int) -> dict[int, np.ndarray]:
    """For every signal (cooldown aside): the move in the trade's direction from the next bar's open to the close
    `m` minutes later, gross. What the detector sees, before any exit rule."""
    o, c = b["open"].to_numpy(dtype=float), b["close"].to_numpy(dtype=float)
    idx = np.flatnonzero(sig)
    out = {}
    for m in FORWARD_MIN:
        ok = idx[idx + m < len(c)]
        r = side * (c[ok + m] / o[ok + 1] - 1)
        out[m] = r[np.isfinite(r)]
    return out


def first_signals(sig: np.ndarray, gap: int) -> np.ndarray:
    """Only the first minute of each run of signals (a new run needs `gap` quiet minutes): one per crash."""
    idx = np.flatnonzero(sig)
    keep = np.zeros_like(sig)
    if len(idx):
        keep[idx[np.r_[True, np.diff(idx) > gap]]] = True
    return keep


def summary(t: dict[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """Portfolio statistics on daily net P&L: equal notional per symbol, every day of the span counted."""
    days = pd.date_range(start.floor("D"), end.floor("D"), freq="D")
    n_sym = max(len(t), 1)
    allt = pd.concat([v.assign(symbol=k) for k, v in t.items()], ignore_index=True) if t else pd.DataFrame()
    if allt.empty:
        daily = pd.Series(0.0, index=days)
    else:
        pnl = allt["net"] * allt["weight"]
        daily = pnl.groupby(allt["exit"].dt.floor("D")).sum().reindex(days, fill_value=0.0) / n_sym
    x = daily.to_numpy()
    n = len(x)
    sd = float(x.std(ddof=1)) if n > 1 else math.nan
    sr = float(x.mean() / sd) if sd and sd > 0 else math.nan
    eq = np.cumsum(x)
    dd = float((np.maximum.accumulate(np.maximum(eq, 0)) - eq).max()) if n else 0.0
    years = n / 365 if n else math.nan
    net = allt["net"].to_numpy() if len(allt) else np.array([])
    return {
        "start": str(days[0].date()) if n else "",
        "end": str(days[-1].date()) if n else "",
        "days": n,
        "trades": len(net),
        "trade_days": int((daily != 0).sum()),
        "mean_bps": float(net.mean() * 1e4) if len(net) else math.nan,
        "median_bps": float(np.median(net) * 1e4) if len(net) else math.nan,
        "win_%": float((net > 0).mean() * 100) if len(net) else math.nan,
        "apr": float(x.sum() / years * 100) if n else math.nan,
        "sharpe": float(sr * math.sqrt(365)) if math.isfinite(sr) else math.nan,
        "sr_daily": sr,
        "t_nw": newey_west_t(x),
        "max_dd": dd * 100,
        "skew": float(pd.Series(x).skew()) if n > 2 else 0.0,
        "kurt": float(pd.Series(x).kurt()) + 3 if n > 3 else 3.0,
    }


def per_symbol(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for sym, v in t.items():
        rows.append(
            {
                "symbol": sym,
                "trades": len(v),
                "mean_bps": v["net"].mean() * 1e4 if len(v) else math.nan,
                "win_%": (v["net"] > 0).mean() * 100 if len(v) else math.nan,
                "stops": int((v["reason"] == "stop").sum()),
                "targets": int((v["reason"] == "target").sum()),
                "net_bps": v["net"].sum() * 1e4,
            }
        )
    return pd.DataFrame(rows)


@dataclass
class Run:
    trades: dict[str, pd.DataFrame]
    baseline: dict[str, pd.DataFrame]
    forward: dict[int, np.ndarray]
    start: pd.Timestamp
    end: pd.Timestamp
    missing: list[str]


def run(root: Path, symbols: list[str], s: CrashSettings, boundary: pd.Timestamp, holdout: bool = False) -> Run:
    """Trades per symbol on research data (before the boundary) or, with holdout, those signalled from it on."""
    out, base, fwd, missing = {}, {}, {m: [] for m in FORWARD_MIN}, []
    start = end = None
    for sym in symbols:
        k = crash_data.load(root, "klines", sym)
        if k is not None and not holdout:
            k = k[k["ts"] < boundary]
        if k is None or len(k) < 2 * 1440:
            missing.append(sym)
            continue
        b = bars(k, crash_data.load(root, "oi", sym))
        keep = b.index >= boundary if holdout else np.ones(len(b), dtype=bool)
        if not keep.any():
            missing.append(sym)
            continue
        sig = signals(b, s) & keep
        out[sym] = trades(b, s, sig)
        base[sym] = trades(b, s, daily_entries(b) & keep)
        for m, r in forward_returns(b, first_signals(sig, s.window_min), s.side).items():
            fwd[m].append(r)
        first, last = b.index[keep][0], b.index[-1]
        start = first if start is None else min(start, first)
        end = last if end is None else max(end, last)
    forward = {m: np.concatenate(v) if v else np.array([]) for m, v in fwd.items()}
    return Run(out, base, forward, start, end, missing)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def boundary(results_dir: Path, root: Path) -> pd.Timestamp:
    """The crash holdout start. Reserved on first use: the last HOLDOUT_DAYS of what is cached, and later data."""
    path = Path(results_dir) / PARTITION
    with file_lock(path.with_name(path.name + ".lock")):
        if path.exists():
            try:
                return pd.Timestamp(read_json(path)["holdout_start"])
            except (OSError, ValueError, KeyError) as e:
                raise HoldoutGuard(f"unreadable {path}; restore it, the holdout depends on it: {e}") from e
        last = crash_data.last_cached(root)
        if last is None:
            raise ValueError(f"no crash data in {root}; run `lab crash fetch` first")
        start = (last - pd.Timedelta(days=HOLDOUT_DAYS)).floor("D")
        hist = [{"at": _now(), "holdout_start": start.isoformat(), "reason": "first crash study"}]
        atomic_json(path, {"version": 1, "holdout_start": start.isoformat(), "history": hist})
        return start


def release(results_dir: Path, root: Path, reason: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Moves the boundary to the end of what is cached now: the holdout becomes research data, and data fetched
    from here on is the new holdout. Returns (old, new)."""
    if not reason.strip():
        raise ValueError("a reason is required: it is recorded with the new boundary")
    old = boundary(results_dir, root)
    new = crash_data.last_cached(root) or old
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


def params_of(s: CrashSettings, symbols: list[str]) -> dict:
    p = asdict(s)
    del p["side"]
    return {**p, "symbols": sorted(symbols)}


def deflated_sharpe(st: dict, trial_srs: list[float]) -> float:
    return probabilistic_sharpe(st["sr_daily"], st["days"], st["skew"], st["kurt"], expected_max_sharpe(trial_srs))
