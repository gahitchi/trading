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
    last_signal_price: dict[str, tuple[int, float]] = {}
    shortfall = []
    for r in sorted((r for r in rows if r.get("type") in ("SignalUpdate", "FillReport")), key=lambda r: r["ts"]):
        if r["type"] == "SignalUpdate":
            last_signal_price[r["ticker"]] = (r["ts"], r["price"])
        elif r["ticker"] in last_signal_price:
            ref = last_signal_price[r["ticker"]][1]
            sign = 1 if r["side"] == "BUY" else -1
            shortfall.append(sign * (r["price"] - ref) / ref * 1e4)
    out = {"equity_points": len(eq), "fills": len(fills), "risk_events": dict(risk)}
    if eq:
        s = pd.Series([e["equity"] for e in eq], index=pd.to_datetime([e["ts"] for e in eq], unit="ns"))
        out.update(
            start=str(s.index[0].date()),
            end=str(s.index[-1].date()),
            first_equity=float(s.iloc[0]),
            last_equity=float(s.iloc[-1]),
            total_return=float(s.iloc[-1] / s.iloc[0] - 1),
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
