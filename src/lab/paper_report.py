"""Summarize paper trading from the agents' journal: performance, costs, implementation shortfall."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd


def load_journal(paths: list[Path]) -> list[dict]:
    rows = []
    for p in sorted(paths):
        for line in p.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def summarize(rows: list[dict]) -> dict:
    """Implementation shortfall per fill: how far the fill price was from the close the
    decision was based on, in basis points, signed so that positive = cost. The backtests
    assume this gap is the overnight move plus one tick; paper trading measures it."""
    eq = [r for r in rows if r.get("type") == "EquitySnapshot"]
    fills = [r for r in rows if r.get("type") == "FillReport"]
    risk = Counter(r["kind"] for r in rows if r.get("type") == "RiskEvent")
    shortfall = []
    unmeasured = 0
    for r in fills:
        ref = r.get("decision_price")
        if ref is None or not np.isfinite(ref) or ref <= 0 or r.get("decision_ts") is None:
            unmeasured += 1
            continue
        sign = 1 if r["side"] == "BUY" else -1
        shortfall.append(sign * (r["price"] - ref) / ref * 1e4)
    out = {"equity_points": len(eq), "fills": len(fills), "risk_events": dict(risk), "shortfall_unmeasured": unmeasured}
    if eq:
        s = pd.Series([e["equity"] for e in eq], index=pd.to_datetime([e["ts"] for e in eq], unit="ns"))
        external = any(r.get("kind") in ("external_cash", "cash_adjusted", "ledger_reset", "adopted") for r in rows)
        out.update(
            start=str(s.index[0].date()),
            end=str(s.index[-1].date()),
            first_equity=float(s.iloc[0]),
            last_equity=float(s.iloc[-1]),
            total_return=None if external or s.iloc[0] <= 0 else float(s.iloc[-1] / s.iloc[0] - 1),
            return_qualified=not external and s.iloc[0] > 0,
            return_note="external cash/ownership adjustment needs classified flow evidence"
            if external
            else "no external-flow event recorded",
            max_drawdown=float((1 - s / s.cummax()).max()),
        )
    if fills:
        out["commissions"] = round(sum(f["commission"] for f in fills), 2)
        out["traded_value"] = round(sum(f["qty"] * f["price"] for f in fills), 2)
    if shortfall:
        a = np.array(shortfall)
        out["shortfall_bps"] = {
            "mean": float(a.mean()),
            "median": float(np.median(a)),
            "p90": float(np.quantile(a, 0.9)),
            "fills_measured": len(a),
        }
    return out
