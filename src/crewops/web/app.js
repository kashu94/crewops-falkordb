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
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
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
    ["crew", "Crew schedules", "calendar"], ["report", "Report", "plus"], ["assistant", "Assistant", "chat"], ["network", "Network", "net"]],
  approver: [["overview", "Overview", "home"], ["approvals", "Approvals", "check"], ["disruptions", "All disruptions", "list"],
    ["crew", "Crew schedules", "calendar"], ["assistant", "Assistant", "chat"], ["network", "Network", "net"]],
  reporter: [["overview", "Overview", "home"], ["report", "Report a disruption", "plus"], ["mine", "My reports", "list"],
    ["crew", "Crew schedules", "calendar"], ["assistant", "Assistant", "chat"]],
  crew: [["roster", "My schedule", "calendar"], ["absence", "Report absence", "plus"], ["assistant", "Assistant", "chat"]],
  oversight: [["overview", "Overview", "home"], ["disruptions", "Audit trail", "list"], ["crew", "Crew schedules", "calendar"],
    ["network", "Network", "net"], ["assistant", "Assistant", "chat"]],
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
  roster: ["My schedule", "Your flights, duty times and reserve days (all times UTC)"],
  crew: ["Crew schedules", "Every crew member's week across all three airlines (times UTC)"],
  assistant: ["Assistant", "Ask about crew, cover, legality, delays and decisions"],
  network: ["Network", "Graph algorithms over the route network, and the rules that exclude crew"],
};

// ---------- landing ----------

const GROUPS = [
  ["reporter", "Reporters", "Log disruptions as they happen."],
  ["controller", "Controllers", "Plan cover for disruptions on their desk."],
  ["approver", "Approvers", "Approve or reject submitted plans."],
  ["crew", "Crew", "See their roster and report absence."],
  ["oversight", "Oversight", "Auditor and admins: read-only view."],
];

const ROLE_ICON = { controller: "inbox", approver: "check", reporter: "plus", crew: "calendar", oversight: "list" };

async function showLanding() {
  username = null;
  me = null;
  $("#app").hidden = true;
  $("#landing").hidden = false;
  const [team, health, departures] = await Promise.all([
    api("/api/team"), api("/api/health").catch(() => ({})), api("/api/board?limit=7").catch(() => []),
  ]);
  $("#landing-clock").textContent = fmtIso(health.clock);
  $("#board-clock").textContent = fmtIso(health.clock);
  $("#board-rows").innerHTML = departures.map((f, i) => {
    const status = f.status === "cancelled" ? ["cancelled", "Cancelled"] : f.delay_minutes ? ["late", `Delayed ${f.delay_minutes}m`] : ["ok", "On time"];
    return `<div class="board-row" style="animation-delay:${i * 90}ms"><span><i style="background:${esc(f.color)}"></i>${esc(f.flight)}</span>
      <span>${esc(f.dest)}</span><span>${esc(f.departs)}</span><span>${esc((f.gate || "-").split("-")[1] || "-")}</span>
      <span class="st ${status[0]}">${status[1]}</span></div>`;
  }).join("");
  $("#people").innerHTML = GROUPS.map(([key, title, sub]) => {
    const people = team.filter((u) => {
      const r = primaryRole(u.roles);
      return key === "oversight" ? ["auditor", "admin", "super_admin"].includes(r) : r === key;
    });
    if (!people.length) return "";
    return `<div class="role-row">
      <div class="role-icon">${icon(ROLE_ICON[key])}</div>
      <div class="role-text"><div class="role-title">${esc(title)}</div><div class="role-sub">${esc(sub)}</div></div>
      <select aria-label="${esc(title)}" data-role="${key}">
        ${people.map((u) => `<option value="${esc(u.username)}">${esc(u.name)}${u.desk ? ` (${esc(u.desk)})` : key === "oversight" ? ` (${esc(ROLE_LABEL[primaryRole(u.roles)])})` : ""}</option>`).join("")}
      </select>
      <button class="btn" type="button" data-enter="${key}">Enter</button>
    </div>`;
  }).join("");
  document.querySelectorAll("[data-enter]").forEach((b) => b.addEventListener("click", () => {
    signIn($(`select[data-role="${b.dataset.enter}"]`).value);
  }));
}

// Approximate positions on a map of India, 0..100 in each direction.
const STATION_XY = { DEL: [44, 14], BOM: [20, 52], GOI: [24, 66], BLR: [43, 76], COK: [37, 92], MAA: [57, 78], HYD: [46, 58], CCU: [82, 38] };

function routeMap(stations, animate = false) {
  const moves = Object.fromEntries((stations || []).map((s) => [s.station, s.movements]));
  const max = Math.max(...Object.values(moves).filter((v, i, a) => a.length), 1);
  const hub = STATION_XY.BLR;
  const spokes = Object.entries(STATION_XY).filter(([k]) => k !== "BLR");
  const arc = ([x1, y1], [x2, y2]) => {
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2, dx = x2 - x1, dy = y2 - y1;
    return `M${x1},${y1} Q${mx - dy * 0.18},${my + dx * 0.18} ${x2},${y2}`;
  };
  const paths = spokes.map(([k, xy], i) => {
    const w = stations ? 0.4 + (1.6 * (moves[k] || 0)) / max : 0.7;
    return `<path id="route-${k}" d="${arc(hub, xy)}" class="route" style="stroke-width:${w};animation-delay:${i * 0.25}s"/>`;
  }).join("");
  const dots = Object.entries(STATION_XY).map(([k, [x, y]]) => `<g class="station ${k === "BLR" ? "hub" : ""}">
      ${k === "BLR" ? `<circle cx="${x}" cy="${y}" r="5" class="pulse"/>` : ""}
      <circle cx="${x}" cy="${y}" r="${k === "BLR" ? 2.6 : 1.6}"/>
      <text x="${x + (x > 70 ? -3 : 3)}" y="${y - 2.5}" text-anchor="${x > 70 ? "end" : "start"}">${k}${stations && moves[k] ? ` ${moves[k]}` : ""}</text></g>`).join("");
  const planes = animate ? ["DEL", "CCU", "BOM", "MAA"].map((k, i) => `<g class="plane"><path d="M-2.2,-1.2 L2.4,0 L-2.2,1.2 L-1.2,0 Z"/>
      <animateMotion dur="${6 + i * 1.5}s" repeatCount="indefinite" rotate="auto" begin="${i * 1.2}s"><mpath href="#route-${k}"/></animateMotion></g>`).join("") : "";
  return `<svg viewBox="0 0 100 100" class="route-map" role="img" aria-label="Route network from the BLR hub">${paths}${dots}${planes}</svg>`;
}

async function signIn(name) {
  await api("/api/login", { method: "POST", body: { username: name } });
  username = name;
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
        ${tile("Open disruptions", num(s.open), `${num(s.total)} reported in total`, "list")}
        ${tile("Need a plan", num(s.needs_plan), "analysed, waiting for a controller", "inbox")}
        ${tile("Awaiting approval", num(s.awaiting_approval), "submitted, waiting for an approver", "check")}
        ${tile("Passengers affected", num(s.passengers), "on open disruptions", "user")}
      </div>
      <div class="grid-cols">
        ${waiting}
        ${listCard("Latest disruptions", all.slice(0, 6), "disruptions")}
        <div class="card"><div class="card-head"><h3>Route network</h3><span class="muted small">flights per station, from the graph</span></div>
          <div class="card-body map-card">${routeMap(insights.stations)}</div></div>
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

  async report(el) { await renderReport(el, Object.keys(KINDS)); },
  async absence(el) { await renderReport(el, ["crew_unavailable"], me.crew_id); },

  async roster(el) {
    const out = await api("/api/tools/crew_schedule", { method: "POST", body: { crew: me.crew_id } });
    if (!out.ok) throw new Error(out.result.error);
    const p = out.result;
    const crew = me.inbox.items.crew || { notices: [], new_trips: [] };
    const notices = [
      ...p.grounded_by.map((g) => `<div class="notice">You are marked unavailable in <b>${esc(g)}</b>. Crew control is arranging cover for your trips.</div>`),
      ...crew.new_trips.map((t) => `<div class="notice">New trip: <b>${esc(t.pairing)}</b> as ${esc(t.role)} (plan ${esc(t.plan)})</div>`),
      ...crew.notices.map((n) => `<div class="notice">${esc(n.disruption)}: ${esc(n.summary)}</div>`),
    ];
    el.innerHTML = scheduleHtml(p) + `
      <div class="card"><div class="card-head"><h3>Notices</h3><a class="btn secondary" href="#/absence">Report absence</a></div><div class="card-body">
        ${notices.join("") || `<p class="muted">No notices. Your schedule is unchanged.</p>`}
      </div></div>`;
  },

  async crew(el, id) {
    if (!id) return rosterBoard(el);
    el.innerHTML = `<div class="card"><div class="card-body">
        <form id="crew-search" class="ask" style="padding:0;border:0">
          <a class="btn secondary" href="#/crew">&larr; All crew</a>
          <input id="crew-q" autocomplete="off" placeholder="Crew id or name, e.g. C-1042 or Pillai" value="${esc(id || "")}">
          <button class="btn" type="submit">Show schedule</button>
        </form>
      </div></div><div id="crew-result"></div>`;
    $("#crew-search").addEventListener("submit", (e) => {
      e.preventDefault();
      const q = $("#crew-q").value.trim();
      if (q) location.hash = `#/crew/${encodeURIComponent(q)}`;
    });
    const box = $("#crew-result");
    box.innerHTML = `<div class="card empty">Loading...</div>`;
    const out = await api("/api/tools/crew_schedule", { method: "POST", body: { crew: id } });
    if (!out.ok) { box.innerHTML = `<div class="card empty error">${esc(out.result.error)}</div>`; return; }
    const r = out.result;
    if (r.matches) {
      box.innerHTML = r.matches.length ? `<div class="card"><div class="card-head"><h3>${r.matches.length} crew match "${esc(id)}"</h3></div><ul class="rows">
        ${r.matches.map((m) => `<li class="row" data-go="#/crew/${esc(m.crew_id)}"><div class="row-top"><span class="row-id">${esc(m.crew_id)}</span><span class="chip">${esc(m.rank)}</span></div><div>${esc(m.name)}</div></li>`).join("")}
        </ul></div>` : `<div class="card empty">No crew match "${esc(id)}".</div>`;
      bindRows(box);
      return;
    }
    box.innerHTML = `<div class="card"><div class="card-body"><h3 style="font-size:18px">${esc(r.name)} <span class="muted">(${esc(r.crew_id)})</span></h3>
      <p class="muted small">${esc(r.rank)} - ${esc(r.airline)} - based at ${esc(r.base)}${r.grounded_by.length ? ` - <b>unavailable</b> (${esc(r.grounded_by.join(", "))})` : ""}</p></div></div>` + scheduleHtml(r);
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

const tile = (label, value, sub, ic = null) => `<div class="card tile">${ic ? `<div class="tile-icon">${icon(ic)}</div>` : ""}<div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
let boardFilter = { airline: "", rank: "", q: "" };

async function rosterBoard(el) {
  const b = await api("/api/roster-board");
  const airlines = [...new Map(b.crew.map((c) => [c.airline, c.airline_name])).entries()];
  const ranks = ["Captain", "First Officer", "Senior Cabin Crew", "Cabin Crew"];
  const cell = (c, day) => {
    const x = c.days[day];
    if (!x) return `<td class="rb-off">-</td>`;
    if (x.unavailable) return `<td class="rb-cell rb-unavail" title="Unavailable (${esc(x.unavailable)})"><b>Unavailable</b><span>${esc(x.unavailable)}</span></td>`;
    if (x.kind === "reserve") return `<td class="rb-cell rb-reserve" title="On call ${esc(x.window)} UTC"><b>Reserve</b><span>${esc(x.window)}</span></td>`;
    return `<td class="rb-cell rb-duty" title="${esc(x.pairing)} as ${esc(x.role)}: report ${esc(x.report)}, release ${esc(x.release)} UTC, ${x.sectors} flights${x.via_plan ? ` (added by ${esc(x.via_plan)})` : ""}">
      <b>${esc(x.pairing)}</b><span>${esc(x.report)}-${esc(x.release)} &middot; ${x.sectors} fl</span></td>`;
  };
  const draw = () => {
    const f = boardFilter;
    const q = f.q.trim().toLowerCase();
    const rows = b.crew.filter((c) => (!f.airline || c.airline === f.airline) && (!f.rank || c.rank === f.rank)
      && (!q || c.crew_id.toLowerCase().includes(q) || c.name.toLowerCase().includes(q)));
    const flying = (day) => rows.filter((c) => c.days[day] && c.days[day].kind === "duty" && !c.days[day].unavailable).length;
    el.querySelector("#rb-table").innerHTML = `
      <thead><tr><th class="rb-name">Crew (${rows.length})</th>${b.dates.map((d) => `<th class="${d < b.today ? "rb-past" : d === b.today ? "rb-today" : ""}">${esc(fmtDay(d))}<div class="rb-count">${flying(d)} flying</div></th>`).join("")}</tr></thead>
      <tbody>${rows.map((c) => `<tr data-go="#/crew/${esc(c.crew_id)}">
        <td class="rb-name"><i style="background:${esc(c.color)}"></i><b>${esc(c.crew_id)}</b> ${esc(c.name)}<div class="muted small">${esc(c.rank)} &middot; ${esc(c.base)}${c.status !== "active" ? ` &middot; ${esc(c.status)}` : ""}</div></td>
        ${b.dates.map((d) => cell(c, d)).join("")}</tr>`).join("") || `<tr><td colspan="${b.dates.length + 1}" class="empty">No crew match these filters.</td></tr>`}</tbody>`;
    bindRows(el.querySelector("#rb-table"));
  };
  el.innerHTML = `<div class="card">
    <div class="card-head rb-filters">
      <div class="seg" role="tablist">${[["", "All airlines"], ...airlines].map(([code, name]) => `<button type="button" data-airline="${esc(code)}" class="${boardFilter.airline === code ? "on" : ""}">${esc(name)}</button>`).join("")}</div>
      <div class="rb-right">
        <select id="rb-rank" aria-label="Rank"><option value="">All ranks</option>${ranks.map((r) => `<option ${boardFilter.rank === r ? "selected" : ""}>${r}</option>`).join("")}</select>
        <input id="rb-q" placeholder="Search name or id" value="${esc(boardFilter.q)}" autocomplete="off">
      </div>
    </div>
    <div class="rb-legend small"><span><i class="rb-duty"></i>On duty (trip, report-release UTC, flights)</span><span><i class="rb-reserve"></i>Reserve (on-call window)</span>
      <span><i class="rb-unavail"></i>Unavailable</span><span>- Day off</span><span class="muted">Click a row for the full flight schedule.</span></div>
    <div class="rb-wrap"><table id="rb-table" class="rb"></table></div></div>`;
  el.querySelectorAll("[data-airline]").forEach((btn) => btn.addEventListener("click", () => {
    boardFilter.airline = btn.dataset.airline;
    el.querySelectorAll("[data-airline]").forEach((x) => x.classList.toggle("on", x === btn));
    draw();
  }));
  $("#rb-rank").addEventListener("change", (e) => { boardFilter.rank = e.target.value; draw(); });
  $("#rb-q").addEventListener("input", (e) => { boardFilter.q = e.target.value; draw(); });
  draw();
}

function scheduleHtml(p) {
  const trips = new Set(p.duty_days.map((d) => d.pairing)).size;
  const flights = p.duty_days.reduce((n, d) => n + d.flights.length, 0);
  const days = p.duty_days.map((d) => `
    <div class="gap-card">
      <div class="gap-title">
        <div><b>${esc(fmtDay(d.date))}</b> <span class="muted">- ${esc(d.pairing)} as ${esc(d.role)}</span>
          ${d.via_plan ? `<span class="chip">added by ${esc(d.via_plan)}</span>` : ""}</div>
        <div class="small">Report <b>${esc(d.report)}</b> - Release <b>${esc(d.release)}</b> <span class="muted">(${d.duty_hours}h duty)</span></div>
      </div>
      <table><thead><tr><th>Flight</th><th>Route</th><th>Departs</th><th>Arrives</th><th>Aircraft</th><th>Gate</th><th>Status</th></tr></thead><tbody>
      ${d.flights.map((f) => `<tr><td><b>${esc(f.flight)}</b></td><td>${esc(f.from)} &rarr; ${esc(f.to)}</td><td>${esc(f.departs)}</td><td>${esc(f.arrives)}</td>
        <td>${esc(f.aircraft)}</td><td>${esc(f.gate || "-")}</td><td>${f.status === "cancelled" ? `<span class="badge REJECTED">Cancelled</span>`
          : f.delay_minutes ? `<span class="badge OPTIONS_READY">Delayed ${Math.round(f.delay_minutes / 6) / 10}h</span>` : `<span class="muted">Scheduled</span>`}</td></tr>`).join("")}
      </tbody></table>
    </div>`).join("");
  return `
    <div class="tiles">
      ${tile("Rank", esc(p.rank), esc(p.airline))}
      ${tile("Base", esc(p.base), `rated on ${esc(p.ratings.join(", "))}`)}
      ${tile("Trips", num(trips), `${num(flights)} flights on ${num(p.duty_days.length)} duty days`)}
      ${tile("Reserve days", num(p.reserve.length), p.reserve.length ? `on call at ${esc(p.reserve[0].base)}` : "not on reserve")}
    </div>
    ${(p.handed_over || []).map((h) => `<div class="notice">Trip <b>${esc(h.pairing)}</b> was handed to ${esc(h.covered_by_name)} (${esc(h.covered_by)})
      by plan ${esc(h.plan)}, so its flights are no longer on this schedule.</div>`).join("")}
    <div class="card"><div class="card-head"><h3>Flights and duty days</h3><span class="muted small">times in UTC</span></div><div class="card-body" style="padding-top:2px">
      ${days || `<div class="empty">${p.reserve.length ? "No flights this week: on reserve (see below)." : "No rostered flights this week."}</div>`}
    </div></div>
    ${p.reserve.length ? `<div class="card"><div class="card-head"><h3>Reserve (on call)</h3></div>
      <table><thead><tr><th>Date</th><th>Window (UTC)</th><th>Base</th></tr></thead><tbody>
      ${p.reserve.map((r) => `<tr><td>${esc(fmtDay(r.date))}</td><td>${esc(r.window)}</td><td>${esc(r.base)}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
}

const fmtDay = (day) => new Date(day + "T00:00:00Z").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });

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
  const plannable = (a.gaps || []).length || (a.proposed_cancellations || []).length || (a.gate_moves || []).length;
  const ownDesk = isController && d.status === "OPTIONS_READY" && (!d.desk || d.desk === me.desk);
  const canSubmit = ownDesk && plannable;
  const canClose = ownDesk;
  const committed = plans.some((p) => p.status === "COMMITTED");
  const canWithdraw = !committed && (d.reporter === me.username || me.roles.some((r) => ["controller", "admin", "super_admin"].includes(r)));
  const canApprove = me.roles.includes("approver") && pending && pending.submitted_by !== me.name;
  let note = "";
  if (isController && d.status === "OPTIONS_READY" && d.desk && d.desk !== me.desk) note = `This disruption belongs to ${esc(d.desk)}. Only that desk can submit a plan.`;
  if (ownDesk && !plannable) note = "Nothing needs re-crewing or cancelling. Close it once the delay is handled.";
  if (me.roles.includes("approver") && pending && pending.submitted_by === me.name) note = "You submitted this plan, so a different approver must approve it.";
  if (d.status === "REPORTED" || d.status === "ANALYSING") note = "The triage agent is analysing this disruption. Refresh in a few seconds.";

  const jointPick = Object.fromEntries((a.joint_plan ? a.joint_plan.assignments : []).map((x) => [x.gap, x.crew_id]));
  const activePlan = plans.find((p) => p.status === "SUBMITTED" || p.status === "COMMITTED");
  const chosen = Object.fromEntries((activePlan ? activePlan.assignments : []).map((x) => [x.gap, x.crew]));
  let body = "";
  (a.gaps || []).forEach((g, gi) => {
    const opts = g.options;
    const best = opts.find((o) => o.crew_id);
    const pick = jointPick[g.gap] || (best ? best.crew_id : opts.some((o) => o.strategy === "short_crew") ? "short" : "cancel");
    const value = (o) => (o.crew_id ? `crew:${o.crew_id}` : o.strategy === "short_crew" ? "short" : "cancel");
    const isPick = (o) => value(o) === (pick === "short" || pick === "cancel" ? pick : `crew:${pick}`);
    body += `<div class="gap-card">
      <div class="gap-title"><div><b>${esc(g.role)} seat on ${esc(g.pairing_id)}</b>
        <div class="muted small">Vacated by ${esc(g.vacated_by)}${g.reason ? ` (${esc(g.reason)})` : ""} - ${g.uncovered_flights.length} flights${g.passengers ? ` - ${num(g.passengers)} passengers` : ""}</div></div>
        ${canSubmit ? `<span class="muted small">Choose one option</span>` : ""}
      </div>
      <div class="funnel">${g.funnel.map((st) => `<span class="chip">${esc(st.stage)} <b>${st.remaining}</b>${st.dropped ? ` <span class="muted">(-${st.dropped})</span>` : ""}</span>`).join("")}</div>
      <table><thead><tr>${canSubmit ? "<th></th>" : ""}<th>#</th><th>Option</th><th class="num">Cost</th><th class="num">Delay</th></tr></thead><tbody>
      ${opts.map((o, i) => `<tr class="${activePlan ? (chosen[g.gap] === o.crew_id && o.crew_id ? "chosen" : "") : o === best ? "best" : ""} ${o.strategy === "cancel" ? "cancel-row" : ""}">
        ${canSubmit ? `<td class="pick"><input type="radio" name="gap-${gi}" value="${esc(value(o))}" data-gap="${esc(g.gap)}" data-pairing="${esc(g.pairing_id)}"
          data-cost="${o.cost_inr}" data-label="${esc(o.action)}" data-pax="${g.passengers || 0}" ${isPick(o) ? "checked" : ""} aria-label="Choose option ${o.rank}"></td>` : ""}
        <td>${o.rank}</td><td>${esc(o.action)}${o === best ? `<span class="tag-best">Recommended</span>` : ""}${activePlan && o.crew_id && chosen[g.gap] === o.crew_id ? `<span class="tag-chosen">Chosen in ${esc(activePlan.plan)}</span>` : ""}
        ${o.crew_id && o !== best && o.strategy !== "delay_departure" ? `<button class="link why" data-crew="${esc(o.crew_id)}" type="button">why not first?</button>` : ""}</td>
        <td class="num">${inr(o.cost_inr)}</td><td class="num">${o.delay_hours ? o.delay_hours + "h" : "-"}</td></tr>`).join("")}
      ${g.delay_checked && !opts.some((o) => o.strategy === "delay_departure") ? `<tr class="note-row">${canSubmit ? "<td></td>" : ""}<td></td>
        <td colspan="3" class="muted small">Delay: holding the trip up to 4h does not make anyone else legal.</td></tr>` : ""}
      </tbody></table></div>`;
  });
  if ((a.proposed_cancellations || []).length) {
    const per = a.cancellation_cost_inr / a.proposed_cancellations.length;
    body += `<div class="gap-card"><div class="gap-title"><div><b>Legs the crew can no longer fly</b>
      <div class="muted small">The delay pushes the duty past its legal limit. These legs come off the crew.</div></div></div>
      <table><thead><tr>${canSubmit ? "<th></th>" : ""}<th>Flight</th><th>Trip</th><th>Date</th><th class="num">If cancelled</th></tr></thead><tbody>
      ${a.proposed_cancellations.map((c) => `<tr class="cancel-row">${canSubmit ? `<td class="pick"><input type="checkbox" name="cancel-leg" value="${esc(c.flight)}" data-cost="${per}" checked aria-label="Cancel ${esc(c.flight)}"></td>` : ""}
        <td><b>${esc(c.flight.split("-")[0])}</b></td><td>${esc(c.pairing)}</td><td>${esc(c.date)}</td><td class="num">${inr(per)}</td></tr>`).join("")}
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
        ${canSubmit ? `<button class="btn" id="submit-plan" type="button">${icon("check")} Submit plan for approval</button>` : ""}
        ${canClose ? `<button class="btn secondary" id="close-d" type="button">Close without a plan</button>` : ""}
        ${canWithdraw ? `<button class="btn danger" id="withdraw-d" type="button">Withdraw report</button>` : ""}
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
  act("#submit-plan", () => submitPlan(id, box), "Plan submitted. An approver will see it in their queue.");
  act("#withdraw-d", async () => {
    const ok = await confirmModal("Withdraw this report?",
      `<p>${esc(d.id)} and its analysis, options and any plans waiting for approval will be removed. The crew it grounded become available again.</p>
       <p>Use this for reports made by mistake.</p>`, "Yes, withdraw it");
    if (!ok) return SKIP;
    await api(`/api/disruptions/${id}/withdraw`, { method: "POST" });
    location.hash = location.hash.split("/").slice(0, 2).join("/");
  }, "Report withdrawn.");
  act("#close-d", () => api(`/api/disruptions/${id}/close`, { method: "POST", body: { note: "handled without a plan" } }), "Disruption closed.");
  act("#approve-plan", () => api(`/api/plans/${pending.plan}/approve`, { method: "POST" }), "Approved. The roster was updated in one graph write.");
  act("#reject-plan", async () => {
    const reason = await askModal("Reject this plan?", "Tell the controller why, so they can submit a better one.", "Reject plan");
    if (reason === null) return SKIP;
    return api(`/api/plans/${pending.plan}/reject`, { method: "POST", body: { note: reason || "rejected" } });
  }, "Plan rejected and sent back to the desk.");
  drawGraph(id);
}

const SKIP = Symbol("skip");

async function submitPlan(id, box) {
  const body = { assignments: [], cancel_pairings: [], short_gaps: [], cancel_flights: [] };
  const cancels = [];
  box.querySelectorAll('input[type="radio"]:checked').forEach((r) => {
    if (r.value.startsWith("crew:")) body.assignments.push({ gap: r.dataset.gap, crew_id: r.value.slice(5) });
    else if (r.value === "short") body.short_gaps.push(r.dataset.gap);
    else if (r.value === "cancel") {
      body.cancel_pairings.push(r.dataset.pairing);
      cancels.push(`${r.dataset.label} (${inr(Number(r.dataset.cost))}${Number(r.dataset.pax) ? `, ${num(r.dataset.pax)} passengers` : ""})`);
    }
  });
  box.querySelectorAll('input[name="cancel-leg"]:checked').forEach((c) => {
    body.cancel_flights.push(c.value);
    cancels.push(`Cancel ${c.value.split("-")[0]} (${inr(Number(c.dataset.cost))})`);
  });
  if (cancels.length) {
    const ok = await confirmModal(
      "Cancel flights?",
      `<p>This plan cancels flights. Passengers will need to be rebooked and the cost cannot be recovered.</p>
       <ul>${cancels.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
       <p>An approver still has to approve it before anything changes.</p>`,
      "Yes, cancel these flights",
    );
    if (!ok) return SKIP;
  }
  return api(`/api/disruptions/${id}/plan`, { method: "POST", body });
}

function openModal(html) {
  const wrap = document.createElement("div");
  wrap.className = "modal-backdrop";
  wrap.innerHTML = `<div class="modal" role="dialog" aria-modal="true">${html}</div>`;
  document.body.appendChild(wrap);
  return wrap;
}

function confirmModal(title, html, yes) {
  return new Promise((resolve) => {
    const m = openModal(`<h3>${esc(title)}</h3><div class="modal-body">${html}</div>
      <div class="actions"><button class="btn secondary" data-a="no" type="button">Go back</button><button class="btn danger-solid" data-a="yes" type="button">${esc(yes)}</button></div>`);
    m.querySelector('[data-a="no"]').focus();
    m.addEventListener("click", (e) => {
      const a = e.target.closest("[data-a]")?.dataset.a || (e.target === m ? "no" : null);
      if (!a) return;
      m.remove();
      resolve(a === "yes");
    });
  });
}

function askModal(title, text, yes) {
  return new Promise((resolve) => {
    const m = openModal(`<h3>${esc(title)}</h3><div class="modal-body"><p>${esc(text)}</p><textarea rows="3" placeholder="Reason"></textarea></div>
      <div class="actions"><button class="btn secondary" data-a="no" type="button">Go back</button><button class="btn danger-solid" data-a="yes" type="button">${esc(yes)}</button></div>`);
    m.querySelector("textarea").focus();
    m.addEventListener("click", (e) => {
      const a = e.target.closest("[data-a]")?.dataset.a || (e.target === m ? "no" : null);
      if (!a) return;
      const v = m.querySelector("textarea").value.trim();
      m.remove();
      resolve(a === "yes" ? v : null);
    });
  });
}

function act(sel, fn, done) {
  const el = $(sel);
  if (!el) return;
  el.addEventListener("click", async () => {
    el.disabled = true;
    try {
      if ((await fn()) === SKIP) { el.disabled = false; return; }
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

let formOptions = null;

const REASONS = ["sick", "injury", "family emergency", "fatigue", "other"];
const CERTS = [["licence", "Licence"], ["medical_class1", "Medical (class 1)"], ["recurrent_training", "Recurrent training"], ["dangerous_goods", "Dangerous goods"]];
const CAUSES = ["technical", "weather", "air traffic control", "late inbound", "crew", "other"];

async function renderReport(el, kinds, fixedCrew = null) {
  formOptions = formOptions || (await api("/api/form-options"));
  const o = formOptions;
  const day0 = o.first_date;
  const later = (n) => { const d = new Date(day0 + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + n); const v = d.toISOString().slice(0, 10); return v > o.last_date ? o.last_date : v; };
  let kind = kinds[0];
  const AIRLINES = { DX: "Deccan Express", KS: "Konkan Skyways", NG: "Nilgiri Connect" };
  const crewOptions = Object.entries(AIRLINES).map(([code, name]) => `<optgroup label="${esc(name)}">${o.crew
    .filter((c) => c.airline === code)
    .map((c) => `<option value="${esc(c.id)}">${esc(c.id)} - ${esc(c.name)} (${esc(c.rank)}, ${esc(c.base)})</option>`).join("")}</optgroup>`).join("");
  const dateInput = (name, value, required = true) =>
    `<input type="date" name="${name}" min="${o.first_date}" max="${o.last_date}" value="${value || ""}" ${required ? "required" : ""}>`;
  const dtInput = (name, value) =>
    `<input type="datetime-local" name="${name}" min="${o.first_date}T00:00" max="${o.last_date}T23:59" value="${value}" required>`;
  const select = (name, items, placeholder) =>
    `<select name="${name}" required><option value="">${esc(placeholder)}</option>${items}</select>`;
  const crewPicker = () => `<select name="crew_id" required><option value="">Choose a crew member</option>${crewOptions}</select>`;
  const flightsFor = (date) => o.flights.filter((f) => f.date === date)
    .map((f) => `<option value="${esc(f.id)}">${esc(f.no)} ${esc(f.from)} &rarr; ${esc(f.to)}, departs ${esc(f.departs)} UTC</option>`).join("");
  const field = (label, control, hint = "") => `<label class="field">${esc(label)}${hint ? ` <span class="hint">${esc(hint)}</span>` : ""}${control}</label>`;

  const FORMS = {
    crew_unavailable: () => [
      fixedCrew ? "" : field("Crew member", crewPicker()),
      field("From", dateInput("from_date", day0)),
      field("To (optional)", dateInput("to_date", "", false)),
      field("Reason", select("reason", REASONS.map((r) => `<option ${r === "sick" ? "selected" : ""}>${r}</option>`).join(""), "Choose a reason")),
    ],
    certification_lapse: () => [
      field("Crew member", crewPicker()),
      field("Grounded from", dateInput("from_date", later(3))),
      field("Certificate", select("cert_type", CERTS.map(([v, t]) => `<option value="${v}">${t}</option>`).join(""), "Choose a certificate")),
    ],
    flight_delay: () => [
      field("Date", dateInput("date", later(1))),
      field("Flight", select("flight_id", flightsFor(later(1)), "Choose a flight")),
      field("Delay (hours)", `<input type="number" name="delay_hours" min="0.25" max="12" step="0.25" value="1.5" required>`),
      field("Cause", select("cause", CAUSES.map((c) => `<option>${c}</option>`).join(""), "Choose a cause")),
    ],
    station_closure: () => [
      field("Station", select("station", o.stations.map((st) => `<option ${st === "BLR" ? "selected" : ""}>${st}</option>`).join(""), "Choose a station")),
      field("Closed from", dtInput("start", `${later(2)}T08:00`), "(UTC)"),
      field("Until", dtInput("end", `${later(2)}T14:00`), "(UTC)"),
    ],
    gate_closure: () => {
      const byStation = {};
      o.gates.forEach((g) => { (byStation[g.station] = byStation[g.station] || []).push(g); });
      const gates = Object.entries(byStation).map(([st, list]) => `<optgroup label="${st}">${list.map((g) =>
        `<option value="${esc(g.id)}">${esc(g.id)}${g.emergency ? " (emergency)" : ""}</option>`).join("")}</optgroup>`).join("");
      return [
        field("Gate", select("gate", gates, "Choose a gate")),
        field("Closed from", dtInput("start", `${day0}T00:00`), "(UTC)"),
        field("Until", dtInput("end", `${day0}T06:00`), "(UTC)"),
      ];
    },
  };

  const draw = () => {
    el.innerHTML = `<div class="card"><div class="card-body">
      ${kinds.length > 1 ? `<div class="kinds">${kinds.map((k) => `<button type="button" class="kind ${k === kind ? "active" : ""}" data-kind="${k}">
        <span class="k-title">${esc(KINDS[k].title)}</span><span class="k-sub">${esc(KINDS[k].sub)}</span></button>`).join("")}</div>` : ""}
      <form id="report-form">
        <div class="form-grid">${FORMS[kind]().join("")}</div>
        ${fixedCrew ? `<p class="muted small" style="margin-top:10px">Reporting for yourself (${esc(fixedCrew)}).</p>` : ""}
        <div class="actions"><button class="btn" type="submit">${icon("plus")} Report</button></div>
        <p id="report-msg" class="small error"></p>
      </form></div></div>
      <div class="card"><div class="card-body small muted">After you report, the triage agent claims the disruption, finds what it affects in the graph,
        ranks legal cover, and hands it to the controller desk with the fewest open disruptions.</div></div>`;
    el.querySelectorAll(".kind").forEach((b) => b.addEventListener("click", () => { kind = b.dataset.kind; draw(); }));
    const form = $("#report-form");
    // "To" can never be earlier than "From"; closures cannot end before they start
    const pairs = [["from_date", "to_date"], ["start", "end"]];
    for (const [a, b] of pairs) {
      const from = form.querySelector(`input[name="${a}"]`);
      const to = form.querySelector(`input[name="${b}"]`);
      if (!from || !to) continue;
      const sync = () => { to.min = from.value; if (to.value && to.value < from.value) to.value = from.value; };
      from.addEventListener("change", sync);
      sync();
    }
    const dateField = form.querySelector('input[name="date"]');
    if (dateField) dateField.addEventListener("change", () => {
      form.querySelector('select[name="flight_id"]').innerHTML = `<option value="">Choose a flight</option>${flightsFor(dateField.value)}`;
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const details = {};
      new FormData(form).forEach((val, k) => { if (val !== "") details[k] = val; });
      if (fixedCrew) details.crew_id = fixedCrew;
      if (details.crew_id && !o.crew.some((c) => c.id === details.crew_id.toUpperCase())) {
        $("#report-msg").textContent = "Pick a crew member from the list.";
        return;
      }
      if (details.delay_hours) details.delay_hours = Number(details.delay_hours);
      for (const k of ["start", "end"]) if (details[k]) details[k] = `${details[k]}:00Z`;
      if (kind === "flight_delay") delete details.date;
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
    crew: ["Anything waiting for me?", `Who is ${me.crew_id}?`, "When is my next flight?"],
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
  history.replaceState(null, "", location.pathname);
  showLanding();
});
window.addEventListener("hashchange", route);

// Opening the link always starts at the sign-in screen. ?as=<user> signs in directly (for demos).
(async () => {
  const wanted = new URLSearchParams(location.search).get("as");
  if (wanted) {
    try { await signIn(wanted); return; } catch { /* unknown user: show the sign-in screen */ }
  }
  history.replaceState(null, "", location.pathname);
  showLanding();
})();
