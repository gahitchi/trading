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
        regime = (
            "volatile"
            if vol >= self.volatility
            else "up"
            if move > self.trend
            else "down"
            if move < -self.trend
            else "range"
        )
        eligible = [
            e
            for e in self.entries
            if e["start"] < now < e["end"] and e["name"] not in retired and regime in e["regimes"]
        ]
        return (eligible[0]["name"] if eligible else None), regime
