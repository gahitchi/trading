"""Predeclared regime selection with dated eligibility, retirement and cash fallback.

No live optimization: this policy must be evaluated as a whole before deployment.
Only completed, common-timestamp history is used, never a future schedule entry.
"""

import math
from datetime import datetime
from itertools import pairwise

from lab.authority import digest
from lab.persistence import read_json

REGIMES = {"up", "down", "range", "volatile"}


def timestamp(value):
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise ValueError("bank eligibility times require an explicit timezone")
    return int(date.timestamp() * 1e9)


class StrategyBank:
    def __init__(self, manifest, specs, tickers):
        if manifest.get("version") != 1 or manifest.get("selection") != "fixed_priority_regime_v1":
            raise ValueError("unknown strategy bank policy")
        self.manifest = manifest
        self.benchmark = manifest["benchmark"]
        self.window = manifest["window"]
        self.volatility = manifest["high_volatility"]
        self.trend = manifest["trend_band"]
        if self.benchmark not in tickers or type(self.window) is not int or not 20 <= self.window <= 252:
            raise ValueError("bank requires a configured benchmark and 20..252 completed returns")
        if any(
            type(v) not in (float, int) or not math.isfinite(v) or not 0 < v < 2 for v in (self.volatility, self.trend)
        ):
            raise ValueError("invalid bank regime thresholds")
        # Optional regime persistence (Gemini audit F-R17); absent keys keep a manifest's original behaviour.
        # A regime is left only past its exit threshold (a share of the entry one), and a new regime must hold
        # for min_dwell_bars consecutive bars before the selection follows it.
        self.volatility_exit = manifest.get("volatility_exit", 1.0)
        self.trend_exit = manifest.get("trend_exit", 1.0)
        self.min_dwell = manifest.get("min_dwell_bars", 1)
        if any(type(v) not in (float, int) or not 0 < v <= 1 for v in (self.volatility_exit, self.trend_exit)):
            raise ValueError("regime exit thresholds must be shares (0, 1] of the entry thresholds")
        if type(self.min_dwell) is not int or not 1 <= self.min_dwell <= 63:
            raise ValueError("min_dwell_bars must be an integer from 1 to 63")
        self.regime = None  # the regime in force
        self.pending = (None, 0, None)  # (candidate regime, consecutive bars, last bar counted)
        actual = {s.name: digest({"name": s.name, "params": s.params, "sha256": s.sha256}) for s in specs}
        entries = manifest["entries"]
        if (
            not isinstance(entries, list)
            or len({e["name"] for e in entries}) != len(entries)
            or {e["name"] for e in entries} != set(actual)
        ):
            raise ValueError("bank entries must exactly match unique configured signals")
        self.entries = []
        for entry in entries:
            if entry["artifact"] != actual[entry["name"]]:
                raise ValueError("bank strategy artifact or parameters changed")
            if (
                type(entry["priority"]) is not int
                or not isinstance(entry["regimes"], list)
                or not entry["regimes"]
                or set(entry["regimes"]) - REGIMES
            ):
                raise ValueError("bank requires integer priorities and known nonempty regimes")
            start, end = timestamp(entry["eligible_after"]), timestamp(entry["expires_at"])
            retired = timestamp(entry["retired_at"]) if entry.get("retired_at") else end
            if end <= start:
                raise ValueError("bank eligibility interval is empty")
            self.entries.append({**entry, "start": start, "end": min(end, retired)})
        self.entries.sort(key=lambda e: (e["priority"], e["name"]))

    @classmethod
    def load(cls, path, specs, tickers):
        return cls(read_json(path), specs, tickers)

    def select(self, histories, retired=()):
        if not histories or any(not rows for rows in histories.values()):
            return None, "warming_up"
        now = min(rows[-1][0] for rows in histories.values())
        rows = [row for row in histories[self.benchmark] if row[0] <= now]
        if len(rows) < self.window + 1:
            return None, "warming_up"
        rows = rows[-self.window - 1 :]
        returns = [math.log(b[1] / a[1]) for a, b in pairwise(rows)]
        mean = sum(returns) / len(returns)
        vol = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1) * 252)
        move = rows[-1][1] / rows[0][1] - 1
        regime = self._persist(self._classify(vol, move), rows[-1][0])
        eligible = [
            e
            for e in self.entries
            if e["start"] < now < e["end"] and e["name"] not in retired and regime in e["regimes"]
        ]
        return (eligible[0]["name"] if eligible else None), regime

    def _classify(self, vol: float, move: float) -> str:
        """Thresholds with a deadband: the regime in force is kept until its exit threshold is crossed."""
        held = self.regime
        v_in = self.volatility * (self.volatility_exit if held == "volatile" else 1)
        if vol >= v_in:
            return "volatile"
        up_in = self.trend * (self.trend_exit if held == "up" else 1)
        down_in = self.trend * (self.trend_exit if held == "down" else 1)
        if move > up_in:
            return "up"
        if move < -down_in:
            return "down"
        return "range"

    def _persist(self, raw: str, bar_ts: int) -> str:
        """Follow a new regime only after it holds for min_dwell distinct bars (select() runs several times a
        bar)."""
        if self.regime is None or self.min_dwell <= 1:
            self.regime = raw
            self.pending = (None, 0, None)
            return raw
        if raw == self.regime:
            self.pending = (None, 0, None)
            return raw
        candidate, count, last = self.pending
        if candidate != raw:
            candidate, count, last = raw, 0, None
        if last != bar_ts:
            count += 1
        self.pending = (candidate, count, bar_ts)
        if count >= self.min_dwell:
            self.regime, self.pending = raw, (None, 0, None)
        return self.regime
