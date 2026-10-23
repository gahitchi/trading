"""Self-contained HTML tearsheet for a saved run: open results/<run>/report.html in a browser."""

from __future__ import annotations

import base64
import html
import io
from pathlib import Path

import matplotlib
import matplotlib.ticker

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from lab import data

INK, MUTED, GRID = "#1f2328", "#656d76", "#d8dee4"
SERIES = ["#1f2328", "#0969da", "#bc4c00", "#1a7f37", "#8250df", "#bf3989", "#9a6700"]


def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=144, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _style(ax, title):
    ax.set_title(title, loc="left", fontsize=11, color=INK, pad=8)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.spines[["top", "right"]].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=8)


def _charts(curves: pd.DataFrame) -> list[tuple[str, str]]:
    out = []
    fig, ax = plt.subplots(figsize=(10, 3.6))
    for i, c in enumerate(curves.columns):
        ax.plot(
            curves.index,
            curves[c] / curves[c].dropna().iloc[0],
            lw=1.6 if i == 0 else 1.2,
            color=SERIES[i % len(SERIES)],
            label=c,
            ls="--" if i == 0 else "-",
        )
    positive = (curves > 0).all().all()
    if positive:
        ax.set_yscale("log")
        ax.yaxis.set_major_locator(matplotlib.ticker.LogLocator(base=10, subs=(1, 2, 3, 5)))
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}×"))
    ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    _style(ax, "Growth of 1 (log scale)" if positive else "Equity includes insolvency (linear scale)")
    out.append(("Equity", _png(fig)))

    fig, ax = plt.subplots(figsize=(10, 2.6))
    for i, c in enumerate(curves.columns):
        s = curves[c].dropna()
        ax.plot(s.index, (s / s.cummax() - 1) * 100, lw=1.0, color=SERIES[i % len(SERIES)], ls="--" if i == 0 else "-")
    _style(ax, "Drawdown (%)")
    out.append(("Drawdown", _png(fig)))

    if curves.shape[1] > 1 and positive:
        fig, ax = plt.subplots(figsize=(10, 2.6))
        base = curves.iloc[:, 0].pct_change()
        for i, c in enumerate(curves.columns[1:], start=1):
            act = (curves[c].pct_change() - base).dropna()
            roll = act.rolling(252).mean() / act.rolling(252).std() * np.sqrt(252)
            ax.plot(roll.index, roll, lw=1.1, color=SERIES[i % len(SERIES)], label=c)
        ax.axhline(0, color=MUTED, lw=0.8)
        ax.legend(frameon=False, fontsize=8, loc="upper left")
        _style(ax, "Rolling 1-year active Sharpe vs buy-and-hold")
        out.append(("Active", _png(fig)))
    return out


def _monthly_table(equity: pd.Series) -> str:
    if equity.empty or (equity <= 0).any():
        return "<p>Monthly returns unavailable for nonpositive equity.</p>"
    ends = equity.resample("ME").last().dropna()
    previous = ends.shift(1)
    previous.iloc[0] = equity.iloc[0]
    m = ends / previous - 1
    if m.empty:
        return ""
    t = pd.DataFrame({"y": m.index.year, "m": m.index.month, "r": m.values}).pivot(index="y", columns="m", values="r")
    rows = []
    for y, r in t.iterrows():
        cells = []
        for mo in range(1, 13):
            v = r.get(mo)
            if v is None or pd.isna(v):
                cells.append("<td></td>")
                continue
            a = min(abs(v) / 0.1, 1.0) * 0.55
            bg = f"rgba(26,127,55,{a:.2f})" if v >= 0 else f"rgba(207,34,46,{a:.2f})"
            cells.append(f'<td style="background:{bg}">{v * 100:+.1f}</td>')
        yr = (1 + r.dropna()).prod() - 1
        rows.append(f"<tr><th>{y}</th>{''.join(cells)}<td class='yr'>{yr * 100:+.1f}</td></tr>")
    head = "".join(f"<th>{x}</th>" for x in "JFMAMJJASOND")
    return f"<table class='monthly'><tr><th></th>{head}<th>Year</th></tr>{''.join(rows)}</table>"


def _fmt(k, v):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return ""
    if k in ("total_return", "cagr", "volatility", "max_drawdown", "avg_exposure"):
        return f"{v * 100:+.1f}%" if k in ("total_return", "cagr") else f"{v * 100:.1f}%"
    if k == "active_sharpe_ci":
        return f"{v[0]:+.2f} .. {v[1]:+.2f}"
    if isinstance(v, float):
        return f"{v:.2f}"
    return html.escape(str(v))


def write_report(path: Path, comparison, results, extra: dict | None = None) -> Path:
    curves = pd.DataFrame({r.label: r.equity for r in results})
    cols = [
        ("label", "Strategy"),
        ("total_return", "Return"),
        ("cagr", "CAGR"),
        ("volatility", "Vol"),
        ("sharpe", "Sharpe"),
        ("sortino", "Sortino"),
        ("max_drawdown", "Max DD"),
        ("avg_exposure", "Exposure"),
        ("fills", "Fills"),
        ("commissions", "Fees $"),
        ("sharpe_edge", "Sharpe edge"),
        ("active_sharpe", "Active Sharpe"),
        ("active_sharpe_ci", "95% CI"),
        ("psr", "PSR"),
        ("dsr", "DSR"),
        ("trials", "Trials"),
    ]
    head = "".join(f"<th>{h}</th>" for _, h in cols)
    body = "".join(
        "<tr>" + "".join(f"<td>{_fmt(k, row.get(k))}</td>" for k, _ in cols) + "</tr>" for row in comparison.rows
    )
    q = comparison.data_quality or {}
    warning = data.quality_warning(q) if q else None
    banner = f"<div class='warn'><b>Data quality:</b> {html.escape(warning)}</div>" if warning else ""
    charts = "".join(f"<figure><img alt='{t}' src='data:image/png;base64,{b}'></figure>" for t, b in _charts(curves))
    monthly = "".join(f"<h3>{html.escape(r.label)}</h3>{_monthly_table(r.equity)}" for r in results)
    extra_html = ""
    if extra and "null_test" in extra:
        nt = extra["null_test"]
        extra_html = (
            f"<h2>Noise floor</h2><p>Observed Sharpe edge {nt['observed_sharpe_edge']:+.2f}; "
            f"{nt['samples']} block-bootstrap samples: mean {nt['null_mean']:+.2f}, sd {nt['null_std']:.2f}, "
            f"95th percentile {nt['null_p95']:+.2f}. <b>p = {nt['p_value']:.3f}</b>.</p>"
        )
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>trading-lab report</title>
<style>
body{{font:14px/1.5 -apple-system,system-ui,Segoe UI,Roboto,sans-serif;color:{INK};max-width:1100px;margin:32px auto;padding:0 16px}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 8px}} h3{{font-size:13px;margin:16px 0 6px;color:{MUTED}}}
.sub{{color:{MUTED};margin:0 0 16px}} .warn{{background:#fff8c5;border:1px solid #d4a72c55;padding:8px 12px;border-radius:6px;margin:12px 0}}
table{{border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:12px;width:100%;overflow-x:auto;display:block}}
th,td{{padding:5px 8px;border-bottom:1px solid {GRID};text-align:right;white-space:nowrap}}
th:first-child,td:first-child{{text-align:left}} th{{color:{MUTED};font-weight:600}}
.monthly td,.monthly th{{text-align:center;min-width:38px}} .monthly .yr{{font-weight:600}}
figure{{margin:12px 0}} img{{max-width:100%}} .note{{color:{MUTED};font-size:12px}}
</style></head><body>
<h1>{html.escape(comparison.period)} · {comparison.start} → {comparison.end}</h1>
<p class="sub">{len(comparison.tickers)} stocks: {html.escape(", ".join(comparison.tickers[:30]))}{" …" if len(comparison.tickers) > 30 else ""}
· sources: {html.escape(", ".join(q.get("sources", [])))}</p>
{banner}
<h2>Summary</h2><table><tr>{head}</tr>{body}</table>
<p class="note">Sharpe edge: Sharpe minus the control's. Active Sharpe: Sharpe of the daily return difference vs buy-and-hold,
with a block-bootstrap 95% interval. PSR: probability the true active Sharpe is above 0. DSR: the same after deflating for
every configuration tried on this data (Bailey &amp; López de Prado); 0.95 or more is significant.</p>
{extra_html}
<h2>Charts</h2>{charts}
<h2>Monthly returns (%)</h2>{monthly}
</body></html>"""
    path.write_text(doc)
    return path
