"use strict";
const token = document.querySelector('meta[name="app-token"]').content;
const form = document.querySelector("#setup");
let initialized = false, busy = false, last;
const notes = {
  calm: "Up to 30% gross exposure. Long-only. 8% drawdown stop.",
  cautious: "Up to 50% gross exposure. Long-only. 12% drawdown stop.",
  balanced: "Up to 100% gross exposure. Long-only. 18% drawdown stop.",
  dynamic: "Up to 135% gross exposure and 30% shorts. 25% drawdown stop.",
  aggressive: "Up to 180% gross exposure and 75% shorts. 30% drawdown stop."
};
function notice(message) {const el=document.querySelector("#notice");el.textContent=message;el.hidden=!message;}
async function api(path, body) {
  const res=await fetch(path,{method:body?"POST":"GET",headers:{"X-App-Token":token,...(body?{"Content-Type":"application/json"}:{})},body:body?JSON.stringify(body):undefined});
  const value=await res.json();if(!res.ok)throw Error(value.error||"Action failed.");return value;
}
function settings() {
  const s=Object.fromEntries(new FormData(form));for(const k of ["port","capital","hours"])s[k]=Number(s[k]);
  delete s.symbol;s.tickers=[...form.querySelectorAll('input[name="symbol"]:checked')].map(el=>el.value);return s;
}
function render(state) {
  last=state;
  document.querySelector("#decision-note").textContent="History: "+state.schedule.signal+" · Decisions: "+state.schedule.decision+" New York.";
  document.querySelector("#schedule").textContent=state.schedule.signal+"–"+state.schedule.decision+" New York";
  if(!initialized) {
    for(const [key,value] of Object.entries(state.settings))if(form.elements[key])form.elements[key].value=value;
    const box=document.querySelector("#symbols");box.replaceChildren();
    for(const ticker of state.symbols){const label=document.createElement("label"),input=document.createElement("input");input.type="checkbox";input.name="symbol";input.value=ticker;input.checked=state.settings.tickers.includes(ticker);label.append(input,document.createTextNode(ticker));box.append(label);}
    initialized=true;
  }
  document.querySelector("#risk-note").textContent=(notes[form.elements.profile.value]||"")+" Existing operator ceilings may reduce these limits.";
  document.querySelector("#phase").textContent=state.phase.toUpperCase();
  document.querySelector("#status-title").textContent=({setup:"Your plan starts here.",stopped:"Ready for your next session.",preparing:"Preparing your market data.",running:"Your plan is active.",paused:"New orders are paused.",blocked:"Your attention is needed."})[state.phase]||state.phase;
  document.querySelector("#status-message").textContent=state.message;
  document.querySelector("#data-status").textContent=state.engine_alive?(state.data_issue||"History checked for this session"):"Not connected";
  document.querySelector("#expires").textContent=state.authority?new Date(state.authority.expires*1000).toLocaleString():"Not issued";
  document.querySelector("#pending").textContent=state.heartbeat.pending_orders??"—";
  document.querySelector("#cash").textContent=Number.isFinite(state.heartbeat.cash)?state.heartbeat.cash.toLocaleString(undefined,{minimumFractionDigits:2}):"—";
  document.querySelector("#snapshot-age").textContent=(state.snapshot_current?"Engine snapshot · ":"Historical / unverified snapshot · ")+(state.heartbeat.at?new Date(state.heartbeat.at).toLocaleString():"not available");
  document.querySelector("#positions").textContent=state.heartbeat.positions?JSON.stringify(state.heartbeat.positions,null,2):"No current account snapshot.";
  document.querySelector("#log").textContent=state.log.join("\n")||"No activity yet.";
  const block=state.heartbeat.hard_halt||state.heartbeat.blocked;document.querySelector("#block").textContent=block||"";document.querySelector("#block").hidden=!block;
  for(const field of form.querySelectorAll("input,select,button"))field.disabled=state.engine_alive||busy;
  document.querySelector("#locked").hidden=!state.engine_alive;
  document.querySelector("#start").disabled=busy||["running","preparing"].includes(state.phase);
  document.querySelector("#pause").disabled=busy||!state.engine_alive;
  document.querySelector("#exit").disabled=busy||!state.engine_alive;
  const proposals=document.querySelector("#approvals");proposals.replaceChildren();
  if(state.requests.length){const title=document.createElement("h3");title.textContent="Orders awaiting your approval";proposals.append(title);}
  for(const req of state.requests){const row=document.createElement("div");row.className="proposal";const text=document.createElement("pre");text.textContent=JSON.stringify(req.order,null,2)+"\nExpires: "+new Date(req.expires*1000).toLocaleString();const button=document.createElement("button");button.textContent=req.approved?"Approved · awaiting checks":"Approve this exact order";button.disabled=req.approved||busy;button.className="secondary";button.onclick=()=>action(()=>api("/api/approve",{request:req.id}));row.append(text,button);proposals.append(row);}
}
async function action(fn){if(busy)return;busy=true;notice("");try{render(await fn());}catch(e){notice(e.message);}finally{busy=false;try{render(await api("/api/status"));}catch(e){notice(e.message);}}}
form.addEventListener("submit",e=>{e.preventDefault();action(()=>api("/api/setup",settings()));});
form.elements.profile.addEventListener("change",()=>{document.querySelector("#risk-note").textContent=notes[form.elements.profile.value];});
document.querySelector("#start").onclick=()=>action(async()=>{if(!last.engine_alive){if(!form.reportValidity())throw Error("Complete the required setup fields.");await api("/api/setup",settings());}return api("/api/start",{});});
document.querySelector("#pause").onclick=()=>action(()=>api("/api/pause",{}));
document.querySelector("#exit").onclick=()=>{if(confirm("Request liquidation of every position managed by this plan? Orders may remain pending until the market and broker allow execution."))action(()=>api("/api/exit",{confirm:true}));};
async function refresh(){if(!busy){try{render(await api("/api/status"));}catch(e){notice("Connection to the local app lost. The engine may still be active; check the service before assuming trading stopped.");}}}
refresh();setInterval(refresh,2000);
