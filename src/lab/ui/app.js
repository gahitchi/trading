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
function positions(hb) {
  const box = $("positions");
  const held = Object.entries(hb.positions || {}).filter(([, qty]) => qty);
  if (!held.length) { box.replaceChildren(el("p", hb.positions ? "No open positions. The plan is in cash." : "No account snapshot yet.", "empty")); return; }
  const table = el("table"), head = el("tr");
  for (const name of ["Stock", "Side", "Shares"]) head.append(el("th", name));
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
  $("front").dataset.phase = state.phase;
  $("phase").textContent = phases[state.phase] || state.phase;
  $("status-title").textContent = titles[state.phase] || state.phase;
  $("status-message").textContent = state.message;
  $("data-status").textContent = state.engine_alive ? (state.data_issue || "History checked") : "Not connected";
  $("expires").textContent = timeLimit(state.authority);
  $("pending").textContent = hb.pending_orders == null ? "None" : String(hb.pending_orders);
  $("cash").textContent = Number.isFinite(hb.cash) ? usd.format(hb.cash) : "Not available";
  $("snapshot-age").textContent = hb.at
    ? (state.snapshot_current ? "Live engine snapshot from " : "Older snapshot, not verified, from ") + new Date(hb.at).toLocaleString()
    : "No current account snapshot.";
  positions(hb);
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
