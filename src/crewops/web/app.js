// CrewOps console. Each role gets its own pages; all data comes from /api (FalkorDB).
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const inr = (n) => (n == null ? "-" : "INR " + Number(n).toLocaleString("en-IN"));
const num = (n) => Number(n || 0).toLocaleString("en-IN");

let username = null;
let me = null;
let chatLog = [];
let sessionId = null;
let network = null;

// ---------- small helpers ----------

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(username ? { "X-CrewOps-User": username } : {}) },
    credentials: "same-origin",
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function store(key, value) {
  try { value == null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch { /* private mode */ }
}
function stored(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 3500);
}

const ICON = {
  home: '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5 5h14l3 7v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6z"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  plus: '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
  chat: '<path d="M21 12a8 8 0 0 1-11.6 7.1L3 21l1.9-6.4A8 8 0 1 1 21 12z"/>',
  net: '<circle cx="5" cy="12" r="2"/><circle cx="19" cy="5" r="2"/><circle cx="19" cy="19" r="2"/><path d="M7 11l10-5M7 13l10 5"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18"/>',
};
const icon = (name) => `<svg viewBox="0 0 24 24">${ICON[name]}</svg>`;

const STATUS = {
  REPORTED: "Reported", ANALYSING: "Analysing", OPTIONS_READY: "Needs plan", PLAN_SUBMITTED: "Awaiting approval",
  RESOLVED: "Resolved", CLOSED: "Closed", PARKED: "Needs attention", SUBMITTED: "Awaiting approval",
  COMMITTED: "Committed", REJECTED: "Rejected",
};
const badge = (s) => `<span class="badge ${esc(s)}">${esc(STATUS[s] || s)}</span>`;

const KINDS = {
  crew_unavailable: { title: "Crew unavailable", sub: "Sick or absent crew", fields: [
    ["crew_id", "Crew id", "C-1042"], ["from_date", "From", "2026-10-13"], ["to_date", "To (optional)", "2026-10-14"], ["reason", "Reason", "sick"]] },
  certification_lapse: { title: "Certificate lapse", sub: "Licence, medical or training expired", fields: [
    ["crew_id", "Crew id", "C-5417"], ["from_date", "Grounded from", "2026-10-16"], ["cert_type", "Certificate", "recurrent_training"]] },
  flight_delay: { title: "Flight delay", sub: "A late aircraft or flight", fields: [
    ["aircraft", "Aircraft", "VT-DXA"], ["date", "Date", "2026-10-14"], ["delay_hours", "Delay (hours)", "1.5"], ["flight_id", "or flight id", "DX401-2026-10-14"]] },
  station_closure: { title: "Station closure", sub: "Airport closed for a period", fields: [
    ["station", "Station", "BLR"], ["start", "Start (UTC)", "2026-10-15T08:00:00Z"], ["end", "End (UTC)", "2026-10-15T14:00:00Z"]] },
  gate_closure: { title: "Gate closure", sub: "A boarding gate out of use", fields: [
    ["gate", "Gate", "BLR-G1"], ["start", "Start (UTC)", "2026-10-13T00:00:00Z"], ["end", "End (UTC)", "2026-10-13T06:00:00Z"]] },
};
const kindTitle = (k) => (KINDS[k] ? KINDS[k].title : String(k).replaceAll("_", " "));

function fmtIso(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC" }) + " UTC";
}
const fmtTs = (ts) => (ts ? fmtIso(new Date(ts * 1000).toISOString()) : "-");
const initials = (name) => String(name || "?").split(/[\s.]+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");

// ---------- roles and navigation ----------

const ROLE_ORDER = ["controller", "approver", "reporter", "crew", "auditor", "super_admin", "admin"];
const ROLE_LABEL = { controller: "Controller", approver: "Approver", reporter: "Reporter", crew: "Crew member",
  auditor: "Auditor", admin: "Admin", super_admin: "Super admin" };
const primaryRole = (roles) => ROLE_ORDER.find((r) => roles.includes(r)) || roles[0];

const NAV = {
  controller: [["overview", "Overview", "home"], ["desk", "My desk", "inbox"], ["disruptions", "All disruptions", "list"],
    ["report", "Report", "plus"], ["assistant", "Assistant", "chat"], ["network", "Network", "net"]],
  approver: [["overview", "Overview", "home"], ["approvals", "Approvals", "check"], ["disruptions", "All disruptions", "list"],
    ["assistant", "Assistant", "chat"], ["network", "Network", "net"]],
  reporter: [["overview", "Overview", "home"], ["report", "Report a disruption", "plus"], ["mine", "My reports", "list"],
    ["assistant", "Assistant", "chat"]],
  crew: [["roster", "My roster", "calendar"], ["absence", "Report absence", "plus"], ["assistant", "Assistant", "chat"]],
  oversight: [["overview", "Overview", "home"], ["disruptions", "Audit trail", "list"], ["network", "Network", "net"],
    ["assistant", "Assistant", "chat"]],
};
const navFor = (role) => NAV[role] || NAV.oversight;

const PAGE_TITLE = {
  overview: ["Overview", "What is happening in the operation right now"],
  desk: ["My desk", "Disruptions assigned to your desk"],
  approvals: ["Approvals", "Plans waiting for a second person to approve"],
  disruptions: ["Disruptions", "Every disruption and its decision trail"],
  mine: ["My reports", "Disruptions you reported and where they are now"],
  report: ["Report a disruption", "The triage agent analyses it within a few seconds"],
  absence: ["Report absence", "Tell crew control you cannot fly"],
  roster: ["My roster", "Your trips, reserve days and notices"],
  assistant: ["Assistant", "Ask about crew, cover, legality, delays and decisions"],
  network: ["Network", "Graph algorithms over the route network, and the rules that exclude crew"],
};

// ---------- landing ----------

const GROUPS = [
  ["controller", "Controllers", "Plan cover for disruptions on their desk."],
  ["approver", "Approvers", "Approve or reject submitted plans."],
  ["reporter", "Reporters", "Log disruptions as they happen."],
  ["crew", "Crew", "See their roster and report absence."],
  ["oversight", "Oversight", "Auditor and admins: read-only view."],
];

async function showLanding() {
  username = null;
  me = null;
  $("#app").hidden = true;
  $("#landing").hidden = false;
  const [team, health] = await Promise.all([api("/api/team"), api("/api/health").catch(() => ({}))]);
  $("#landing-clock").textContent = fmtIso(health.clock);
  const groups = GROUPS.map(([key, title, sub]) => {
    const people = team.filter((u) => {
      const r = primaryRole(u.roles);
      return key === "oversight" ? ["auditor", "admin", "super_admin"].includes(r) : r === key;
    });
    if (!people.length) return "";
    return `<div class="role-group"><h3>${esc(title)}</h3><p>${esc(sub)}</p>
      ${people.map((u) => `<button class="person" data-user="${esc(u.username)}" type="button">
        <span class="avatar">${esc(initials(u.name))}</span>
        <span><span class="who">${esc(u.name)}</span><br><span class="what">${esc(u.desk ? u.desk : ROLE_LABEL[primaryRole(u.roles)])}</span></span>
      </button>`).join("")}</div>`;
  });
  $("#people").innerHTML = groups.join("");
  document.querySelectorAll(".person").forEach((b) => b.addEventListener("click", () => signIn(b.dataset.user)));
}

async function signIn(name) {
  await api("/api/login", { method: "POST", body: { username: name } });
  username = name;
  store("crewops.user", name);
  chatLog = [];
  sessionId = null;
  await loadMe();
  $("#landing").hidden = true;
  $("#app").hidden = false;
  const first = navFor(me.role)[0][0];
  const current = location.hash.split("/")[1];
  if (!current || !navFor(me.role).some((n) => n[0] === current)) location.hash = `#/${first}`;
  else route();
}

async function loadMe() {
  me = await api("/api/me");
  me.role = ["auditor", "admin", "super_admin"].includes(primaryRole(me.roles)) ? "oversight" : primaryRole(me.roles);
  $("#me-avatar").textContent = initials(me.name);
  $("#me-name").textContent = me.name;
  $("#me-role").textContent = me.roles.map((r) => ROLE_LABEL[r] || r).join(", ") + (me.desk ? ` - ${me.desk}` : "");
  const counts = {
    desk: (me.inbox.items.desk || []).length,
    approvals: (me.inbox.items.approvals || []).length,
  };
  $("#nav").innerHTML = navFor(me.role).map(([key, label, ic]) =>
    `<a href="#/${key}" data-key="${key}">${icon(ic)}<span>${esc(label)}</span>${counts[key] ? `<span class="count">${counts[key]}</span>` : ""}</a>`).join("");
  const s = await api("/api/summary").catch(() => null);
  if (s) $("#clock").textContent = fmtIso(s.clock);
}

// ---------- router ----------

async function route() {
  if (!me) return;
  const [, page = navFor(me.role)[0][0], arg] = location.hash.split("/");
  const allowed = navFor(me.role).map((n) => n[0]);
  const target = allowed.includes(page) ? page : allowed[0];
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.key === target));
  const [title, sub] = PAGE_TITLE[target] || [target, ""];
  $("#page-title").textContent = title;
  $("#page-sub").textContent = sub;
  const page$ = $("#page");
  page$.innerHTML = `<div class="card empty">Loading...</div>`;
  try {
    await PAGES[target](page$, arg ? decodeURIComponent(arg) : null);
  } catch (e) {
    page$.innerHTML = `<div class="card empty error">${esc(e.message)}</div>`;
  }
}

// ---------- pages ----------

const PAGES = {
  async overview(el) {
    const [s, insights, all] = await Promise.all([api("/api/summary"), api("/api/insights"), api("/api/disruptions")]);
    const items = me.inbox.items;
    let waiting = "";
    if (me.role === "controller") waiting = listCard("Waiting on your desk", (items.desk || []).map(fromInbox), "desk");
    else if (me.role === "approver") waiting = listCard("Waiting for your approval", approvalsList(), "approvals");
    else if (me.role === "reporter") waiting = listCard("Your recent reports", (items.my_reports || []).map(fromInbox), "mine");
    el.innerHTML = `
      <div class="tiles">
        ${tile("Open disruptions", num(s.open), `${num(s.total)} reported in total`)}
        ${tile("Need a plan", num(s.needs_plan), "analysed, waiting for a controller")}
        ${tile("Awaiting approval", num(s.awaiting_approval), "submitted, waiting for an approver")}
        ${tile("Passengers affected", num(s.passengers), "on open disruptions")}
      </div>
      <div class="grid-cols">
        ${waiting}
        ${listCard("Latest disruptions", all.slice(0, 6), "disruptions")}
        <div class="card"><div class="card-head"><h3>Agents</h3><span class="muted small">coordinating through the graph</span></div>
          ${insights.agents.length ? `<table><thead><tr><th>Agent</th><th>Kind</th><th class="num">Claimed</th><th class="num">Handoffs</th></tr></thead><tbody>
          ${insights.agents.map((a) => `<tr><td>${esc(a.agent)}</td><td>${esc(a.kind)}</td><td class="num">${a.claimed}</td><td class="num">${a.handoffs}</td></tr>`).join("")}
          </tbody></table>` : `<div class="empty">No agent has run yet.</div>`}
        </div>
      </div>`;
    bindRows(el);
  },

  desk: (el, id) => listDetail(el, id, "desk", () => (me.inbox.items.desk || []).map(fromInbox), "Nothing on your desk right now."),
  approvals: (el, id) => listDetail(el, id, "approvals", approvalsList, "No plans are waiting for approval."),
  mine: (el, id) => listDetail(el, id, "mine", () => (me.inbox.items.my_reports || []).map(fromInbox), "You have not reported anything yet."),
  disruptions: (el, id) => listDetail(el, id, "disruptions", () => api("/api/disruptions"), "No disruptions yet."),

  async report(el) { renderReport(el, Object.keys(KINDS)); },
  async absence(el) { renderReport(el, ["crew_unavailable"], me.crew_id); },

  async roster(el) {
    const out = await api("/api/tools/crew_profile", { method: "POST", body: { crew: me.crew_id } });
    if (!out.ok) throw new Error(out.result.error);
    const p = out.result;
    const crew = me.inbox.items.crew || { notices: [], new_trips: [] };
    el.innerHTML = `
      <div class="tiles">
        ${tile("Rank", esc(p.rank), esc(p.airline))}
        ${tile("Base", esc(p.base), `rated on ${esc(p.ratings.join(", "))}`)}
        ${tile("Trips", num(new Set(p.duties.map((d) => d.pairing)).size), `${p.duties.length} duty days`)}
        ${tile("Reserve days", num(p.on_call.length), "status: " + esc(p.status))}
      </div>
      <div class="grid-cols">
        <div class="card"><div class="card-head"><h3>Rostered duties</h3></div>
          ${p.duties.length ? `<table><thead><tr><th>Date</th><th>Pairing</th><th>Role</th></tr></thead><tbody>
          ${p.duties.map((d) => `<tr><td>${esc(d.date)}</td><td>${esc(d.pairing)}</td><td>${esc(d.role)}</td></tr>`).join("")}</tbody></table>`
          : `<div class="empty">No rostered duties.</div>`}
        </div>
        <div class="card"><div class="card-head"><h3>Notices</h3></div><div class="card-body">
          ${p.grounded_by.map((g) => `<div class="notice">You are marked unavailable in <b>${esc(g)}</b>. Crew control is arranging cover for your trips.</div>`).join("")}
          ${crew.new_trips.map((t) => `<div class="notice">New trip: <b>${esc(t.pairing)}</b> as ${esc(t.role)} (plan ${esc(t.plan)})</div>`).join("")}
          ${crew.notices.map((n) => `<div class="notice">${esc(n.disruption)}: ${esc(n.summary)}</div>`).join("")}
          ${!crew.new_trips.length && !crew.notices.length && !p.grounded_by.length ? `<p class="muted">No notices. Your roster is unchanged.</p>` : ""}
          <div class="actions"><a class="btn secondary" href="#/absence">Report absence</a></div>
        </div></div>
      </div>`;
  },

  async assistant(el) {
    const memory = await api("/api/memory");
    el.innerHTML = `
      <div class="chat-wrap">
        <div class="card">
          <div id="chat" class="chat">${chatLog.length ? chatLog.join("") : `<div class="empty">Ask a question, or pick one below.</div>`}</div>
          <div class="suggest">${suggestionsFor(me.role).map((q) => `<button type="button">${esc(q)}</button>`).join("")}</div>
          <form id="ask" class="ask"><input id="q" autocomplete="off" placeholder="Ask CrewOps..."><button class="btn" type="submit">Ask</button></form>
        </div>
        <div class="card"><div class="card-head"><h3>Your memory</h3></div><div class="card-body small" style="display:grid;gap:8px">
          <p>Stored in its own graph: <code>${esc(memory.graph)}</code></p>
          <p>${memory.stats.sessions} sessions, ${memory.stats.turns} questions, ${memory.stats.entities} ids remembered</p>
          <p>Preferences: ${Object.entries(memory.facts).map(([k, v]) => `<span class="chip">${esc(k)} = ${esc(v)}</span>`).join(" ") || "none yet"}</p>
          <p>Shared playbook: ${memory.playbook.map((p) => `${esc(p.role)}: ${esc(p.strategy.replaceAll("_", " "))} (${p.times})`).join(", ") || "nothing learned yet"}</p>
          <p class="muted">${memory.all_memory_graphs.length} separate memory graphs in this FalkorDB.</p>
        </div></div>
      </div>`;
    const chat = $("#chat");
    chat.scrollTop = chat.scrollHeight;
    $("#ask").addEventListener("submit", (e) => { e.preventDefault(); const q = $("#q").value; $("#q").value = ""; ask(q); });
    el.querySelectorAll(".suggest button").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));
  },

  async network(el) {
    const i = await api("/api/insights");
    const maxMov = Math.max(...i.stations.map((s) => s.movements), 1);
    const maxEx = Math.max(...i.rule_pressure.map((r) => r.exclusions), 1);
    el.innerHTML = `
      <div class="grid-cols">
        <div class="card"><div class="card-head"><h3>Station criticality</h3><span class="muted small">PageRank and betweenness on ROUTE edges</span></div>
          <table><thead><tr><th>Station</th><th class="num">Betweenness</th><th class="num">PageRank</th><th class="num">Movements</th><th class="bar-cell"></th></tr></thead><tbody>
          ${i.stations.map((s) => `<tr><td><b>${esc(s.station)}</b></td><td class="num">${s.betweenness}</td><td class="num">${s.pagerank}</td><td class="num">${s.movements}</td>
            <td class="bar-cell"><div class="bar" style="width:${(100 * s.movements) / maxMov}%"></div></td></tr>`).join("")}
          </tbody></table></div>
        <div class="card"><div class="card-head"><h3>Rules ruling out the most crew</h3></div>
          ${i.rule_pressure.length ? `<table><thead><tr><th>Rule</th><th class="num">Excluded</th><th class="bar-cell"></th></tr></thead><tbody>
          ${i.rule_pressure.map((r) => `<tr><td><b>${esc(r.rule)}</b><div class="muted small">${esc(r.text)}</div></td><td class="num">${r.exclusions}</td>
            <td class="bar-cell"><div class="bar" style="width:${(100 * r.exclusions) / maxEx}%"></div></td></tr>`).join("")}
          </tbody></table>` : `<div class="empty">No exclusions yet. Report a disruption to see which rules apply.</div>`}
        </div>
      </div>`;
  },
};

// ---------- shared pieces ----------

const tile = (label, value, sub) => `<div class="card tile"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
const fromInbox = (d) => ({ id: d.disruption, status: d.status, summary: d.summary });
const approvalsList = () => (me.inbox.items.approvals || []).map((a) => ({
  id: a.disruption, status: "PLAN_SUBMITTED", summary: `${a.plan} from ${a.submitted_by} - ${inr(a.cost)}` }));

function rowHtml(d, page, active) {
  return `<li class="row ${d.id === active ? "active" : ""}" data-go="#/${page}/${encodeURIComponent(d.id)}">
    <div class="row-top"><span class="row-id">${esc(d.id)}</span>${badge(d.status)}</div>
    <div>${esc(d.summary)}</div>
    ${d.type ? `<div class="row-sub">${esc(kindTitle(d.type))}${d.desk ? " - " + esc(d.desk) : ""}</div>` : ""}
  </li>`;
}

function listCard(title, items, page) {
  return `<div class="card"><div class="card-head"><h3>${esc(title)}</h3><a class="link" href="#/${page}">View all</a></div>
    ${items.length ? `<ul class="rows">${items.slice(0, 6).map((d) => rowHtml(d, page)).join("")}</ul>` : `<div class="empty">Nothing here.</div>`}</div>`;
}

function bindRows(root) {
  root.querySelectorAll("[data-go]").forEach((r) => r.addEventListener("click", () => { location.hash = r.dataset.go; }));
}

async function listDetail(el, id, page, source, emptyText) {
  const items = await source();
  const active = id || (items[0] && items[0].id);
  el.innerHTML = `<div class="grid-2">
    <div class="card"><div class="card-head"><h3>${items.length} item${items.length === 1 ? "" : "s"}</h3><button class="link" id="reload" type="button">Refresh</button></div>
      ${items.length ? `<ul class="rows">${items.map((d) => rowHtml(d, page, active)).join("")}</ul>` : `<div class="empty">${esc(emptyText)}</div>`}</div>
    <div id="detail">${active ? "" : `<div class="card empty">Select an item on the left.</div>`}</div>
  </div>`;
  bindRows(el);
  $("#reload").addEventListener("click", async () => { await loadMe(); route(); });
  if (active) await renderDetail($("#detail"), active);
}

async function renderDetail(box, id) {
  box.innerHTML = `<div class="card empty">Loading ${esc(id)}...</div>`;
  const d = await api(`/api/disruptions/${encodeURIComponent(id)}`);
  const a = d.analysis || {};
  const plans = d.trail.plans || [];
  const pending = plans.find((p) => p.status === "SUBMITTED");
  const isController = me.roles.includes("controller");
  const canSubmit = isController && d.status === "OPTIONS_READY" && (!d.desk || d.desk === me.desk);
  const canApprove = me.roles.includes("approver") && pending && pending.submitted_by !== me.name;
  let note = "";
  if (isController && d.status === "OPTIONS_READY" && d.desk && d.desk !== me.desk) note = `This disruption belongs to ${esc(d.desk)}. Only that desk can submit a plan.`;
  if (me.roles.includes("approver") && pending && pending.submitted_by === me.name) note = "You submitted this plan, so a different approver must approve it.";
  if (d.status === "REPORTED" || d.status === "ANALYSING") note = "The triage agent is analysing this disruption. Refresh in a few seconds.";

  let body = "";
  for (const g of a.gaps || []) {
    const opts = g.options.slice(0, 6);
    body += `<div class="gap-card">
      <div class="gap-title"><div><b>${esc(g.role)} seat on ${esc(g.pairing_id)}</b>
        <div class="muted small">Vacated by ${esc(g.vacated_by)} - ${g.uncovered_flights.length} flights - ${num(g.passengers)} passengers</div></div>
      </div>
      <div class="funnel">${g.funnel.map((s) => `<span class="chip">${esc(s.stage)} <b>${s.remaining}</b>${s.dropped ? ` <span class="muted">(-${s.dropped})</span>` : ""}</span>`).join("")}</div>
      <table><thead><tr><th>#</th><th>Option</th><th class="num">Cost</th><th class="num">Delay</th></tr></thead><tbody>
      ${opts.map((o, i) => `<tr class="${i === 0 && o.crew_id ? "best" : ""}"><td>${o.rank}</td><td>${esc(o.action)}${i === 0 && o.crew_id ? `<span class="tag-best">Recommended</span>` : ""}
        ${o.crew_id && i > 0 ? `<button class="link why" data-crew="${esc(o.crew_id)}" type="button">why not first?</button>` : ""}</td>
        <td class="num">${inr(o.cost_inr)}</td><td class="num">${o.delay_hours ? o.delay_hours + "h" : "-"}</td></tr>`).join("")}
      </tbody></table></div>`;
  }
  if (a.joint_plan) body += `<div class="notice"><b>Joint plan:</b> ${a.joint_plan.assignments.map((x) => esc(x.crew_id)).join(" + ")} for ${inr(a.joint_plan.total_cost_inr)} (nobody gets two seats).</div>`;
  if (a.delay) body += `<div class="gap-card"><div class="gap-title"><b>Delay along the aircraft rotation</b></div>
    <table><thead><tr><th>Flight</th><th class="num">Delay</th><th>New departure</th></tr></thead><tbody>
    ${a.delay.legs.map((l) => `<tr><td>${esc(l.flight_id)}</td><td class="num">${l.delay_hours}h</td><td>${fmtIso(l.new_dep)}</td></tr>`).join("")}</tbody></table></div>`;
  if (a.flights) body += `<div class="gap-card"><div class="gap-title"><b>Flights in the closure</b></div>
    <table><thead><tr><th>Flight</th><th class="num">Min delay</th><th class="num">Crew FDP</th><th>Action</th></tr></thead><tbody>
    ${a.flights.map((f) => `<tr><td>${esc(f.flight_id)}</td><td class="num">${f.min_delay_hours}h</td><td class="num">${f.crew_fdp_after_delay ?? "-"} / ${f.fdp_limit ?? "-"}</td><td class="small">${esc(f.action)}</td></tr>`).join("")}</tbody></table></div>`;
  if (a.gate_moves) body += `<div class="gap-card"><div class="gap-title"><b>Gate moves</b></div>
    <table><thead><tr><th>Flight</th><th>Move to</th><th>Also free</th></tr></thead><tbody>
    ${a.gate_moves.map((m) => `<tr><td>${esc(m.flight_id)}</td><td><b>${esc(m.to_gate || "remote stand")}</b></td><td class="small">${esc(m.free_gates.slice(1).join(", ") || "-")}</td></tr>`).join("")}</tbody></table></div>`;

  const plansHtml = plans.map((p) => `<div style="margin-bottom:12px"><div class="row-top"><b>${esc(p.plan)}</b>${badge(p.status)}</div>
    <div class="small">${inr(p.total_cost)} - submitted by ${esc(p.submitted_by)}${p.approved_by ? `, approved by ${esc(p.approved_by)}` : ""}${p.rejected_by ? `, rejected by ${esc(p.rejected_by)}: ${esc(p.reject_reason)}` : ""}</div>
    <div class="muted small mono">fingerprint ${esc((p.fingerprint || "").slice(0, 16))}${p.seal ? ` - seal ${esc(p.seal.slice(0, 16))}` : ""}</div></div>`).join("");

  box.innerHTML = `
    <div class="card"><div class="card-body">
      <div class="detail-head"><div><h3>${esc(d.id)} - ${esc(kindTitle(d.type))}</h3>
        <div class="muted small">Reported by ${esc(d.trail.reported_by)}${d.desk ? ` - ${esc(d.desk)} (${esc(d.trail.controller || "")})` : ""}</div></div>${badge(d.status)}</div>
      <p class="headline">${esc(a.headline || d.summary)}</p>
      ${body}
      ${note ? `<div class="notice">${note}</div>` : ""}
      <div class="actions">
        ${canSubmit ? `<button class="btn" id="submit-plan" type="button">${icon("check")} Submit plan for approval</button><button class="btn secondary" id="close-d" type="button">Close without a plan</button>` : ""}
        ${canApprove ? `<button class="btn" id="approve-plan" type="button">${icon("check")} Approve ${esc(pending.plan)}</button><button class="btn danger" id="reject-plan" type="button">Reject</button>` : ""}
      </div>
      <p id="action-msg" class="small error"></p>
    </div></div>
    <div class="grid-cols" style="margin-top:18px">
      <div class="card"><div class="card-head"><h3>Decision trail</h3></div><div class="card-body">
        ${plansHtml}
        <ul class="timeline">${d.trail.events.map((e) => `<li><div><b>${esc(e.kind.replaceAll("_", " "))}</b> by ${esc(e.actor)}</div><div class="small">${esc(e.note)}</div><div class="when">${fmtTs(e.ts)}</div></li>`).join("")}</ul>
      </div></div>
      <div class="card"><div class="card-head"><h3>In the graph</h3><span class="muted small">read from FalkorDB</span></div><div class="card-body">
        <div id="graph"></div>
        <div class="legend"><span><i style="background:var(--ink)"></i>Disruption</span><span><i style="background:var(--series-1)"></i>Crew</span>
          <span><i style="background:var(--series-2)"></i>Flight</span><span><i style="background:var(--series-3)"></i>Seat gap / plan</span><span><i style="background:var(--neutral-node)"></i>People, desk, pairing</span></div>
      </div></div>
    </div>`;

  box.querySelectorAll(".why").forEach((b) => b.addEventListener("click", async () => {
    const r = await api(`/api/disruptions/${encodeURIComponent(id)}/why-not/${encodeURIComponent(b.dataset.crew)}`);
    const o = (r.options || [])[0];
    b.outerHTML = o ? `<div class="muted small">Legal, ranked ${o.rank}. Cheaper: ${o.ranked_above.map((x) => `${esc(x.crew)} (${inr(x.cost)})`).join(", ")}</div>` : "";
  }));
  act("#submit-plan", () => api(`/api/disruptions/${id}/plan`, { method: "POST", body: {} }), "Plan submitted. An approver will see it in their queue.");
  act("#close-d", () => api(`/api/disruptions/${id}/close`, { method: "POST", body: { note: "handled without a plan" } }), "Disruption closed.");
  act("#approve-plan", () => api(`/api/plans/${pending.plan}/approve`, { method: "POST" }), "Approved. The roster was updated in one graph write.");
  act("#reject-plan", () => api(`/api/plans/${pending.plan}/reject`, { method: "POST", body: { note: prompt("Reason for rejecting?") || "rejected" } }), "Plan rejected and sent back to the desk.");
  drawGraph(id);
}

function act(sel, fn, done) {
  const el = $(sel);
  if (!el) return;
  el.addEventListener("click", async () => {
    el.disabled = true;
    try {
      await fn();
      toast(done);
      await loadMe();
      route();
    } catch (e) {
      $("#action-msg").textContent = e.message;
      el.disabled = false;
    }
  });
}

const NODE_COLOR = { Disruption: "--ink", Crew: "--series-1", Flight: "--series-2", Gap: "--series-3", Plan: "--series-3" };

async function drawGraph(id) {
  const box = $("#graph");
  if (!box || !window.vis) return;
  const data = await api(`/api/disruptions/${encodeURIComponent(id)}/graph`);
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  const nodes = new vis.DataSet(data.nodes.map((n) => ({
    id: n.id, label: n.label, shape: "dot", size: n.group === "Disruption" ? 16 : 9,
    color: { background: v(NODE_COLOR[n.group] || "--neutral-node"), border: v("--surface") }, borderWidth: 2,
    font: { color: v("--ink-2"), size: 11, face: "Inter" },
    title: `${n.group}\n` + Object.entries(n.props).slice(0, 8).map(([k, val]) => `${k}: ${val}`).join("\n"),
  })));
  const edges = new vis.DataSet(data.edges.map((e) => ({
    from: e.from, to: e.to, label: e.label, arrows: { to: { scaleFactor: 0.5 } },
    font: { size: 9, color: v("--muted"), strokeWidth: 0, face: "Inter" },
    color: { color: v("--line-strong") }, width: 1,
  })));
  if (network) network.destroy();
  network = new vis.Network(box, { nodes, edges }, { physics: { stabilization: { iterations: 200 }, barnesHut: { springLength: 120 } }, interaction: { hover: true } });
}

// ---------- report ----------

function renderReport(el, kinds, fixedCrew = null) {
  let kind = kinds[0];
  const draw = () => {
    const spec = KINDS[kind];
    el.innerHTML = `<div class="card"><div class="card-body">
      ${kinds.length > 1 ? `<div class="kinds">${kinds.map((k) => `<button type="button" class="kind ${k === kind ? "active" : ""}" data-kind="${k}">
        <span class="k-title">${esc(KINDS[k].title)}</span><span class="k-sub">${esc(KINDS[k].sub)}</span></button>`).join("")}</div>` : ""}
      <form id="report-form">
        <div class="form-grid">
          ${spec.fields.filter(([name]) => !(fixedCrew && name === "crew_id")).map(([name, label, ph]) => `<label class="field">${esc(label)}
            <input name="${name}" placeholder="e.g. ${esc(ph)}"></label>`).join("")}
        </div>
        ${fixedCrew ? `<p class="muted small" style="margin-top:10px">Reporting for yourself (${esc(fixedCrew)}).</p>` : ""}
        <div class="actions"><button class="btn" type="submit">${icon("plus")} Report</button></div>
        <p id="report-msg" class="small error"></p>
      </form></div></div>
      <div class="card"><div class="card-body small muted">After you report, the triage agent claims the disruption, finds what it affects in the graph,
        ranks legal cover, and hands it to the least busy controller desk.</div></div>`;
    el.querySelectorAll(".kind").forEach((b) => b.addEventListener("click", () => { kind = b.dataset.kind; draw(); }));
    $("#report-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const details = {};
      new FormData(e.target).forEach((val, k) => { if (val) details[k] = k === "delay_hours" ? Number(val) : val; });
      if (fixedCrew) details.crew_id = fixedCrew;
      try {
        const r = await api("/api/disruptions", { method: "POST", body: { kind, details } });
        toast(`${r.id} reported. The triage agent is analysing it.`);
        await new Promise((ok) => setTimeout(ok, 2500));
        await loadMe();
        location.hash = me.role === "crew" ? "#/roster" : me.role === "reporter" ? `#/mine/${r.id}` : `#/disruptions/${r.id}`;
      } catch (err) {
        $("#report-msg").textContent = err.message;
      }
    });
  };
  draw();
}

// ---------- assistant ----------

function suggestionsFor(role) {
  const common = ["Which station is most critical?", "Best route from GOI to CCU?"];
  return {
    controller: ["Anything waiting for me?", "Who covers C-1042 on 13 Oct?", "Is she legal for it?", "Why not C-2087 for D-0001?",
      "VT-DXA is 90 minutes late on 14 Oct, delay impact?", "Who is on reserve at BLR tomorrow?", "I prefer least delay from now on"],
    approver: ["Anything waiting for me?", "Explain the decision on D-0001", "Why not C-2087 for D-0001?", ...common],
    reporter: ["Anything waiting for me?", "What is the status of D-0001?", "Who is C-1042?"],
    crew: ["Anything waiting for me?", `Who is ${me.crew_id}?`],
    oversight: ["Explain the decision on D-0001", "Which station is most critical?", "Who is on reserve at BLR tomorrow?"],
  }[role] || common;
}

async function ask(question) {
  if (!question.trim()) return;
  const chat = $("#chat");
  if (!chatLog.length) chat.innerHTML = "";
  const q = `<div class="bubble q">${esc(question)}</div>`;
  chatLog.push(q);
  chat.insertAdjacentHTML("beforeend", q + `<div class="bubble a muted" id="pending">Reading the graph...</div>`);
  chat.scrollTop = chat.scrollHeight;
  let html;
  try {
    const r = await api("/api/ask", { method: "POST", body: { question, session_id: sessionId } });
    sessionId = r.session_id;
    const cypher = r.evidence.map((e) => `// ${e.query} (${e.rows} rows, ${e.ms} ms)\n${e.cypher}`).join("\n\n");
    html = `<div class="bubble a">${esc(r.answer)}
      <div class="meta">${r.engine === "planner" ? "Planner" : esc(r.engine)} - ${esc(r.tool_calls.map((c) => c.tool).join(", ") || "no tools")}</div>
      ${r.evidence.length ? `<details><summary>${r.evidence.length} Cypher queries behind this answer</summary><pre>${esc(cypher)}</pre></details>` : ""}</div>`;
  } catch (e) {
    html = `<div class="bubble a error">${esc(e.message)}</div>`;
  }
  chatLog.push(html);
  const pending = $("#pending");
  if (pending) pending.outerHTML = html;
  chat.scrollTop = chat.scrollHeight;
}

// ---------- start ----------

$("#switch").addEventListener("click", () => {
  store("crewops.user", null);
  history.replaceState(null, "", location.pathname);
  showLanding();
});
window.addEventListener("hashchange", route);

(async () => {
  const wanted = new URLSearchParams(location.search).get("as") || stored("crewops.user");
  if (wanted) {
    try { await signIn(wanted); return; } catch { store("crewops.user", null); }
  }
  showLanding();
})();
