// Market Data Monitor: polls /api/status and renders it. Read-only; every value goes in as text, never as HTML.
"use strict";

const POLL_MS = 5000;
const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  for (const c of children) node.append(c instanceof Node ? c : String(c));
  return node;
}

const int = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const dec = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1, minimumFractionDigits: 1 });

function bytes(n) {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
  return `${i ? dec.format(n) : int.format(n)} ${units[i]}`;
}

function duration(s) {
  if (s == null) return "–";
  s = Math.max(0, s);
  if (s >= 86400) return `${dec.format(s / 86400)} d`;
  if (s >= 3600) return `${dec.format(s / 3600)} h`;
  if (s >= 60) return `${int.format(s / 60)} min`;
  return `${int.format(s)} s`;
}

function rate(r) {
  return r == null ? "–" : r >= 100 ? int.format(r) : dec.format(r);
}

function when(iso) {
  if (!iso) return "–";
  return iso.replace("T", " ").replace("+00:00", " UTC");
}

const STATUS = { ok: "Recording", degraded: "Degraded", down: "Down" };

function sparkline(points) {
  const w = 168, h = 32, pad = 4;
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("role", "img");
  if (points.length < 2) {
    svg.setAttribute("aria-label", "not enough samples yet");
    return svg;
  }
  const max = Math.max(...points.map((p) => p[1]), 1e-9);
  const x = (i) => pad + (i * (w - 2 * pad)) / (points.length - 1);
  const y = (v) => h - pad - (v / max) * (h - 2 * pad);
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join(""));
  const cross = document.createElementNS(SVG, "line");
  const dot = document.createElementNS(SVG, "circle");
  dot.setAttribute("r", "4");
  cross.setAttribute("y1", "0");
  cross.setAttribute("y2", String(h));
  cross.style.display = dot.style.display = "none";
  svg.append(path, cross, dot);
  const last = points[points.length - 1];
  svg.setAttribute("aria-label", `events per second, last ${rate(last[1])}, peak ${rate(max)}`);
  const tip = $("tip");
  svg.addEventListener("pointermove", (e) => {
    const box = svg.getBoundingClientRect();
    const i = Math.round(((e.clientX - box.left) / box.width * w - pad) / ((w - 2 * pad) / (points.length - 1)));
    const p = points[Math.min(points.length - 1, Math.max(0, i))];
    const k = points.indexOf(p);
    cross.setAttribute("x1", x(k));
    cross.setAttribute("x2", x(k));
    dot.setAttribute("cx", x(k));
    dot.setAttribute("cy", y(p[1]));
    cross.style.display = dot.style.display = "";
    tip.textContent = `${when(p[0]).slice(11)}  ${rate(p[1])} events/s`;
    tip.style.left = `${e.clientX + 12}px`;
    tip.style.top = `${e.clientY - 30}px`;
    tip.hidden = false;
  });
  svg.addEventListener("pointerleave", () => {
    cross.style.display = dot.style.display = "none";
    tip.hidden = true;
  });
  return svg;
}

function emptyRow(body, cols, text) {
  body.replaceChildren(el("tr", {}, el("td", { colspan: cols, class: "empty" }, text)));
}

function render(s) {
  $("status").dataset.status = s.status;
  $("status-label").textContent = STATUS[s.status] || s.status;
  $("checks").replaceChildren(
    ...s.checks.map((c) =>
      el("li", {}, el("span", { class: `badge ${c.state}` }, c.state), el("b", {}, c.name), el("span", {}, c.detail)),
    ),
  );
  $("rate").textContent = rate(s.events_per_s);
  const up = s.recorders.filter((r) => r.running);
  $("recorders").textContent = s.recorders.length ? `${up.length} of ${s.recorders.length}` : "none";
  $("recorders").title = s.recorders.map((r) => `${r.venue}: ${r.running ? `up ${duration(r.uptime_s)}` : "down"}`).join(", ");
  const span = s.catalog.first && s.catalog.last ? (Date.parse(s.catalog.last) - Date.parse(s.catalog.first)) / 1000 : null;
  $("recorded").textContent = duration(span);
  $("size").textContent = bytes(s.catalog.bytes);
  $("size").title = `${int.format(s.catalog.rows)} rows in ${int.format(s.catalog.files)} files`;
  // The running recorders' files, promoted hourly. Leftovers of runs that stopped without saving never are:
  // the "stream" check reports those.
  const left = s.stream.leftover, pending = s.stream.files - left.files;
  $("stream").textContent = pending ? `${bytes(s.stream.bytes - left.bytes)} · ${pending} file(s)` : "none";
  $("stream").title = left.files ? `not counted: ${bytes(left.bytes)} in ${left.files} leftover file(s)` : "";
  $("disk").textContent = s.disk.runway_days != null ? duration(s.disk.runway_days * 86400) : `${bytes(s.disk.free)} free`;
  $("disk").title = `${bytes(s.disk.free)} free of ${bytes(s.disk.total)}` +
    (s.disk.bytes_per_day ? `, writing ${bytes(s.disk.bytes_per_day)} per day` : "");

  const feeds = $("feeds");
  if (!s.instruments.length) emptyRow(feeds, 10, "No instruments yet. Start a recorder: lab record --venue binance");
  else
    feeds.replaceChildren(
      ...s.instruments.map((i) => {
        const c = i.counts;
        const age = i.last_event_age_s != null ? `${duration(i.last_event_age_s)} ago` : "–";
        return el(
          "tr", {},
          el("td", {}, i.symbol), el("td", {}, i.venue),
          el("td", {}, el("span", { class: `state ${i.state.replace(" ", "-")}` }, i.state)),
          el("td", { title: when(i.last_event) }, age),
          el("td", { class: "num" }, rate(i.events_per_s)),
          el("td", {}, sparkline(i.history)),
          ...["book", "trades", "mark", "funding"].map((k) => el("td", { class: "num" }, c[k] != null ? int.format(c[k]) : "–")),
        );
      }),
    );

  const data = $("data");
  const stored = s.instruments.filter((i) => i.catalog.files);
  if (!stored.length) emptyRow(data, 8, "Nothing in the catalog yet: data is promoted every hour and when the recorder stops.");
  else
    data.replaceChildren(
      ...stored.map((i) => {
        const k = i.catalog;
        const worst = [...k.gaps].sort((a, b) => b.seconds - a.seconds).slice(0, 3);
        return el(
          "tr", {},
          el("td", {}, i.id), el("td", {}, when(k.first)), el("td", {}, when(k.last)),
          el("td", { class: "num" }, int.format(k.rows)), el("td", { class: "num" }, bytes(k.bytes)),
          el("td", { class: "num" }, int.format(k.files)),
          el("td", { class: "num" }, k.coverage_pct != null ? `${dec.format(k.coverage_pct)} %` : "–"),
          el("td", { class: "gaps" }, worst.length ? worst.map((g) => `${duration(g.seconds)} from ${when(g.from)}`).join("; ") : "none"),
        );
      }),
    );

  const types = Object.entries(s.catalog.by_type);
  if (!types.length) emptyRow($("types"), 4, "–");
  else
    $("types").replaceChildren(
      ...types.map(([name, t]) =>
        el("tr", {}, el("td", {}, name), el("td", { class: "num" }, int.format(t.rows)),
          el("td", { class: "num" }, bytes(t.bytes)), el("td", { class: "num" }, int.format(t.files))),
      ),
    );
  $("updated").textContent = `Updated ${when(s.now).slice(11)}`;
}

function money(v, signed = false) {
  if (v == null) return "–";
  const text = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Math.abs(v));
  return `${v < 0 ? "−" : signed && v > 0 ? "+" : ""}${text}`;
}

function signed(node, v) {
  node.textContent = money(v, true);
  node.className = v > 0 ? "pos" : v < 0 ? "neg" : "";
}

function equityChart(box, points) {
  const w = Math.max(box.clientWidth, 280), h = 160, left = 64, right = 8, top = 10, bottom = 22;
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", "chart");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("role", "img");
  if (points.length < 2) {
    svg.setAttribute("aria-label", "no equity history yet");
    const t = document.createElementNS(SVG, "text");
    t.setAttribute("x", left);
    t.setAttribute("y", h / 2);
    t.textContent = "The curve starts after two minutes of paper trading.";
    svg.append(t);
    return box.replaceChildren(svg);
  }
  const vals = points.map((p) => p[1]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
  const x = (i) => left + (i * (w - left - right)) / (points.length - 1);
  const y = (v) => top + ((hi - v) * (h - top - bottom)) / (hi - lo);
  const add = (tag, attrs, text) => {
    const n = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text != null) n.textContent = text;
    svg.append(n);
    return n;
  };
  add("line", { class: "base", x1: left, x2: w - right, y1: h - bottom, y2: h - bottom });
  add("text", { x: left - 6, y: y(hi) + 4, "text-anchor": "end" }, money(hi));
  add("text", { x: left - 6, y: y(lo) + 4, "text-anchor": "end" }, money(lo));
  add("text", { x: left, y: h - 6 }, when(points[0][0]).slice(5, 16));
  add("text", { x: w - right, y: h - 6, "text-anchor": "end" }, when(points[points.length - 1][0]).slice(5, 16));
  add("path", { d: points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join("") });
  const cross = add("line", { class: "cross", y1: top, y2: h - bottom });
  const dot = add("circle", { r: 4 });
  cross.style.display = dot.style.display = "none";
  svg.setAttribute("aria-label", `equity from ${money(vals[0])} to ${money(vals[vals.length - 1])}`);
  const tip = $("tip");
  svg.addEventListener("pointermove", (e) => {
    const box2 = svg.getBoundingClientRect();
    const px = ((e.clientX - box2.left) / box2.width) * w;
    const k = Math.min(points.length - 1, Math.max(0, Math.round(((px - left) * (points.length - 1)) / (w - left - right))));
    cross.setAttribute("x1", x(k));
    cross.setAttribute("x2", x(k));
    dot.setAttribute("cx", x(k));
    dot.setAttribute("cy", y(points[k][1]));
    cross.style.display = dot.style.display = "";
    tip.textContent = `${when(points[k][0]).slice(0, 16)}  ${money(points[k][1])}`;
    tip.style.left = `${e.clientX + 12}px`;
    tip.style.top = `${e.clientY - 30}px`;
    tip.hidden = false;
  });
  svg.addEventListener("pointerleave", () => {
    cross.style.display = dot.style.display = "none";
    tip.hidden = true;
  });
  box.replaceChildren(svg);
}

const PAPER = { running: "Paper trading", stopped: "Paper trading stopped", none: "Not run yet" };

function event(e) {
  if (e.kind === "denied") return `${e.side} ${e.quantity} ${e.instrument}: ${e.reason}`;
  if (e.kind === "state") return `${e.from} → ${e.to}${e.reason ? `: ${e.reason}` : ""}`;
  if (e.kind === "day_start") return `${e.day}, start equity ${money(e.start_equity)}`;
  return Object.entries(e).filter(([k]) => !["kind", "at"].includes(k)).map(([k, v]) => `${k}=${v}`).join(", ");
}

function renderPaper(p) {
  $("paper").hidden = !p;
  if (!p) return;
  $("paper-status").dataset.status = p.status === "running" ? (p.checks.some((c) => c.state !== "ok") ? "degraded" : "ok") : p.status === "stopped" ? "down" : "unknown";
  $("paper-label").textContent = PAPER[p.status];
  $("paper-checks").replaceChildren(
    ...p.checks.map((c) => el("li", {}, el("span", { class: `badge ${c.state}` }, c.state), el("b", {}, c.name), el("span", {}, c.detail))),
  );
  $("kill").hidden = !p.kill;
  if (p.kill) $("kill").textContent = `Kill switch set (${p.kill.mode}): ${p.kill.reason || "no reason given"}. Clear it with lab resume.`;
  const r = p.report;
  const risk = (r && r.risk) || {};
  const limits = risk.limits || {};
  const ccy = limits.currency || "USDT";
  $("p-ccy").textContent = ccy;
  $("p-state").textContent = risk.state || "–";
  $("p-equity").textContent = r ? money(r.equity) : "–";
  signed($("p-pnl"), r ? r.pnl : null);
  $("p-day").textContent = r && r.day_pnl != null && limits.max_daily_loss
    ? `${money(r.day_pnl, true)} / −${money(limits.max_daily_loss)}` : "–";
  const pos = r && r.position;
  $("p-position").textContent = pos ? `${pos.net > 0 ? "long" : "short"} ${Math.abs(pos.net)} @ ${money(pos.avg_px)}` : r ? "flat" : "–";
  $("p-position").title = pos && pos.unrealized != null ? `unrealized ${money(pos.unrealized, true)} ${ccy}` : "";
  $("p-trips").textContent = r ? `${int.format(r.round_trips)} · t ${r.t_stat.toFixed(2)}` : "–";
  $("p-trips").title = r ? `mean ${money(r.mean_trip, true)} ${ccy} per round trip, ${int.format(r.fills)} fills, ${money(r.commissions)} ${ccy} fees` : "";
  equityChart($("p-curve"), (r && r.equity_curve) || []);
  const denials = Object.entries(risk.denials || {}).map(([k, n]) => `${k} ${n}`).join(", ") || "none";
  const rows = r ? [
    ["Strategy", `${r.strategy} on ${r.instrument}`],
    ["Parameters", Object.entries(r.params || {}).map(([k, v]) => `${k.replaceAll("_", " ")} ${v}`).join(", ")],
    ["Run started", when(r.started)],
    ["Daily loss cap", `${money(limits.max_daily_loss)} ${ccy}${risk.breach ? ` (reached, loss ${money(risk.breach.loss)})` : ""}`],
    ["Position / gross / order cap", `${money(limits.max_position_notional)} / ${money(limits.max_gross_notional)} / ${money(limits.max_order_notional)} ${ccy}`],
    ["Live orders / submit rate", `${limits.max_live_orders} / ${limits.max_order_submit_rate}`],
    ["Orders denied by risk", denials],
  ] : [["", "No report yet. Start paper trading with lab fast paper BTCUSDT."]];
  $("p-limits").replaceChildren(...rows.map(([k, v]) => el("tr", {}, el("th", { scope: "row" }, k), el("td", {}, v))));
  if (!p.events.length) emptyRow($("p-events"), 3, "No risk events yet.");
  else $("p-events").replaceChildren(...p.events.map((e) => el("tr", {}, el("td", {}, when(e.at).slice(0, 19)), el("td", {}, e.kind), el("td", { class: "gaps" }, event(e)))));
}

async function poll() {
  try {
    const res = await fetch("/api/status", { cache: "no-store" });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || res.statusText);
    render(body);
    renderPaper(body.paper);
    $("notice").hidden = true;
  } catch (e) {
    $("notice").textContent = `Monitor unreachable or failing: ${e.message}. Retrying.`;
    $("notice").hidden = false;
  } finally {
    setTimeout(poll, POLL_MS);
  }
}

poll();
