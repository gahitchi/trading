"use strict";
const token = document.querySelector('meta[name="app-token"]').content;
const form = document.querySelector("#setup");
const $ = (id) => document.getElementById(id);
let initialized = false, busy = false, last;
const notes = {
  calm: "Up to 30% gross exposure. Long only. Stops at an 8% drawdown.",
  cautious: "Up to 50% gross exposure. Long only. Stops at a 12% drawdown.",
  balanced: "Up to 100% gross exposure. Long only. Stops at an 18% drawdown.",
  dynamic: "Up to 135% gross exposure, shorts up to 30%. Stops at a 25% drawdown.",
  aggressive: "Up to 180% gross exposure, shorts up to 75%. Stops at a 30% drawdown."
};
const phases = {setup: "Setup", stopped: "Stopped", preparing: "Preparing", running: "Running", paused: "Paused", blocked: "Blocked"};
const titles = {
  setup: "Your plan starts here.", stopped: "Ready for your next session.", preparing: "Preparing your market data.",
  running: "Your plan is active.", paused: "New orders are paused.", blocked: "Your attention is needed."
};
const usd = new Intl.NumberFormat(undefined, {style: "currency", currency: "USD"});
const shares = new Intl.NumberFormat(undefined, {maximumFractionDigits: 4});
const when = (seconds) => new Date(seconds * 1000).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"});

function notice(message) { const el = $("notice"); el.textContent = message; el.hidden = !message; }
async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: {"X-App-Token": token, ...(body ? {"Content-Type": "application/json"} : {})},
    body: body ? JSON.stringify(body) : undefined
  });
  const value = await res.json();
  if (!res.ok) throw Error(value.error || "Action failed.");
  return value;
}
function settings() {
  const s = Object.fromEntries(new FormData(form));
  s.gold = form.elements.gold.checked;
  for (const k of ["port", "capital", "hours"]) s[k] = Number(s[k]);
  delete s.symbol;
  s.tickers = [...form.querySelectorAll('input[name="symbol"]:checked')].map(el => el.value);
  return s;
}
function riskNote() {
  $("risk-note").textContent = (notes[form.elements.profile.value] || "") + " Existing operator ceilings may lower these limits.";
}
function timeLimit(authority) {
  if (!authority) return "Not issued";
  if (authority.expires == null) return "No time limit"; // a standing autonomous mandate
  return (authority.expires * 1000 <= Date.now() ? "Expired " : "Until ") + when(authority.expires);
}
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function positions(hb, gold) {
  const box = $("positions");
  const held = Object.entries(hb.positions || {}).filter(([, qty]) => qty);
  if (gold && gold.position_oz) held.push(["XAUUSD (oz)", gold.position_oz]);
  if (!held.length) { box.replaceChildren(el("p", hb.positions ? "No open positions. The plan is in cash." : "No account snapshot yet.", "empty")); return; }
  const table = el("table"), head = el("tr");
  for (const name of ["Instrument", "Side", "Quantity"]) head.append(el("th", name));
  table.append(el("thead"), el("tbody"));
  table.tHead.append(head);
  for (const [ticker, qty] of held.sort()) {
    const row = el("tr"), side = el("td");
    side.append(el("span", qty < 0 ? "Short" : "Long", qty < 0 ? "side-tag short" : "side-tag"));
    row.append(el("td", ticker), side, el("td", shares.format(Math.abs(qty))));
    table.tBodies[0].append(row);
  }
  box.replaceChildren(table);
}
const pct = (v) => (v * 100).toFixed(1) + "%";
const times = (v) => v.toFixed(2) + "x";
function riskTable(r) {
  const box = $("risk");
  if (!r || !r.marks_current || r.gross == null) {
    box.replaceChildren(el("p", r && !r.marks_current ? "Unknown: a held stock has no current price." : "Shown while the engine runs with current prices.", "empty"));
    return;
  }
  const lim = r.limits || {};
  const rows = [
    ["Gross exposure", times(r.gross), lim.max_gross != null ? "at most " + times(lim.max_gross) : "", lim.max_gross != null && r.gross > lim.max_gross + 1e-9],
    ["Net exposure", times(r.net), lim.max_net != null ? `${times(lim.min_net)} to ${times(lim.max_net)}` : "", lim.max_net != null && (r.net > lim.max_net + 1e-9 || r.net < lim.min_net - 1e-9)],
    ["Short book", times(r.short), lim.max_short_gross != null ? "at most " + times(lim.max_short_gross) : "", lim.max_short_gross != null && r.short > lim.max_short_gross + 1e-9],
    ["Largest position", pct(r.largest), lim.max_weight != null ? "at most " + pct(lim.max_weight) : "", lim.max_weight != null && r.largest > lim.max_weight + 1e-9],
    ["Initial margin", pct(r.initial_margin), "of equity", false],
  ];
  const table = el("table"), head = el("tr");
  for (const name of ["Measure", "Now", "Limit"]) head.append(el("th", name));
  table.append(el("thead"), el("tbody"));
  table.tHead.append(head);
  for (const [name, now, limit, over] of rows) {
    const row = el("tr");
    row.append(el("td", name), el("td", now, over ? "over" : null), el("td", limit));
    table.tBodies[0].append(row);
  }
  box.replaceChildren(table);
}
function alarms(state, hb) {
  const box = $("alarms"), out = [];
  const held = Object.entries(hb.positions || {}).filter(([, q]) => q);
  if (state.gold_status && state.gold_status.position_oz) held.push(["XAUUSD (oz)", state.gold_status.position_oz]);
  const r = hb.risk;
  if (!state.engine_alive && held.length) {
    out.push(["red", "The engine is not running, and the broker may still hold positions: " + held.map(([t, q]) => `${t} ${shares.format(q)}`).join(", ") + ".",
      "Start paper trading to reconnect: the engine reconciles with the broker before anything else. Exit all positions works once it is running."]);
  }
  if (state.engine_alive && r && r.breach) {
    out.push(["red", "A limit is breached: " + r.breach + ".", "New positions are blocked; the engine trims the book back at its next decision."]);
  }
  if (state.engine_alive && r && !r.marks_current && held.length) {
    out.push(["amber", "Exposure unknown: prices for held stocks are missing or stale.", "No orders go out until every held stock has a current price."]);
  }
  box.replaceChildren(...out.map(([tone, text, note]) => {
    const div = el("div", text, "alarm " + tone);
    div.append(el("small", note));
    return div;
  }));
}
function approvals(requests) {
  const box = $("approvals");
  box.hidden = !requests.length;
  box.replaceChildren();
  if (!requests.length) return;
  box.append(el("h2", requests.length === 1 ? "An order is waiting for your approval" : requests.length + " orders are waiting for your approval"));
  for (const req of requests) {
    const o = req.order || {}, row = el("div", null, "proposal");
    const verb = o.side === "BUY" ? "Buy" : o.side === "SELL" ? "Sell" : o.side;
    row.append(el("strong", `${verb} ${shares.format(o.qty)} ${o.ticker}`));
    const price = Number.isFinite(o.decision_price) ? `Decided at ${usd.format(o.decision_price)}. ` : "";
    row.append(el("p", price + "Approval expires " + when(req.expires) + ".", "help"));
    const button = el("button", req.approved ? "Approved, waiting for checks" : "Approve this order", "primary");
    button.disabled = req.approved || busy;
    button.onclick = () => action(() => api("/api/approve", {request: req.id}));
    row.append(button);
    box.append(row);
  }
}
function render(state) {
  last = state;
  const hb = state.heartbeat || {};
  $("schedule").textContent = `History is checked at ${state.schedule.signal} and decisions are made at ${state.schedule.decision}, New York time.`;
  if (!initialized) {
    for (const [key, value] of Object.entries(state.settings)) if (form.elements[key]) form.elements[key].value = value;
    form.elements.gold.checked = state.settings.gold;
    const box = $("symbols");
    box.replaceChildren();
    for (const ticker of state.symbols) {
      const label = el("label"), input = el("input");
      input.type = "checkbox"; input.name = "symbol"; input.value = ticker;
      input.checked = state.settings.tickers.includes(ticker);
      label.append(input, el("span", ticker));
      box.append(label);
    }
    initialized = true;
  }
  riskNote();
  const gold = state.gold_status;
  $("gold-status").textContent = !state.settings.gold ? "London Gold is off."
    : !gold ? "London Gold: waiting for broker data"
    : gold.state === "gold bracket submitted"
      ? `London Gold: ${shares.format(gold.qty)} oz order, limit ${usd.format(gold.entry_limit)}, stop ${usd.format(gold.stop)}, target ${usd.format(gold.target)}${gold.minimum_lot_override ? " (minimum lot exceeds 1% budget)" : ""}.`
      : `London Gold: ${gold.state}${gold.position_oz ? ` (${shares.format(gold.position_oz)} oz)` : ""}.`;
  $("front").dataset.phase = state.phase;
  $("phase").textContent = phases[state.phase] || state.phase;
  $("status-title").textContent = titles[state.phase] || state.phase;
  $("status-message").textContent = state.message;
  $("data-status").textContent = state.engine_alive ? (state.data_issue || "History checked") : "Not connected";
  $("expires").textContent = timeLimit(state.authority);
  $("pending").textContent = hb.pending_orders == null ? "None" : String(hb.pending_orders);
  $("cash").textContent = Number.isFinite(hb.cash) ? usd.format(hb.cash) : "Not available";
  const r = hb.risk;
  const live = state.engine_alive && r && r.marks_current && r.gross != null;
  $("equity").textContent = live && Number.isFinite(r.equity) ? usd.format(r.equity) : "Not available";
  $("exposure").textContent = live ? `${times(r.gross)} gross, ${times(r.net)} net` : (state.engine_alive && r && !r.marks_current ? "Unknown" : "Not available");
  $("exposure").classList.toggle("warn", Boolean(state.engine_alive && r && (r.breach || !r.marks_current)));
  riskTable(state.engine_alive ? r : null);
  alarms(state, hb);
  $("snapshot-age").textContent = hb.at
    ? (state.snapshot_current ? "Live engine snapshot from " : "Older snapshot, not verified, from ") + new Date(hb.at).toLocaleString()
    : "No current account snapshot.";
  positions(hb, state.gold_status);
  $("log").textContent = state.log.join("\n") || "No activity yet.";
  const block = hb.hard_halt || hb.blocked;
  $("block").textContent = block || "";
  $("block").hidden = !block;
  for (const field of form.querySelectorAll("input,select,button")) field.disabled = state.engine_alive || busy;
  $("locked").hidden = !state.engine_alive;
  $("start").disabled = busy || ["running", "preparing"].includes(state.phase);
  $("pause").disabled = busy || !state.engine_alive;
  $("exit").disabled = busy || !state.engine_alive;
  approvals(state.requests);
}
async function action(fn) {
  if (busy) return;
  busy = true;
  notice("");
  try { render(await fn()); } catch (e) { notice(e.message); }
  finally { busy = false; try { render(await api("/api/status")); } catch (e) { notice(e.message); } }
}
$("offline-run").onclick = async () => {
  if (busy) return;
  busy = true;
  $("offline-run").disabled = true;
  notice("");
  try {
    const payload = {
      side: $("offline-side").value,
      quantity: $("offline-quantity").value,
      capital: $("offline-capital").value
    };
    const data = await api("/api/offline-demo", payload);
    const sim = data.result;
    $("offline-result").textContent = [
      "OFFLINE SIMULATION — SYNTHETIC FIXTURE — NO ORDERS ROUTED",
      "Instrument: " + sim.instrument,
      "Position: " + sim.position,
      "Final simulated equity: $" + sim.equity,
      "Fees paid: $" + sim.fees,
      "Synthetic fills: " + sim.fills,
      "Risk halt: " + (sim.hard_halt || "none"),
      "No strategy was evaluated and no market feed was qualified."
    ].join("\n");
  } catch (e) {
    notice(e.message);
  } finally {
    busy = false;
    $("offline-run").disabled = false;
  }
};
$("public-crypto-run").onclick = async () => {
  if (busy) return;
  busy = true;
  $("public-crypto-run").disabled = true;
  notice("");
  try {
    const data = await api("/api/public-crypto", {
      instrument: $("public-crypto-instrument").value,
      depth: $("public-crypto-depth").value
    });
    $("public-crypto-result").textContent = [
      "READ-ONLY EEA SNAPSHOTS — NO ORDERS ROUTED",
      "Instrument: " + data.instrument,
      "Best bid: " + data.book.best_bid + " | Best ask: " + data.book.best_ask,
      "Observed spread: " + (data.book.spread === null ? "not valid (crossed or locked)" : data.book.spread),
      "Book event UTC nanoseconds: " + data.book.exchange_ns,
      "Book received UTC nanoseconds: " + data.book.received_ns,
      "Indicative funding rate (fraction): " + data.funding.indicated_rate,
      "Funding observed UTC nanoseconds: " + data.funding.exchange_ns,
      "Funding received UTC nanoseconds: " + data.funding.received_ns,
      "WARNING: snapshots are independent, not a synchronized tradable feed.",
      "No position, funding settlement, strategy approval or external orders."
    ].join("\n");
  } catch (e) {
    notice(e.message);
  } finally {
    busy = false;
    $("public-crypto-run").disabled = false;
  }
};
form.addEventListener("submit", e => { e.preventDefault(); action(() => api("/api/setup", settings())); });
form.addEventListener("change", e => { if (e.target.name === "profile") riskNote(); });
$("start").onclick = () => action(async () => {
  if (!last.engine_alive) {
    if (!form.reportValidity()) throw Error("Complete the required setup fields.");
    await api("/api/setup", settings());
  }
  return api("/api/start", {});
});
$("pause").onclick = () => action(() => api("/api/pause", {}));
const exitDialog = $("exit-dialog");
$("exit").onclick = () => exitDialog.showModal();
$("exit-cancel").onclick = () => exitDialog.close();
$("exit-confirm").onclick = () => { exitDialog.close(); action(() => api("/api/exit", {confirm: true})); };
async function refresh() {
  if (busy) return;
  try { render(await api("/api/status")); }
  catch (e) { notice("Lost the connection to the local app. The engine may still be running: check the service before assuming trading stopped."); }
}
refresh();
setInterval(refresh, 2000);
