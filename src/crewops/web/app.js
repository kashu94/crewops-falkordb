// CrewOps console. Everything on screen is read from the FalkorDB graph through /api.
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const inr = (n) => (n == null ? "" : "INR " + Number(n).toLocaleString("en-IN"));

let me = null;
let current = null;
let sessionId = null;
let network = null;

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

const FIELDS = {
  crew_unavailable: [["crew_id", "Crew id, e.g. C-1042"], ["from_date", "From 2026-10-13"], ["to_date", "To (optional)"], ["reason", "Reason"]],
  certification_lapse: [["crew_id", "Crew id, e.g. C-5417"], ["from_date", "Grounded from 2026-10-16"], ["cert_type", "Certificate"]],
  flight_delay: [["flight_id", "Flight, e.g. DX401-2026-10-14"], ["delay_hours", "Hours late"], ["aircraft", "or aircraft VT-DXA"], ["date", "with date"]],
  station_closure: [["station", "Station, e.g. BLR"], ["start", "2026-10-15T08:00:00Z"], ["end", "2026-10-15T14:00:00Z"]],
  gate_closure: [["gate", "Gate, e.g. BLR-G1"], ["start", "2026-10-13T00:00:00Z"], ["end", "2026-10-13T06:00:00Z"]],
};

const SUGGESTIONS = [
  "Anything waiting for me?",
  "Who covers C-1042 on 13 Oct?",
  "Is she legal for it?",
  "Why not C-2087 for D-0001?",
  "VT-DXA is 90 minutes late on 14 Oct, delay impact?",
  "Who is on reserve at BLR tomorrow?",
  "Which station is most critical?",
  "I prefer least delay from now on",
];

function renderFields() {
  $("#fields").innerHTML = FIELDS[$("#kind").value]
    .map(([name, ph]) => `<input name="${name}" placeholder="${esc(ph)}">`)
    .join("");
}

async function loadTeam() {
  const team = await api("/api/team");
  $("#user").innerHTML = `<option value="">Choose...</option>` + team
    .map((u) => `<option value="${esc(u.username)}">${esc(u.name)} - ${esc(u.roles.join(", "))}${u.desk ? " (" + esc(u.desk) + ")" : ""}</option>`)
    .join("");
  const saved = new URLSearchParams(location.search).get("as") || localStorageGet("crewops.user");
  if (saved && team.some((u) => u.username === saved)) {
    $("#user").value = saved;
    await signIn(saved);
  }
}

function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }

async function signIn(username) {
  if (!username) return;
  await api("/api/login", { method: "POST", body: { username } });
  localStorageSet("crewops.user", username);
  sessionId = null;
  $("#chat").innerHTML = "";
  await refreshAll();
}

async function refreshAll() {
  me = await api("/api/me");
  $("#roles").innerHTML = me.roles.map((r) => `<span class="chip">${esc(r)}</span>`).join("") + (me.desk ? `<span class="chip">${esc(me.desk)}</span>` : "");
  renderInbox(me.inbox);
  await Promise.all([loadDisruptions(), loadMemory(), loadInsights()]);
  if (current) await openDisruption(current, false);
}

function renderInbox(box) {
  const parts = [];
  const items = box.items || {};
  if (items.desk) parts.push(section("Your desk", items.desk.map((d) => `<li data-id="${esc(d.disruption)}"><div class="top"><b>${esc(d.disruption)}</b><span class="status ${esc(d.status)}">${esc(d.status)}</span></div><div class="small">${esc(d.summary)}</div>${d.new_handoffs ? `<div class="small muted">${d.new_handoffs} new handoff from triage</div>` : ""}</li>`)));
  if (items.approvals) parts.push(section("To approve", items.approvals.map((a) => `<li data-id="${esc(a.disruption)}"><div class="top"><b>${esc(a.plan)}</b><span>${inr(a.cost)}</span></div><div class="small">${esc(a.disruption)} from ${esc(a.submitted_by)}</div></li>`)));
  if (items.my_reports) parts.push(section("Your reports", items.my_reports.map((d) => `<li data-id="${esc(d.disruption)}"><div class="top"><b>${esc(d.disruption)}</b><span class="status ${esc(d.status)}">${esc(d.status)}</span></div><div class="small">${esc(d.summary)}</div></li>`)));
  if (items.crew) {
    const c = items.crew;
    parts.push(`<p class="small">${c.new_trips.length ? "New trips: " + c.new_trips.map((t) => esc(t.pairing + " (" + t.plan + ")")).join(", ") : "No roster changes."}</p>`);
  }
  $("#inbox").innerHTML = parts.join("") || "Nothing is waiting for you.";
  $("#inbox").classList.toggle("muted", !parts.length);
  $("#inbox").querySelectorAll("li[data-id]").forEach((li) => li.addEventListener("click", () => openDisruption(li.dataset.id)));
}

function section(title, rows) {
  return rows.length ? `<p class="small muted">${esc(title)}</p><ul class="list">${rows.join("")}</ul>` : "";
}

async function loadDisruptions() {
  const rows = await api("/api/disruptions");
  $("#disruptions").innerHTML = rows.length ? rows.map((d) => `
    <li data-id="${esc(d.id)}" class="${d.id === current ? "active" : ""}">
      <div class="top"><b>${esc(d.id)}</b><span class="status ${esc(d.status)}">${esc(d.status)}</span></div>
      <div class="small">${esc(d.summary)}</div>
      <div class="small muted">${esc(d.type.replaceAll("_", " "))}${d.desk ? " - " + esc(d.desk) : ""}</div>
    </li>`).join("") : `<li class="muted">None yet. Report one above.</li>`;
  $("#disruptions").querySelectorAll("li[data-id]").forEach((li) => li.addEventListener("click", () => openDisruption(li.dataset.id)));
}

async function openDisruption(id, scroll = true) {
  current = id;
  document.querySelectorAll("#disruptions li").forEach((li) => li.classList.toggle("active", li.dataset.id === id));
  const d = await api(`/api/disruptions/${encodeURIComponent(id)}`);
  const a = d.analysis || {};
  const plans = d.trail.plans || [];
  const pending = plans.find((p) => p.status === "SUBMITTED");
  const canSubmit = me.roles.includes("controller") && d.status === "OPTIONS_READY" && (!d.desk || d.desk === me.desk);
  const canApprove = me.roles.includes("approver") && pending && pending.submitted_by !== me.name;

  let html = `<div class="top" style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
      <h2 style="margin:0">${esc(d.id)} - ${esc(d.type.replaceAll("_", " "))}</h2>
      <span class="status ${esc(d.status)}">${esc(d.status)}</span></div>
    <p class="small muted">Reported by ${esc(d.trail.reported_by)}${d.desk ? ", desk " + esc(d.desk) + " (" + esc(d.trail.controller || "") + ")" : ""}</p>
    <p class="headline">${esc(a.headline || d.summary)}</p>`;

  for (const g of a.gaps || []) {
    html += `<div class="gap"><b>${esc(g.role)} seat on ${esc(g.pairing_id)}</b>
      <span class="small muted"> vacated by ${esc(g.vacated_by)}, ${g.uncovered_flights.length} flights, ${g.passengers} passengers</span>
      <div class="funnel">${g.funnel.map((s) => `<span class="chip">${esc(s.stage)}: ${s.remaining}${s.dropped ? " (-" + s.dropped + ")" : ""}</span>`).join("")}</div>
      <table><thead><tr><th>#</th><th>Option</th><th class="num">Cost</th><th class="num">Delay</th></tr></thead><tbody>
      ${g.options.slice(0, 6).map((o) => `<tr><td>${o.rank}</td><td>${esc(o.action)}${o.crew_id ? ` <button class="link why" data-crew="${esc(o.crew_id)}" type="button">why this rank?</button>` : ""}</td><td class="num">${inr(o.cost_inr)}</td><td class="num">${o.delay_hours ? o.delay_hours + "h" : ""}</td></tr>`).join("")}
      </tbody></table></div>`;
  }
  if (a.joint_plan) html += `<p><b>Joint plan:</b> ${a.joint_plan.assignments.map((x) => esc(x.crew_id)).join(" + ")}, ${inr(a.joint_plan.total_cost_inr)}</p>`;
  if (a.delay) {
    html += `<table><thead><tr><th>Leg</th><th class="num">Delay</th><th>New departure</th></tr></thead><tbody>${a.delay.legs.map((l) => `<tr><td>${esc(l.flight_id)}</td><td class="num">${l.delay_hours}h</td><td>${esc(l.new_dep)}</td></tr>`).join("")}</tbody></table>`;
  }
  if (a.flights) {
    html += `<table><thead><tr><th>Flight</th><th class="num">Min delay</th><th class="num">FDP</th><th>Action</th></tr></thead><tbody>${a.flights.map((f) => `<tr><td>${esc(f.flight_id)}</td><td class="num">${f.min_delay_hours}h</td><td class="num">${f.crew_fdp_after_delay ?? ""}/${f.fdp_limit ?? ""}</td><td class="small">${esc(f.action)}</td></tr>`).join("")}</tbody></table>`;
  }
  if (a.gate_moves) {
    html += `<table><thead><tr><th>Flight</th><th>Move to</th><th>Also free</th></tr></thead><tbody>${a.gate_moves.map((m) => `<tr><td>${esc(m.flight_id)}</td><td>${esc(m.to_gate || "remote stand")}</td><td class="small">${esc(m.free_gates.slice(1).join(", "))}</td></tr>`).join("")}</tbody></table>`;
  }

  html += `<div class="actions">
    ${canSubmit ? `<button id="submit-plan" type="button">Submit plan for approval</button><button id="close-d" class="secondary" type="button">Close, no plan needed</button>` : ""}
    ${canApprove ? `<button id="approve-plan" type="button">Approve ${esc(pending.plan)} (${inr(pending.total_cost)})</button><button id="reject-plan" class="secondary" type="button">Reject</button>` : ""}
    ${d.status === "REPORTED" ? `<button id="run-triage" class="secondary" type="button">Run triage agent now</button>` : ""}
  </div><p id="action-msg" class="small"></p>`;

  html += `<h3>Decision trail</h3>`;
  for (const p of plans) {
    html += `<p class="small"><b>${esc(p.plan)}</b> <span class="status ${esc(p.status)}">${esc(p.status)}</span> ${inr(p.total_cost)} - submitted by ${esc(p.submitted_by)}${p.approved_by ? ", approved by " + esc(p.approved_by) : ""}${p.rejected_by ? ", rejected by " + esc(p.rejected_by) + ": " + esc(p.reject_reason) : ""}<br><span class="muted">fingerprint ${esc((p.fingerprint || "").slice(0, 16))}${p.seal ? " - seal " + esc(p.seal.slice(0, 16)) : ""}</span></p>`;
  }
  html += `<ol class="events small">${d.trail.events.map((e) => `<li><b>${esc(e.kind)}</b> by ${esc(e.actor)}: ${esc(e.note)}</li>`).join("")}</ol>`;
  $("#detail").innerHTML = html;
  $("#detail").classList.remove("muted");

  $("#detail").querySelectorAll(".why").forEach((b) => b.addEventListener("click", async () => {
    const r = await api(`/api/disruptions/${encodeURIComponent(id)}/why-not/${encodeURIComponent(b.dataset.crew)}`);
    const o = (r.options || [])[0];
    b.outerHTML = o ? `<span class="small muted">ranked ${o.rank}${o.ranked_above.length ? " behind " + o.ranked_above.map((x) => esc(x.crew) + " " + inr(x.cost)).join(", ") : ": cheapest legal"}</span>` : "";
  }));
  bind("#submit-plan", () => api(`/api/disruptions/${id}/plan`, { method: "POST", body: {} }));
  bind("#close-d", () => api(`/api/disruptions/${id}/close`, { method: "POST", body: { note: "handled without a plan" } }));
  bind("#approve-plan", () => api(`/api/plans/${pending.plan}/approve`, { method: "POST" }));
  bind("#reject-plan", () => api(`/api/plans/${pending.plan}/reject`, { method: "POST", body: { note: prompt("Reason?") || "rejected" } }));
  bind("#run-triage", () => api(`/api/triage`, { method: "POST" }));
  await drawGraph(id);
  if (scroll && window.innerWidth < 1100) $("#center").scrollIntoView({ behavior: "smooth" });
}

function bind(sel, action) {
  const el = $(sel);
  if (!el) return;
  el.addEventListener("click", async () => {
    el.disabled = true;
    try {
      await action();
      await refreshAll();
    } catch (e) {
      $("#action-msg").innerHTML = `<span class="error">${esc(e.message)}</span>`;
      el.disabled = false;
    }
  });
}

const GROUP_COLORS = {
  Disruption: "#cf222e", Gap: "#bf8700", Crew: "#1f6feb", Flight: "#8250df", User: "#1a7f37",
  Desk: "#0a7ea4", Plan: "#d4a72c", Pairing: "#57606a", Contact: "#6e7781", Station: "#953800",
  Gate: "#953800", Aircraft: "#57606a",
};

async function drawGraph(id) {
  const data = await api(`/api/disruptions/${encodeURIComponent(id)}/graph`);
  $("#graph-wrap").hidden = !data.nodes.length;
  if (!window.vis || !data.nodes.length) return;
  const ink = getComputedStyle(document.documentElement).getPropertyValue("--ink").trim();
  const nodes = new vis.DataSet(data.nodes.map((n) => ({
    id: n.id, label: n.label, group: n.group, shape: "dot", size: n.group === "Disruption" ? 18 : 10,
    color: GROUP_COLORS[n.group] || "#888", font: { color: ink, size: 11 },
    title: `${n.group}\n${Object.entries(n.props).slice(0, 8).map(([k, v]) => k + ": " + v).join("\n")}`,
  })));
  const edges = new vis.DataSet(data.edges.map((e) => ({ from: e.from, to: e.to, label: e.label, arrows: "to", font: { size: 9, color: ink, strokeWidth: 0 }, color: { opacity: 0.5 } })));
  if (network) network.destroy();
  network = new vis.Network($("#graph"), { nodes, edges }, { physics: { stabilization: { iterations: 150 } }, interaction: { hover: true } });
}

async function loadMemory() {
  const m = await api("/api/memory");
  const facts = Object.entries(m.facts).map(([k, v]) => `${esc(k)} = ${esc(v)}`).join(", ") || "none yet";
  const pb = m.playbook.map((p) => `${esc(p.role)}: ${esc(p.strategy)} x${p.times}`).join("; ") || "nothing learned yet";
  $("#memory").innerHTML = `<p>Your graph <code>${esc(m.graph)}</code>: ${m.stats.sessions} sessions, ${m.stats.turns} turns, ${m.stats.entities} entities.</p>
    <p>Preferences: ${facts}</p><p>Shared playbook: ${pb}</p><p>${m.all_memory_graphs.length} isolated memory graphs in this FalkorDB.</p>`;
}

async function loadInsights() {
  const i = await api("/api/insights");
  const st = i.stations.slice(0, 3).map((s) => `${esc(s.station)} (betweenness ${s.betweenness}, ${s.movements} movements)`).join(", ");
  const rules = i.rule_pressure.slice(0, 3).map((r) => `${esc(r.rule)}: ${r.exclusions}`).join(", ") || "no exclusions yet";
  const agents = i.agents.map((a) => `${esc(a.agent)}: ${a.claimed} claimed`).join(", ") || "no agent has run yet";
  $("#insights").innerHTML = `<p>Critical stations: ${st}</p><p>Rules ruling out most candidates: ${rules}</p><p>Agents: ${agents}</p>`;
}

async function ask(question) {
  if (!question.trim()) return;
  const chat = $("#chat");
  chat.insertAdjacentHTML("beforeend", `<div class="msg q">${esc(question)}</div>`);
  const pendingEl = document.createElement("div");
  pendingEl.className = "msg a muted";
  pendingEl.textContent = "Reading the graph...";
  chat.appendChild(pendingEl);
  chat.scrollTop = chat.scrollHeight;
  try {
    const r = await api("/api/ask", { method: "POST", body: { question, session_id: sessionId } });
    sessionId = r.session_id;
    const cypher = r.evidence.map((e) => `// ${e.query} (${e.rows} rows, ${e.ms} ms)\n${e.cypher}`).join("\n\n");
    pendingEl.className = "msg a";
    pendingEl.innerHTML = `${esc(r.answer)}<div class="meta">${esc(r.engine)} - tools: ${esc(r.tool_calls.map((c) => c.tool).join(", ") || "none")}</div>
      ${r.evidence.length ? `<details><summary class="small">${r.evidence.length} Cypher queries behind this answer</summary><pre>${esc(cypher)}</pre></details>` : ""}`;
    loadMemory();
  } catch (e) {
    pendingEl.innerHTML = `<span class="error">${esc(e.message)}</span>`;
  }
  chat.scrollTop = chat.scrollHeight;
}

$("#user").addEventListener("change", (e) => signIn(e.target.value));
$("#kind").addEventListener("change", renderFields);
$("#refresh").addEventListener("click", refreshAll);
$("#report-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const details = {};
  new FormData(e.target).forEach((v, k) => { if (v) details[k] = k === "delay_hours" ? Number(v) : v; });
  try {
    const r = await api("/api/disruptions", { method: "POST", body: { kind: $("#kind").value, details } });
    $("#report-msg").textContent = `${r.id} reported. The triage agent picks it up within a few seconds.`;
    current = r.id;
    setTimeout(refreshAll, 2500);
    await refreshAll();
  } catch (err) {
    $("#report-msg").innerHTML = `<span class="error">${esc(err.message)}</span>`;
  }
});
$("#ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#question").value;
  $("#question").value = "";
  ask(q);
});
$("#suggest").innerHTML = SUGGESTIONS.map((s) => `<button type="button" class="secondary">${esc(s)}</button>`).join("");
$("#suggest").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));

renderFields();
const openParam = new URLSearchParams(location.search).get("open");
if (openParam) current = openParam;
loadTeam().catch((e) => { $("#inbox").innerHTML = `<span class="error">${esc(e.message)}</span>`; });
