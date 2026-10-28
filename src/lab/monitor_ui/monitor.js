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
  const r = s.recorder;
  $("rate").textContent = rate(r.events_per_s);
  $("uptime").textContent = duration(r.uptime_s);
  const span = s.catalog.first && s.catalog.last ? (Date.parse(s.catalog.last) - Date.parse(s.catalog.first)) / 1000 : null;
  $("recorded").textContent = duration(span);
  $("size").textContent = bytes(s.catalog.bytes);
  $("size").title = `${int.format(s.catalog.rows)} rows in ${int.format(s.catalog.files)} files`;
  $("stream").textContent = s.stream.files ? `${bytes(s.stream.bytes)} · ${s.stream.files} file(s)` : "none";
  $("disk").textContent = s.disk.runway_days != null ? duration(s.disk.runway_days * 86400) : `${bytes(s.disk.free)} free`;
  $("disk").title = `${bytes(s.disk.free)} free of ${bytes(s.disk.total)}` +
    (s.disk.bytes_per_day ? `, writing ${bytes(s.disk.bytes_per_day)} per day` : "");

  const feeds = $("feeds");
  if (!s.instruments.length) emptyRow(feeds, 10, "No instruments yet. Start the recorder: lab record");
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

async function poll() {
  try {
    const res = await fetch("/api/status", { cache: "no-store" });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || res.statusText);
    render(body);
    $("notice").hidden = true;
  } catch (e) {
    $("notice").textContent = `Monitor unreachable or failing: ${e.message}. Retrying.`;
    $("notice").hidden = false;
  } finally {
    setTimeout(poll, POLL_MS);
  }
}

poll();
