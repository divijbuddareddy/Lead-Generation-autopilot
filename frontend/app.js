/* GTM Autopilot - single-page app. Backend enforces every permission; the UI only hides what a role can't use. */
"use strict";

const S = { token: localStorage.getItem("gtm_token"), ws: localStorage.getItem("gtm_ws"), me: null, search: null, cache: {} };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const can = (p) => !!S.me && S.me.permissions.includes(p);
const fmt = (n, d = 0) => (n === null || n === undefined || n === "" ? "—" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d }));
const dt = (s) => (s ? new Date(s.endsWith("Z") || s.includes("+") ? s : s + "Z").toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—");
const localDt = (s) => (s ? new Date(s).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) : "—");
const ago = (s) => { if (!s) return ""; const d = (Date.now() - new Date(s.endsWith("Z") ? s : s + "Z")) / 1000; if (d < 60) return "just now"; if (d < 3600) return Math.floor(d / 60) + "m ago"; if (d < 86400) return Math.floor(d / 3600) + "h ago"; return Math.floor(d / 86400) + "d ago"; };

async function api(path, { method = "GET", body, form, raw } = {}) {
  const headers = {};
  if (S.token) headers.Authorization = "Bearer " + S.token;
  if (S.ws) headers["X-Workspace-Id"] = S.ws;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method, headers, body: form || (body !== undefined ? JSON.stringify(body) : undefined) });
  if (raw) return res;
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    if (res.status === 401 && !path.includes("/auth/")) { logout(); }
    const e = new Error((data && data.error && data.error.message) || res.statusText);
    e.code = data && data.error && data.error.code; e.status = res.status; throw e;
  }
  return data;
}

function toast(msg, err = false) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), err ? 6000 : 3500);
}
const fail = (e) => toast(e.message || String(e), true);

function modal(html, { wide = false } = {}) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="overlay"><div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true">${html}</div></div>`;
  $(".overlay", root).addEventListener("click", (e) => { if (e.target.classList.contains("overlay")) closeModal(); });
  $$("[data-close]", root).forEach((b) => b.addEventListener("click", closeModal));
  return $(".modal", root);
}
const closeModal = () => ($("#modal-root").innerHTML = "");
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

function logout() {
  localStorage.removeItem("gtm_token"); S.token = null; S.me = null; location.hash = "#/login"; render();
}

/* ---------- small UI helpers ---------- */
const badge = (text, kind = "") => `<span class="badge ${kind}">${esc(text)}</span>`;
const tempBadge = (t) => badge(t || "cold", t === "hot" ? "b-hot" : t === "warm" ? "b-warn" : "");
const statusKind = (s) => ({ RUNNING: "b-ok", Running: "b-ok", Scaling: "b-ok", COMPLETED: "b-info", PAUSED: "b-warn", Paused: "b-warn", READY: "b-info", DRAFT: "", BUDGET_EXHAUSTED: "b-bad", "Needs attention": "b-bad", QUEUED: "", COMPLETED_: "b-ok", FAILED: "b-bad", HEALTHY: "b-ok", WARMING: "b-info", DEGRADED: "b-bad", active: "b-ok", suspended: "b-bad", booked: "b-ok", cancelled: "b-bad", completed: "b-info", VALID: "b-ok", RISKY: "b-warn", INVALID: "b-bad" }[s] || "");
const scoreCell = (v) => `<span class="score"><span class="bar"><i style="width:${Math.max(0, Math.min(100, v || 0))}%"></i></span>${fmt(v)}</span>`;
const empty = (title, body, action = "") => `<div class="card empty"><h2>${esc(title)}</h2><p>${esc(body)}</p>${action}</div>`;
const lock = (perm) => `<span class="lock" title="Requires ${esc(perm)}">🔒 ${esc(perm)}</span>`;
const loading = () => `<div class="row muted"><span class="spinner"></span> Loading…</div>`;
function setBusy(btn, busy, label) { if (!btn) return; btn.disabled = busy; if (busy) { btn.dataset.l = btn.innerHTML; btn.innerHTML = `<span class="spinner"></span> ${label || "Working…"}`; } else if (btn.dataset.l) btn.innerHTML = btn.dataset.l; }
function on(sel, ev, fn, root = document) { const el = typeof sel === "string" ? $(sel, root) : sel; if (el) el.addEventListener(ev, fn); }

/* ---------- navigation ---------- */
const NAV = [
  ["Workspace", null],
  ["Home", "#/home", "dashboard.view", "⌂"],
  ["AI Command Center", "#/command", "agents.monitor", "✦"],
  ["Find Companies", "#/companies", "leads.view", "▦"],
  ["Find People", "#/people", "leads.view", "☺"],
  ["Research", "#/research", "leads.view", "⌕"],
  ["Leads", "#/leads", "leads.view", "≡"],
  ["GTM", null],
  ["GTM Missions", "#/missions", "leads.view", "◎"],
  ["Campaigns", "#/campaigns", "leads.view", "➤"],
  ["Inbox", "#/inbox", "conversations.view", "✉"],
  ["Meetings", "#/meetings", "meetings.view", "◷"],
  ["LinkedIn Tasks", "#/linkedin", "leads.view", "in"],
  ["Analytics", "#/analytics", "dashboard.view", "▤"],
  ["Admin", null],
  ["Integrations", "#/integrations", "integrations.manage", "⚿"],
  ["Data Sources", "#/data-sources", "leads.view", "⛁"],
  ["Usage & Billing", "#/billing", "billing.view", "$"],
  ["Settings", "#/settings", "dashboard.view", "⚙"],
];

function shell() {
  const me = S.me;
  const items = [];
  let pendingSec = null;
  if (me.organization) {
    for (const [label, href, perm, icon] of NAV) {
      if (!href) { pendingSec = label; continue; }
      if (perm && !can(perm)) continue;
      if (pendingSec) { items.push(`<div class="sec">${pendingSec}</div>`); pendingSec = null; }
      items.push(`<a href="${href}" data-nav="${href}"><span style="width:18px;text-align:center">${icon}</span>${label}${label === "Inbox" ? '<span class="badge b-hot" id="nav-review" hidden></span>' : ""}</a>`);
    }
  }
  if (me.user.is_platform_admin) items.push(`<div class="sec">Platform</div><a href="#/platform" data-nav="#/platform"><span style="width:18px;text-align:center">⛭</span>Platform Admin</a>`);
  const wsOpts = (me.workspaces || []).map((w) => `<option value="${w.id}" ${me.workspace && w.id === me.workspace.id ? "selected" : ""}>${esc(w.name)}</option>`).join("");
  return `<div class="shell">
    <aside class="sidebar" id="sidebar">
      <div class="brand"><div class="logo">↗</div><div><div>GTM Autopilot</div><div class="small muted" style="font-weight:500">${esc(me.organization ? me.organization.name : "Platform")}</div></div></div>
      <nav class="nav">${items.join("")}</nav>
      <hr>
      <div class="small muted" style="padding:0 8px">
        <div><b>${esc(me.user.name)}</b></div>
        <div>${esc(me.role_label || (me.user.is_platform_admin ? "Platform Super Admin" : ""))}${me.user.is_platform_admin && me.role ? " · Platform Admin" : ""}</div>
        ${me.demo_session ? `<div style="margin-top:4px">${badge("Demo session", "b-warn")}</div>` : ""}
        ${me.credits ? `<div style="margin-top:6px">Credits: <b>${fmt(me.credits.balance)}</b> GTU</div>` : ""}
        <div class="row" style="margin-top:10px"><button class="btn sm" id="theme-btn">◐ Theme</button><button class="btn sm" id="logout-btn">Log out</button></div>
      </div>
    </aside>
    <div class="main">
      <div class="topbar">
        <button class="btn sm menu-btn" id="menu-btn" aria-label="Menu">☰</button>
        ${me.organization && can("command.run") ? `<form class="cmd" id="cmd-form"><span>✦</span><input id="cmd-input" placeholder="Ask your GTM agent… e.g. “Find CTOs at SaaS companies in India”" autocomplete="off"><button class="btn primary sm round">Run</button></form>` : `<div class="grow"></div>`}
        ${wsOpts && (me.workspaces || []).length > 1 ? `<select id="ws-select" style="width:auto">${wsOpts}</select>` : ""}
        ${me.organization ? `<button class="btn sm" id="notif-btn" aria-label="Notifications">🔔 <span id="notif-count">${me.unread_notifications || ""}</span></button>` : ""}
      </div>
      <div id="notif-panel"></div>
      <div class="content" id="view"></div>
    </div>
  </div>`;
}

function bindShell() {
  on("#logout-btn", "click", logout);
  on("#theme-btn", "click", () => {
    const cur = document.documentElement.dataset.theme;
    const next = cur === "dark" ? "light" : cur === "light" ? "" : "dark";
    if (next) document.documentElement.dataset.theme = next; else delete document.documentElement.dataset.theme;
    try { localStorage.setItem("gtm_theme", next); } catch { /* ignore */ }
  });
  on("#menu-btn", "click", () => $("#sidebar").classList.toggle("open"));
  on("#ws-select", "change", (e) => { S.ws = e.target.value; localStorage.setItem("gtm_ws", S.ws); boot(); });
  on("#cmd-form", "submit", async (e) => {
    e.preventDefault();
    const text = $("#cmd-input").value.trim();
    if (!text) return;
    runCommand(text);
  });
  on("#notif-btn", "click", toggleNotifications);
}

async function runCommand(text) {
  const m = modal(`<div class="row between"><h2>✦ GTM Agent</h2><button class="btn sm" data-close>Close</button></div>
    <div class="chat"><div class="bubble me">${esc(text)}</div><div class="bubble" id="cmd-answer">${loading()}</div></div><div id="cmd-data" style="margin-top:16px"></div>`, { wide: true });
  try {
    const r = await api("/app/command", { method: "POST", body: { text } });
    $("#cmd-answer", m).innerHTML = esc(r.message) + (r.tool ? `<div class="small faint" style="margin-top:4px">tool: ${esc(r.tool)}</div>` : "");
    const d = r.data;
    if (d && d.results && d.mode !== "people" && d.results[0] && d.results[0].industry !== undefined) $("#cmd-data", m).innerHTML = companyTable(d.results.slice(0, 15));
    else if (d && d.results) $("#cmd-data", m).innerHTML = peopleTable(d.results.slice(0, 20));
    else if (d && d.mission) $("#cmd-data", m).innerHTML = `<a class="btn primary" href="#/missions/${d.mission.id}">Open mission</a>`;
    $$("[data-href]", m).forEach((tr) => tr.addEventListener("click", () => { closeModal(); location.hash = tr.dataset.href; }));
    $("#cmd-input").value = "";
  } catch (e) { $("#cmd-answer", m).innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`; }
}

async function toggleNotifications() {
  const p = $("#notif-panel");
  if (p.innerHTML) { p.innerHTML = ""; return; }
  const list = await api("/app/notifications");
  p.innerHTML = `<div class="card notif-panel"><div class="row between"><h2>Notifications</h2><button class="btn sm" id="mark-read">Mark all read</button></div>
    ${list.length ? list.map((n) => `<div class="list-item" data-link="${esc(n.link)}"><div class="row between"><b>${esc(n.title)}</b>${n.read ? "" : badge("new", "b-accent")}</div><div class="small muted">${esc(n.body)}</div><div class="small faint">${ago(n.at)}</div></div>`).join("") : '<p class="muted">You\'re all caught up.</p>'}</div>`;
  on("#mark-read", "click", async () => { await api("/app/notifications/read", { method: "POST" }); $("#notif-count").textContent = ""; p.innerHTML = ""; });
  $$("[data-link]", p).forEach((el) => el.addEventListener("click", () => { p.innerHTML = ""; if (el.dataset.link) location.hash = el.dataset.link; }));
}

/* ---------- auth screens ---------- */
function authView(mode) {
  return `<div class="auth"><div class="card">
    <div class="brand" style="padding:0 0 16px"><div class="logo">↗</div><div style="font-size:18px">GTM Autopilot</div></div>
    <h1>${mode === "signup" ? "Turn your website into an autonomous sales engine." : "Welcome back"}</h1>
    <p class="muted">${mode === "signup" ? "Create your workspace. You start on the Starter plan." : "Sign in to your AI sales team."}</p>
    <form id="auth-form" style="margin-top:16px">
      ${mode === "signup" ? `
      <div class="field"><label>Your name</label><input name="name" required></div>
      <div class="field"><label>Company name</label><input name="company_name" required></div>
      <div class="grid g2"><div class="field"><label>Website</label><input name="website" placeholder="https://"></div>
      <div class="field"><label>Industry</label><input name="industry"></div></div>
      <div class="grid g2"><div class="field"><label>Country</label><input name="country" value="India"></div>
      <div class="field"><label>Timezone</label><input name="timezone" value="Asia/Kolkata"></div></div>` : ""}
      <div class="field"><label>Email</label><input name="email" type="email" required autocomplete="username"></div>
      <div class="field"><label>Password</label><input name="password" type="password" required minlength="${mode === "signup" ? 8 : 1}" autocomplete="${mode === "signup" ? "new-password" : "current-password"}"></div>
      <button class="btn primary" style="width:100%;justify-content:center">${mode === "signup" ? "Create workspace" : "Sign in"}</button>
    </form>
    <p class="small muted" style="margin-top:16px;text-align:center">${mode === "signup" ? 'Already have an account? <a href="#/login">Sign in</a>' : 'New here? <a href="#/signup">Create a workspace</a>'}</p>
    ${mode === "login" ? `<div id="demo-accounts"></div>` : ""}
  </div></div>`;
}

function forcePasswordView() {
  return `<div class="auth"><div class="card">
    <div class="brand" style="padding:0 0 16px"><div class="logo">↗</div><div style="font-size:18px">GTM Autopilot</div></div>
    <h1>Set your own password</h1><p class="muted">You signed in with a one-time password. Choose a new one to continue.</p>
    <form id="fp-form" style="margin-top:16px"><div class="field"><label>One-time password</label><input name="current_password" type="password" required autocomplete="current-password"></div>
      <div class="field"><label>New password (min 8 characters)</label><input name="new_password" type="password" minlength="8" required autocomplete="new-password"></div>
      <div class="field"><label>Confirm new password</label><input name="confirm" type="password" minlength="8" required autocomplete="new-password"></div>
      <button class="btn primary" style="width:100%;justify-content:center">Save and continue</button></form>
    <p class="small" style="text-align:center;margin-top:12px"><a href="#" id="fp-logout">Log out</a></p></div></div>`;
}

function bindForcePassword() {
  on("#fp-logout", "click", (e) => { e.preventDefault(); logout(); });
  on("#fp-form", "submit", async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    if (f.new_password !== f.confirm) return toast("Passwords don't match.", true);
    try { await api("/app/auth/password", { method: "POST", body: { current_password: f.current_password, new_password: f.new_password } }); toast("Password updated"); location.hash = "#/home"; boot(); }
    catch (err) { fail(err); }
  });
}

async function loadDemoAccounts() {
  const box = $("#demo-accounts");
  if (!box) return;
  let list = [];
  try { list = await api("/app/auth/demo-accounts"); } catch { list = []; }
  if (!list.length) return;
  box.innerHTML = `<div style="margin-top:20px"><div class="row" style="gap:8px;margin-bottom:8px"><hr style="flex:1;margin:0"><span class="small muted">Quick demo access - click a name to sign in</span><hr style="flex:1;margin:0"></div>
    ${list.map((a) => `<button type="button" class="list-item" data-demo="${esc(a.role)}" style="display:block;width:100%;text-align:left;border:1px solid var(--border);border-radius:8px;margin-bottom:6px;background:var(--surface);color:var(--text);font:inherit">
      <div class="row between"><b>${esc(a.name)}</b>${badge(a.label, a.role === "owner" || a.role === "platform_admin" ? "b-accent" : "")}</div>
      <div class="small muted">${esc(a.email)} · ${esc(a.description)}</div></button>`).join("")}</div>`;
  $$("[data-demo]", box).forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      const r = await api("/app/auth/demo-login", { method: "POST", body: { role: b.dataset.demo } });
      S.token = r.token; localStorage.setItem("gtm_token", r.token); localStorage.removeItem("gtm_ws"); S.ws = null;
      location.hash = b.dataset.demo === "platform_admin" ? "#/platform" : "#/home";
      await boot();
    } catch (err) { fail(err); b.disabled = false; }
  }));
}

function bindAuth(mode) {
  if (mode === "login") loadDemoAccounts();
  on("#auth-form", "submit", async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(e.target));
    const btn = $("button", e.target);
    setBusy(btn, true);
    try {
      const r = await api(mode === "signup" ? "/app/auth/signup" : "/app/auth/login", { method: "POST", body });
      S.token = r.token; localStorage.setItem("gtm_token", r.token);
      localStorage.removeItem("gtm_ws"); S.ws = null;
      location.hash = mode === "signup" ? "#/onboarding" : "#/home";
      await boot();
    } catch (err) { fail(err); setBusy(btn, false); }
  });
}

/* ---------- tables ---------- */
function companyTable(rows, { select = false } = {}) {
  if (!rows.length) return empty("No companies yet", "Describe your ideal customer above and the agent will find matching companies.");
  return `<div class="tbl-wrap"><table><thead><tr>${select ? '<th><input type="checkbox" id="sel-all"></th>' : ""}<th>Company</th><th>Industry</th><th>Location</th><th>Employees</th><th>ICP</th><th>Intent</th><th>Opportunity</th><th>Top signal</th></tr></thead><tbody>
  ${rows.map((c) => `<tr class="click" data-href="#/companies/${c.id}">${select ? `<td onclick="event.stopPropagation()"><input type="checkbox" class="sel" value="${c.id}"></td>` : ""}
    <td><b>${esc(c.name)}</b><div class="small faint">${esc(c.domain)}</div></td><td>${esc(c.industry)}<div class="small faint">${esc(c.sub_industry)}</div></td>
    <td>${esc(c.city)}${c.city ? ", " : ""}${esc(c.country)}</td><td>${fmt(c.employee_count)}</td>
    <td>${scoreCell(c.scores.icp)}</td><td>${scoreCell(c.scores.intent)}</td><td>${badge(`${c.scores.priority || ""} ${fmt(c.scores.opportunity)}`, c.scores.priority === "A" ? "b-ok" : c.scores.priority === "B" ? "b-info" : "")}${c.similarity !== undefined ? `<div class="small faint">similarity ${c.similarity}</div>` : ""}</td>
    <td class="small">${esc((c.signals[0] || {}).text || "—")}</td></tr>`).join("")}</tbody></table></div>`;
}

function peopleTable(rows, { select = false } = {}) {
  if (!rows.length) return empty("No people yet", "Search for decision makers by title and company criteria.");
  return `<div class="tbl-wrap"><table><thead><tr>${select ? '<th><input type="checkbox" id="sel-all"></th>' : ""}<th>Name</th><th>Company</th><th>Title</th><th>ICP</th><th>Intent</th><th>Email</th><th>Phone</th><th>Location</th><th>Stage</th></tr></thead><tbody>
  ${rows.map((p) => `<tr class="click" data-href="#/leads/${p.id}">${select ? `<td onclick="event.stopPropagation()"><input type="checkbox" class="sel" value="${p.id}"></td>` : ""}
    <td><b>${esc(p.full_name)}</b> ${p.temperature && p.temperature !== "cold" ? tempBadge(p.temperature) : ""}${p.owner ? `<div class="small faint">owner: ${esc(p.owner)}</div>` : ""}</td>
    <td>${esc(p.company ? p.company.name : "")}<div class="small faint">${esc(p.company ? p.company.industry : "")}</div></td>
    <td>${esc(p.title)}<div class="small faint">${esc(p.normalized_title)}</div></td>
    <td>${scoreCell(p.icp_score)}</td><td>${scoreCell(p.intent_score)}</td>
    <td class="small">${p.email ? esc(p.email) + " " + badge(p.email_status, statusKind(p.email_status)) : p.has_email ? badge("available - enrich", "b-info") : "—"}</td>
    <td class="small">${p.phone ? esc(p.phone) : p.has_phone ? badge("available", "b-info") : "—"}</td>
    <td class="small">${esc(p.city)}</td><td>${badge(p.stage)}</td></tr>`).join("")}</tbody></table></div>`;
}
const bindRows = (root = document) => $$("[data-href]", root).forEach((tr) => tr.addEventListener("click", () => (location.hash = tr.dataset.href)));
const selected = (root = document) => $$(".sel:checked", root).map((x) => x.value);
function bindSelectAll(root = document) { on("#sel-all", "change", (e) => $$(".sel", root).forEach((x) => (x.checked = e.target.checked)), root); }

/* ---------- pages ---------- */
const V = () => $("#view");

async function pageHome() {
  V().innerHTML = loading();
  const d = await api("/app/analytics/dashboard");
  const k = d.kpis;
  const hour = new Date().getHours();
  const greet = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  const max = Math.max(1, ...d.daily.map((x) => x.sent));
  V().innerHTML = `
  <div class="page-head"><div><h1>${greet}, ${esc(S.me.user.name.split(" ")[0])}.</h1>
    <p class="muted">${d.agents_active ? `Your autonomous GTM system is running · ${d.running_campaigns} campaign(s) active` : "Your GTM engine is not running yet."}</p></div>
    <div class="row">${can("missions.manage") ? '<a class="btn primary" href="#/missions?new=1">+ New mission</a>' : ""}${!S.me.organization.onboarded && can("workspace.manage") ? '<a class="btn" href="#/onboarding">Set up from website</a>' : ""}</div></div>
  <div class="grid g4">
    ${[["Hot leads", k.hot_leads], ["Meetings", k.meetings], ["Positive replies", k.positive_replies], ["Outreach sent", k.outreach]].map(([l, v]) => `<div class="card kpi"><div class="l">${l}</div><div class="v">${fmt(v)}</div></div>`).join("")}
    ${[["Reply rate", k.reply_rate + "%"], ["Meeting rate", k.meeting_rate + "%"], ["Spend (GTU)", fmt(k.spend_gtu, 1)], ["Cost / meeting", k.cost_per_meeting ? fmt(k.cost_per_meeting, 1) + " GTU" : "—"]].map(([l, v]) => `<div class="card kpi"><div class="l">${l}</div><div class="v">${v}</div></div>`).join("")}
  </div>
  <div class="grid g-side" style="margin-top:16px">
    <div class="stack">
      <div class="card brief"><h3>✦ AI executive brief</h3>
        <p><b>What happened.</b> ${esc(d.brief.what_happened)}</p>
        <p><b>What I changed.</b> ${d.brief.what_i_changed.map(esc).join(" · ")}</p>
        <p><b>What I'm testing.</b> ${d.brief.what_im_testing.map(esc).join(" · ")}</p>
        <p style="margin:0"><b>What requires you.</b> ${esc(d.brief.what_requires_you)} ${d.needs_attention ? '<a href="#/inbox?f=human_review">Open review queue →</a>' : ""}</p></div>
      <div class="card"><div class="row between"><h2>Campaigns</h2><a href="#/campaigns">All campaigns →</a></div>
        ${d.campaigns.length ? `<div class="tbl-wrap"><table><thead><tr><th>Campaign</th><th>Status</th><th>Sent</th><th>Replies</th><th>Hot</th><th>Meetings</th><th>Spend</th><th>Cost/lead</th></tr></thead><tbody>
        ${d.campaigns.map((c) => `<tr class="click" data-href="#/campaigns/${c.id}"><td><b>${esc(c.name)}</b></td><td>${badge(c.status_label, statusKind(c.status_label))}</td><td>${c.stats.sent}</td><td>${c.stats.replies} <span class="faint small">(${c.stats.reply_rate}%)</span></td><td>${c.stats.hot}</td><td>${c.stats.meetings}</td><td>${fmt(c.stats.spend, 1)}</td><td>${c.stats.cost_per_lead ?? "—"}</td></tr>`).join("")}</tbody></table></div>`
        : `<div class="empty"><h2>Your GTM engine is not running yet.</h2><p>Tell the agent who you sell to and it will build the mission, campaigns and audience.</p>${can("missions.manage") ? '<a class="btn primary" href="#/missions?new=1">Create your first mission</a>' : ""}</div>`}</div>
      <div class="card"><h2>Last 14 days</h2><div class="spark">${d.daily.map((x) => `<div title="${x.date}: ${x.sent} sent, ${x.replies} replies, ${x.hot} hot" style="height:${(100 * x.sent) / max}%"></div>`).join("")}</div>
        <div class="row between small faint" style="margin-top:4px"><span>${d.daily[0].date}</span><span>messages sent per day</span><span>${d.daily[d.daily.length - 1].date}</span></div></div>
    </div>
    <div class="stack">
      <div class="card"><h2>AI recommendations</h2>${d.recommendations.length ? `<ul style="margin:0;padding-left:18px">${d.recommendations.map((r) => `<li style="margin-bottom:6px">${esc(r)}</li>`).join("")}</ul>` : '<p class="muted">No recommendations right now.</p>'}</div>
      <div class="card"><h2>Funnel</h2>${funnel(d.funnel)}</div>
      ${can("command.run") ? `<div class="card"><h2>Ask your GTM agent</h2><div class="chips">${["Find more companies like our best customers", "Show me prospects with high buying intent", "Why are our replies declining?", "Pause campaigns with cost per meeting above $50", "Target founders in Bangalore"].map((q) => `<span class="chip" data-q="${esc(q)}">${esc(q)}</span>`).join("")}</div></div>` : ""}
    </div>
  </div>`;
  bindRows();
  $$("[data-q]").forEach((c) => c.addEventListener("click", () => runCommand(c.dataset.q)));
}

function funnel(rows) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  return rows.map((r) => `<div class="funnel-row"><span class="muted">${esc(r.stage)}</span><div><div class="f" style="width:${Math.max(1, (100 * r.value) / max)}%"></div></div><b style="text-align:right">${fmt(r.value)}</b></div>`).join("");
}

async function pageCommandCenter() {
  V().innerHTML = loading();
  const [d, decisions, jobs] = await Promise.all([api("/app/analytics/dashboard"), api("/app/analytics/decisions"), api("/app/jobs")]);
  const agents = ["research", "gtm_orchestrator", "compliance", "reply", "booking", "optimization", "command_bar"];
  V().innerHTML = `<div class="page-head"><div><h1>AI Command Center</h1><p class="muted">What happened, what is happening, what the AI did, what it will do next, and what needs you.</p></div>
    <div class="row"><button class="btn" id="opt-btn">Run optimization agent now</button></div></div>
  <div class="grid g4">${agents.slice(0, 4).map((a) => { const n = decisions.filter((x) => x.agent === a).length; return `<div class="card kpi"><div class="l">${a.replace("_", " ")} agent</div><div class="v">${n}</div><div class="small faint">decisions logged</div></div>`; }).join("")}</div>
  <div class="grid g-side" style="margin-top:16px">
    <div class="card"><h2>Agent decision trace</h2><p class="small muted">Every autonomous action with its reason, confidence and policy result. Chain-of-thought is never stored, only concise rationale and evidence.</p>
      <div class="chips" id="agent-filter" style="margin-bottom:10px"><span class="chip on" data-a="">All</span>${agents.map((a) => `<span class="chip" data-a="${a}">${a.replace("_", " ")}</span>`).join("")}</div>
      <div id="dec-list">${decisionTable(decisions)}</div></div>
    <div class="stack"><div class="card brief"><h3>✦ Brief</h3><p>${esc(d.brief.what_happened)}</p><p class="small"><b>Needs you:</b> ${esc(d.brief.what_requires_you)}</p></div>
      <div class="card"><h2>Background jobs</h2>${jobs.length ? jobs.slice(0, 10).map((j) => `<div class="row between small" style="margin-bottom:6px"><span>${esc(j.type)} ${badge(j.status, statusKind(j.status))}</span><span>${j.total ? `${j.progress}/${j.total} (${j.percent}%)` : ""}</span></div>`).join("") : '<p class="muted small">No jobs yet.</p>'}</div>
      <div class="card"><h2>Agent loop</h2><p class="small muted">LEARN → RESEARCH → TARGET → ENRICH → SCORE → PLAN → EXECUTE → CONVERSE → QUALIFY → BOOK → MEASURE → LEARN</p></div></div>
  </div>`;
  $$("#agent-filter .chip").forEach((c) => c.addEventListener("click", async () => {
    $$("#agent-filter .chip").forEach((x) => x.classList.remove("on")); c.classList.add("on");
    $("#dec-list").innerHTML = decisionTable(await api("/app/analytics/decisions" + (c.dataset.a ? "?agent=" + c.dataset.a : "")));
  }));
  on("#opt-btn", "click", async (e) => { setBusy(e.target, true); try { const r = await api("/app/agents/optimize", { method: "POST" }); toast(r.actions.length ? r.actions.join("; ") : "No changes - not enough data or nothing to optimize yet."); pageCommandCenter(); } catch (err) { fail(err); setBusy(e.target, false); } });
}

function decisionTable(rows) {
  if (!rows.length) return '<p class="muted">No decisions yet.</p>';
  return `<div class="tbl-wrap" style="max-height:560px"><table><thead><tr><th>When</th><th>Agent</th><th>Action</th><th>Why</th><th>Conf.</th><th>Policy</th></tr></thead><tbody>
  ${rows.map((x) => `<tr><td class="small">${ago(x.at)}</td><td>${badge(x.agent)}</td><td class="small"><b>${esc(x.action.replace(/_/g, " "))}</b><div class="faint">${esc(x.model)}</div></td><td class="small">${esc(x.reason)}${x.evidence && x.evidence.length ? `<div class="faint">${x.evidence.map((e) => esc(typeof e === "string" ? e : JSON.stringify(e))).join("; ")}</div>` : ""}</td><td>${Math.round((x.confidence || 0) * 100)}%</td><td>${badge(x.decision, x.decision === "executed" ? "b-ok" : x.decision === "blocked" ? "b-bad" : "b-warn")}</td></tr>`).join("")}</tbody></table></div>`;
}

/* ----- Find companies: chat + results + filters (PRD 15, 100) ----- */
async function pageCompanies() {
  const st = S.search || (S.search = { chat: [], criteria: null, results: null, mode: "companies" });
  V().innerHTML = `<div class="page-head"><div><h1>Find Companies</h1><p class="muted">Describe your ideal customer. Refine in plain language: “Exclude Mumbai”, “Only show owners and CEOs”.</p></div>
    <div class="row"><button class="btn" id="saved-btn">Saved companies</button></div></div>
  <div class="grid g-side">
    <div class="stack">
      <div class="card">${can("search.run") ? `<form id="search-form" class="row"><input id="search-q" class="grow" placeholder="AI B2B SaaS companies in India with 20-200 employees that are hiring salespeople" autocomplete="off"><button class="btn primary">Search</button>${st.criteria ? '<button type="button" class="btn" id="reset-btn">New search</button>' : ""}</form>` : `<p class="muted">Browsing saved companies. ${lock("search.run")}</p>`}
        <div id="crit" style="margin-top:10px">${st.criteria ? critChips(st.criteria) : `<div class="chips">${["Indian SaaS companies with 20-200 employees", "Manufacturing companies in Chennai expanding internationally", "Healthcare companies in Bangalore with 100+ employees", "Real estate developers in Pune using WhatsApp Business"].map((q) => `<span class="chip" data-ex="${esc(q)}">${esc(q)}</span>`).join("")}</div>`}</div></div>
      <div class="card"><div class="row between" style="margin-bottom:10px"><h2 id="res-title">${st.results ? `${st.results.total ?? st.results.length} ${st.mode === "people" ? "people" : "companies"}` : "Results"}</h2>
        <div class="row" id="bulk-bar">${st.mode === "companies" ? `${can("research.run") ? '<button class="btn sm" data-bulk="research">Research</button>' : ""}${can("search.run") ? '<button class="btn sm" data-bulk="people">Find decision makers</button><button class="btn sm" data-bulk="lookalike">Find lookalikes</button>' : ""}${can("research.run") ? '<button class="btn sm" data-bulk="criterion">Score by AI criterion</button>' : ""}${can("lists.manage") ? '<button class="btn sm" data-bulk="save">Save search</button>' : ""}` : `${can("campaigns.manage") ? '<button class="btn sm" data-bulk="campaign">Add to campaign</button>' : ""}${can("enrich.run") ? '<button class="btn sm" data-bulk="enrich">Enrich emails</button>' : ""}`}</div></div>
        <div id="results">${st.results ? (st.mode === "people" ? peopleTable(st.results.rows, { select: true }) : companyTable(st.results.rows, { select: true })) : '<p class="muted">Run a search to see companies ranked by opportunity score.</p>'}</div></div>
    </div>
    <div class="card"><h2>✦ Search assistant</h2><div class="chat" id="chat">${st.chat.map((c) => `<div class="bubble ${c.me ? "me" : ""}">${esc(c.t)}</div>`).join("") || '<p class="muted small">The assistant converts your request into structured filters you can keep refining.</p>'}</div></div>
  </div>`;
  bindRows(); bindSelectAll();
  $$("[data-ex]").forEach((c) => c.addEventListener("click", () => { $("#search-q").value = c.dataset.ex; $("#search-form").requestSubmit(); }));
  on("#reset-btn", "click", () => { S.search = null; pageCompanies(); });
  on("#saved-btn", "click", async () => { const r = await api("/app/companies?limit=200"); st.mode = "companies"; st.results = { rows: r.results, total: r.total }; pageCompanies(); });
  on("#search-form", "submit", async (e) => {
    e.preventDefault();
    const q = $("#search-q").value.trim(); if (!q) return;
    st.chat.push({ me: true, t: q });
    const btn = $("button", e.target); setBusy(btn, true, "Searching");
    try {
      const body = { query: q, base: st.criteria, limit: 60 };
      if (st.mode === "companies" && st.results) body.company_ids = st.results.rows.map((r) => r.id);
      const r = await api("/app/search", { method: "POST", body });
      st.criteria = r.criteria; st.mode = r.mode;
      st.results = { rows: r.results, total: r.mode === "people" ? r.returned : r.total_matches };
      st.chat.push({ t: r.mode === "people" ? `Switched to people search: ${r.returned} decision makers (${r.summary}).` : `I found ${fmt(r.total_matches)} companies: ${r.summary}. Showing the top ${r.returned} by opportunity.${r.parser !== "rules" && r.parser !== "structured" ? " (parsed by " + r.parser + ")" : ""}${r.provider ? " Source: " + r.provider + "." : ""}${r.provider_notes && r.provider_notes.length ? " Not filterable at source: " + r.provider_notes.join(", ") + "." : ""}${r.provider_notice ? " ⚠ " + r.provider_notice : ""}` });
    } catch (err) { fail(err); st.chat.push({ t: "⚠ " + err.message }); }
    pageCompanies();
  });
  $$("[data-bulk]").forEach((b) => b.addEventListener("click", () => companyBulk(b.dataset.bulk, b)));
}

function critChips(c) {
  const parts = [];
  const add = (k, label) => (c[k] || []).forEach((v) => parts.push(`<span class="chip on">${esc(label)}: ${esc(v)}</span>`));
  add("industries", "Industry"); add("countries", "Country"); add("states", "State"); add("cities", "City"); add("exclude_cities", "Exclude"); add("signals", "Signal"); add("technologies", "Tech"); add("job_titles", "Title"); add("ai_criteria", "AI");
  if (c.employee_min || c.employee_max) parts.push(`<span class="chip on">Employees: ${c.employee_min || 1}-${c.employee_max || "∞"}</span>`);
  if (c.exclude_contacted) parts.push('<span class="chip on">Exclude contacted</span>');
  return `<div class="chips">${parts.join("") || '<span class="muted small">No filters</span>'}</div>`;
}

async function companyBulk(action, btn) {
  const st = S.search;
  const ids = selected();
  const all = st && st.results ? st.results.rows.map((r) => r.id) : [];
  const target = ids.length ? ids : all.slice(0, 20);
  if (!target.length && action !== "save") return toast("Select companies first.", true);
  try {
    if (action === "research") {
      setBusy(btn, true);
      const r = await api("/app/research", { method: "POST", body: { company_ids: target, depth: "standard" } });
      toast(r.job_id ? `Researching ${r.total} companies in the background.` : "Research complete."); setBusy(btn, false);
    } else if (action === "people") {
      setBusy(btn, true);
      const r = await api("/app/search", { method: "POST", body: { mode: "people", company_ids: target, criteria: { job_titles: ["Founder", "CEO", "COO", "Head of Sales"] }, limit: 100 } });
      st.mode = "people"; st.results = { rows: r.results, total: r.returned }; st.chat.push({ t: `Found ${r.returned} decision makers at ${target.length} companies.` }); pageCompanies();
    } else if (action === "lookalike") {
      setBusy(btn, true);
      const r = await api("/app/companies/lookalike", { method: "POST", body: { company_ids: target.slice(0, 10), limit: 40 } });
      st.mode = "companies"; st.results = { rows: r.results, total: r.results.length }; st.criteria = r.profile; st.chat.push({ t: `Lookalikes of ${Math.min(10, target.length)} companies, ranked by similarity.` }); pageCompanies();
    } else if (action === "criterion") {
      const m = modal(`<h2>Score by custom AI criterion</h2><p class="muted small">e.g. “Likely to have repetitive customer-support processes”. Scores are saved on each company.</p><div class="field"><input id="crit-in" placeholder="Describe the criterion"></div><div class="row"><button class="btn primary" id="crit-go">Score ${target.length} companies</button><button class="btn" data-close>Cancel</button></div><div id="crit-res" style="margin-top:12px"></div>`);
      on("#crit-go", "click", async (e) => {
        setBusy(e.target, true);
        try { const r = await api("/app/companies/criteria", { method: "POST", body: { criterion: $("#crit-in").value, company_ids: target } });
          $("#crit-res", m).innerHTML = `<div class="tbl-wrap"><table><thead><tr><th>Company</th><th>Score</th><th>Reason</th></tr></thead><tbody>${r.results.map((x) => `<tr><td>${esc(x.name)}</td><td>${x.score}</td><td class="small">${esc(x.reason)}</td></tr>`).join("")}</tbody></table></div>`;
        } catch (err) { fail(err); } setBusy(e.target, false);
      }, m);
    } else if (action === "save") {
      const name = prompt("Name this saved search:", "Saved search"); if (!name) return;
      await api("/app/lists", { method: "POST", body: { name, type: "saved_search", definition: { criteria: st.criteria || {}, limit: 50 } } });
      toast("Saved. It stores the definition, so it can be refreshed later.");
    } else if (action === "enrich") {
      setBusy(btn, true);
      const r = await api("/app/leads/bulk", { method: "POST", body: { person_ids: target, action: "enrich" } });
      toast(r.job_id ? "Enrichment job started." : "Enriched."); setBusy(btn, false);
    } else if (action === "campaign") {
      addToCampaignModal(target);
    }
  } catch (e) { fail(e); setBusy(btn, false); }
}

async function addToCampaignModal(ids) {
  const camps = await api("/app/campaigns");
  if (!camps.length) return toast("Create a campaign first (GTM Missions or Campaigns).", true);
  modal(`<h2>Add ${ids.length} people to a campaign</h2><div class="field"><select id="camp-sel">${camps.map((c) => `<option value="${c.id}">${esc(c.name)} (${c.status})</option>`).join("")}</select></div><div class="row"><button class="btn primary" id="add-go">Add</button><button class="btn" data-close>Cancel</button></div>`);
  on("#add-go", "click", async () => { try { const r = await api("/app/leads/bulk", { method: "POST", body: { person_ids: ids, action: "add_to_campaign", campaign_id: $("#camp-sel").value } }); toast(`Added ${r.count}.`); closeModal(); } catch (e) { fail(e); } });
}

async function pageCompany(id) {
  V().innerHTML = loading();
  const c = await api("/app/companies/" + id);
  const ex = c.score_explanation || {};
  const r = c.research || {};
  V().innerHTML = `<div class="page-head"><div><a href="#/companies" class="small">← Companies</a><h1>${esc(c.name)}</h1>
    <p class="muted">${esc(c.industry)} · ${esc(c.sub_industry)} · ${esc(c.city)}, ${esc(c.country)} · ${fmt(c.employee_count)} employees · <span class="mono">${esc(c.domain)}</span></p></div>
    <div class="row">${badge("ICP " + fmt(c.scores.icp), "b-accent")}${badge("Intent " + fmt(c.scores.intent), "b-hot")}${badge((c.scores.freshness_label || "") + " data", "b-info")}
    ${can("research.run") ? '<button class="btn" data-depth="standard">Research</button><button class="btn" data-depth="deep">Deep research</button>' : ""}</div></div>
  <div class="grid g-side"><div class="stack">
    <div class="card"><h3>Why this company?</h3><p>${esc(c.why)}</p>
      <div class="grid g2"><div><b>ICP fit ${fmt(c.scores.icp)} / 100</b><ul class="explain small">${(ex.icp || []).map((x) => `<li><b>${esc(x.points)}</b> ${esc(x.label)} <span class="faint">(${esc(x.evidence)})</span></li>`).join("") || "<li class='faint'>No ICP matches</li>"}</ul></div>
      <div><b>Buying intent ${fmt(c.scores.intent)} / 100</b><ul class="explain small">${(ex.intent || []).map((x) => `<li><b>${esc(x.points)}</b> ${esc(x.label)}: <span class="faint">${esc(x.evidence)}</span></li>`).join("") || "<li class='faint'>No signals</li>"}</ul></div></div>
      <p class="small faint">Opportunity ${fmt(c.scores.opportunity)} = ICP×${ex.weights ? ex.weights.icp : ""} + Intent×${ex.weights ? ex.weights.intent : ""} + Reachability×${ex.weights ? ex.weights.reachability : ""} + Confidence×${ex.weights ? ex.weights.confidence : ""} + Freshness×${ex.weights ? ex.weights.freshness : ""}</p></div>
    <div class="card"><h3>Overview</h3><p>${esc(c.description)}</p><div class="kv"><div>Business model</div><div>${esc(c.business_model)}</div><div>Revenue estimate</div><div>${c.revenue_estimate ? "$" + fmt(c.revenue_estimate) : "—"}</div><div>Founded</div><div>${c.founded_year || "—"}</div><div>Technologies</div><div>${(c.technologies || []).map((t) => badge(t)).join(" ")}</div></div></div>
    <div class="card"><h3>Signals</h3>${(c.signals || []).length ? c.signals.map((s) => `<div class="row between" style="margin-bottom:6px"><span>${badge(s.type.replace("_", " "), "b-info")} ${esc(s.text)}</span><span class="small faint">${esc(s.source)} · ${s.observed_days_ago}d ago</span></div>`).join("") : '<p class="muted">No signals detected yet.</p>'}</div>
    <div class="card"><h3>AI research ${r.model ? `<span class="faint small">(${esc(r.depth)} · ${esc(r.model)})</span>` : ""}</h3>${r.company_overview ? `
      <p>${esc(r.company_overview)}</p><div class="grid g2">
      ${[["Likely pain points", r.likely_pain_points], ["Buying signals", r.buying_signals], ["Growth indicators", r.growth_indicators], ["Decision makers", r.relevant_decision_makers]].map(([t, l]) => `<div><b class="small">${t}</b><ul class="small">${(l || []).map((x) => `<li>${esc(x)}</li>`).join("") || "<li class='faint'>unknown</li>"}</ul></div>`).join("")}</div>
      <p><b>Suggested approach:</b> ${esc(r.suggested_approach)}</p><details class="small"><summary>Evidence (${(r.evidence || []).length})</summary><ul>${(r.evidence || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul></details>` : `<p class="muted">Not researched yet.</p>`}</div>
    <div class="card"><h3>People</h3>${peopleTable(c.people)}</div>
  </div><div class="stack">
    <div class="card"><h3>Data provenance</h3><p class="small muted">Click-through for “why did you say this?”</p>${(c.provenance || []).slice(0, 14).map((o) => `<div class="small" style="margin-bottom:6px"><b>${esc(o.field)}</b> = ${esc(o.value.slice(0, 80))}<div class="faint">Source: ${esc(o.provider)} · ${dt(o.observed_at)} · confidence ${Math.round(o.confidence * 100)}%</div></div>`).join("") || '<p class="muted small">No observations</p>'}</div>
    <div class="card"><h3>Timeline</h3>${(c.timeline || []).slice(0, 15).map((e) => `<div class="small" style="margin-bottom:4px">${esc(e.type)} <span class="faint">${ago(e.at)}</span></div>`).join("") || '<p class="muted small">No activity</p>'}</div>
  </div></div>`;
  bindRows();
  $$("[data-depth]").forEach((b) => b.addEventListener("click", async () => { setBusy(b, true, "Researching"); try { await api("/app/research", { method: "POST", body: { company_ids: [id], depth: b.dataset.depth } }); toast("Research complete"); pageCompany(id); } catch (e) { fail(e); setBusy(b, false); } }));
}

async function pagePeople() {
  V().innerHTML = `<div class="page-head"><div><h1>Find People</h1><p class="muted">Search titles with company criteria. “Head of Sales” also finds VP Sales, CRO, Sales Director… (title ontology).</p></div></div>
  <div class="card">${can("search.run") ? `<form id="pform" class="grid g3"><div><label>Job titles (comma separated)</label><input id="p-titles" value="Founder, CEO, COO"></div><div><label>Company criteria</label><input id="p-co" placeholder="IT services companies in India with 50-500 employees"></div><div style="align-self:end"><button class="btn primary">Search people</button></div></form>` : lock("search.run")}</div>
  <div class="card" style="margin-top:16px"><div class="row between" style="margin-bottom:10px"><h2 id="p-count">Leads in workspace</h2><div class="row">${can("campaigns.manage") ? '<button class="btn sm" id="p-camp">Add to campaign</button>' : ""}${can("enrich.run") ? '<button class="btn sm" id="p-enrich">Enrich</button>' : ""}</div></div><div id="p-res">${loading()}</div></div>`;
  const show = (rows) => { $("#p-res").innerHTML = peopleTable(rows, { select: true }); bindRows(); bindSelectAll(); };
  api("/app/leads?limit=100").then((r) => { $("#p-count").textContent = `${r.total} people in workspace`; show(r.results); }).catch(fail);
  on("#pform", "submit", async (e) => {
    e.preventDefault(); const b = $("button", e.target); setBusy(b, true, "Searching");
    try {
      const crit = $("#p-co").value ? (await api("/app/search/parse", { method: "POST", body: { query: $("#p-co").value } })).criteria : {};
      crit.job_titles = $("#p-titles").value.split(",").map((s) => s.trim()).filter(Boolean);
      const r = await api("/app/search", { method: "POST", body: { mode: "people", criteria: crit, limit: 150 } });
      $("#p-count").textContent = `${r.returned} people · ${r.summary}`; show(r.results);
    } catch (err) { fail(err); } setBusy(b, false);
  });
  on("#p-camp", "click", () => { const ids = selected(); if (!ids.length) return toast("Select people first.", true); addToCampaignModal(ids); });
  on("#p-enrich", "click", async (e) => { const ids = selected(); if (!ids.length) return toast("Select people first.", true); setBusy(e.target, true); try { const r = await api("/app/leads/bulk", { method: "POST", body: { person_ids: ids, action: "enrich" } }); toast(r.job_id ? "Enrichment job started" : "Enriched"); } catch (err) { fail(err); } setBusy(e.target, false); });
}

async function pageResearch() {
  V().innerHTML = loading();
  const r = await api("/app/companies?limit=200");
  const researched = r.results.filter((c) => c.research && c.research.company_overview);
  V().innerHTML = `<div class="page-head"><div><h1>Research</h1><p class="muted">Quick (website + provider data), Standard (multiple sources + signals) or Deep (multi-page + competitors + synthesis). Deep research is a billable capability.</p></div>
    <div class="row">${can("research.run") ? '<select id="depth" style="width:auto"><option value="quick">Quick</option><option value="standard" selected>Standard</option><option value="deep">Deep</option></select><button class="btn primary" id="rs-go">Research selected</button>' : lock("research.run")}</div></div>
  <div class="grid g2"><div class="card"><h2>Companies (${r.total})</h2>${companyTable(r.results, { select: true })}</div>
  <div class="card"><h2>Researched (${researched.length})</h2>${researched.length ? researched.map((c) => `<div class="list-item" data-href="#/companies/${c.id}"><b>${esc(c.name)}</b> <span class="faint small">${esc(c.research.depth)} · ${esc(c.research.model)}</span><div class="small muted">${esc((c.research.likely_pain_points || [])[0] || "")}</div></div>`).join("") : '<p class="muted">Nothing researched yet. Select companies and click “Research selected”.</p>'}</div></div>`;
  bindRows(); bindSelectAll();
  on("#rs-go", "click", async (e) => { const ids = selected(); if (!ids.length) return toast("Select companies first.", true); setBusy(e.target, true); try { const x = await api("/app/research", { method: "POST", body: { company_ids: ids, depth: $("#depth").value } }); toast(x.job_id ? `Job started for ${x.total} companies - see Command Center` : "Research complete"); pageResearch(); } catch (err) { fail(err); setBusy(e.target, false); } });
}

/* ----- Leads ----- */
async function pageLeads(params) {
  const q = params.get("q") || "", stage = params.get("stage") || "", temp = params.get("t") || "";
  V().innerHTML = loading();
  const [r, lists] = await Promise.all([api(`/app/leads?limit=200&q=${encodeURIComponent(q)}&stage=${encodeURIComponent(stage)}&temperature=${temp}`), api("/app/lists")]);
  const stages = ["New", "Contacted", "Engaged", "Warm", "Hot", "Meeting Booked", "Meeting Completed", "Opportunity", "Won", "Lost", "Nurture", "Suppressed"];
  V().innerHTML = `<div class="page-head"><div><h1>Leads</h1><p class="muted">${fmt(r.total)} people · pipeline stages, scores, channels and ownership.</p></div>
    <div class="row">${can("data.import") ? '<button class="btn" id="imp-btn">Import CSV / XLSX</button>' : ""}${can("data.export") ? `<a class="btn" id="exp-btn">Export CSV</a>` : ""}</div></div>
  <div class="card" style="margin-bottom:16px"><form id="lf" class="row"><input name="q" value="${esc(q)}" placeholder="Search name, email, title" class="grow" style="max-width:320px">
    <select name="stage" style="width:auto"><option value="">All stages</option>${stages.map((s) => `<option ${s === stage ? "selected" : ""}>${s}</option>`).join("")}</select>
    <select name="t" style="width:auto"><option value="">Any temperature</option>${["hot", "warm", "cold"].map((s) => `<option ${s === temp ? "selected" : ""}>${s}</option>`).join("")}</select><button class="btn">Filter</button></form>
    <div class="chips" style="margin-top:10px">${Object.entries(r.stages).map(([k, v]) => `<a class="chip ${k === stage ? "on" : ""}" href="#/leads?stage=${encodeURIComponent(k)}">${esc(k)} · ${v}</a>`).join("")}</div></div>
  <div class="row" style="margin-bottom:10px">
    ${can("enrich.run") ? '<button class="btn sm" data-b="enrich">Enrich</button><button class="btn sm" data-b="verify">Verify</button>' : ""}
    ${can("campaigns.manage") ? '<button class="btn sm" data-b="add_to_campaign">Add to campaign</button><button class="btn sm" data-b="assign">Assign to me</button>' : ""}
    ${can("lists.manage") && lists.length ? `<select id="list-sel" style="width:auto" class="sm"><option value="">Add to list…</option>${lists.filter((l) => l.type !== "saved_search").map((l) => `<option value="${l.id}">${esc(l.name)}</option>`).join("")}</select>` : ""}
    ${can("suppression.manage") ? '<button class="btn sm danger" data-b="suppress">Suppress</button>' : ""}${can("data.delete") ? '<button class="btn sm danger" data-b="delete">Delete</button>' : ""}</div>
  ${peopleTable(r.results, { select: true })}`;
  bindRows(); bindSelectAll();
  on("#lf", "submit", (e) => { e.preventDefault(); const f = new FormData(e.target); location.hash = `#/leads?q=${encodeURIComponent(f.get("q"))}&stage=${encodeURIComponent(f.get("stage"))}&t=${f.get("t")}`; });
  on("#exp-btn", "click", async () => { try { const res = await api(`/app/leads-export?q=${encodeURIComponent(q)}&stage=${encodeURIComponent(stage)}`, { raw: true }); if (!res.ok) throw new Error("Export failed"); const blob = await res.blob(); const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "leads.csv"; a.click(); } catch (e) { fail(e); } });
  on("#imp-btn", "click", importModal);
  on("#list-sel", "change", async (e) => { const ids = selected(); if (!ids.length || !e.target.value) return; await api("/app/leads/bulk", { method: "POST", body: { person_ids: ids, action: "add_to_list", list_id: e.target.value } }).then(() => toast("Added to list")).catch(fail); });
  $$("[data-b]").forEach((b) => b.addEventListener("click", async () => {
    const ids = selected(); if (!ids.length) return toast("Select leads first.", true);
    const action = b.dataset.b;
    if (action === "add_to_campaign") return addToCampaignModal(ids);
    if ((action === "delete" || action === "suppress") && !confirm(`${action} ${ids.length} lead(s)?`)) return;
    setBusy(b, true);
    try { const x = await api("/app/leads/bulk", { method: "POST", body: { person_ids: ids, action, owner_id: S.me.user.id } }); toast(x.job_id ? "Job started" : "Done"); route(); } catch (e) { fail(e); setBusy(b, false); }
  }));
}

function importModal() {
  const m = modal(`<div class="row between"><h2>Import leads</h2><button class="btn sm" data-close>Close</button></div>
    <p class="muted small">Upload → column mapping → validation → deduplication → suppression check → preview → import. Bad rows never fail the whole file.</p>
    <div class="field"><input type="file" id="imp-file" accept=".csv,.xlsx"></div><div class="field"><label>Save as list (optional)</label><input id="imp-list" placeholder="e.g. Trade show leads Nov"></div><div id="imp-step"></div>`, { wide: true });
  let file = null;
  on("#imp-file", "change", async (e) => {
    file = e.target.files[0]; if (!file) return;
    const fd = new FormData(); fd.append("file", file);
    try {
      const p = await api("/app/import", { method: "POST", form: fd });
      $("#imp-step", m).innerHTML = `<h3>Column mapping (${p.rows} rows)</h3><div class="grid g3">${p.headers.map((h) => `<div><label>${esc(h)}</label><select data-h="${esc(h)}"><option value="">(custom field)</option>${p.fields.map((f) => `<option ${p.mapping[h] === f ? "selected" : ""}>${f}</option>`).join("")}</select></div>`).join("")}</div>
        <h3 style="margin-top:12px">Preview</h3><div class="tbl-wrap"><table><thead><tr>${p.headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${p.preview.map((r) => `<tr>${p.headers.map((h) => `<td class="small">${esc(r[h])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
        <button class="btn primary" id="imp-go" style="margin-top:12px">Import</button><div id="imp-res"></div>`;
      on("#imp-go", "click", async (ev) => {
        const mp = {}; $$("[data-h]", m).forEach((s) => { if (s.value) mp[s.dataset.h] = s.value; });
        const fd2 = new FormData(); fd2.append("file", file); fd2.append("mapping", JSON.stringify(mp)); fd2.append("commit", "true"); fd2.append("list_name", $("#imp-list", m).value);
        setBusy(ev.target, true, "Importing");
        try { const r = await api("/app/import", { method: "POST", form: fd2 }); const s = r.stats;
          $("#imp-res", m).innerHTML = `<div class="grid g3" style="margin-top:12px">${Object.entries(s).map(([k, v]) => `<div class="card kpi flat"><div class="l">${k.replace(/_/g, " ")}</div><div class="v">${fmt(v)}</div></div>`).join("")}</div>`;
          setBusy(ev.target, false); ev.target.remove(); toast("Import complete");
        } catch (err) { fail(err); setBusy(ev.target, false); }
      }, m);
    } catch (err) { fail(err); }
  }, m);
}

async function pageLead(id) {
  V().innerHTML = loading();
  const p = await api("/app/leads/" + id);
  const c = p.company_detail;
  V().innerHTML = `<div class="page-head"><div><a href="#/leads" class="small">← Leads</a><h1>${esc(p.full_name)} ${tempBadge(p.temperature)}</h1>
    <p class="muted">${esc(p.title)} ${c ? `at <a href="#/companies/${c.id}">${esc(c.name)}</a>` : ""} · ${esc(p.city)} ${esc(p.country)}</p></div>
    <div class="row">${can("notes.add") ? `<select id="stage-sel" style="width:auto">${["New", "Contacted", "Engaged", "Warm", "Hot", "Meeting Booked", "Meeting Completed", "Opportunity", "Won", "Lost", "Nurture", "Suppressed"].map((s) => `<option ${s === p.stage ? "selected" : ""}>${s}</option>`).join("")}</select>
      <button class="btn sm" data-fb="good">👍 Good lead</button><button class="btn sm" data-fb="bad">👎 Bad lead</button>` : badge(p.stage)}
      ${can("enrich.run") ? '<button class="btn sm" id="enrich-one">Enrich</button>' : ""}${can("meetings.book") ? '<button class="btn sm primary" id="book-one">Book meeting</button>' : ""}</div></div>
  <div class="grid g-side"><div class="stack">
    <div class="card"><div class="grid g2"><div><h3>Why this person?</h3><p>${esc(p.why_this_person)}</p></div><div><h3>Why now?</h3>${p.why_now.length ? `<ul class="small">${p.why_now.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : '<p class="muted small">No recent signals.</p>'}</div></div>
      <p><b>Recommended action:</b> ${esc(p.recommended_action)}</p></div>
    <div class="card"><h3>Conversation history (all channels)</h3>${p.messages.length ? p.messages.map((m) => msgBubble(m)).join("") : '<p class="muted">No outreach yet.</p>'}${p.conversation_ids[0] ? `<a href="#/inbox/${p.conversation_ids[0]}">Open in inbox →</a>` : ""}</div>
    <div class="card"><h3>Notes</h3>${can("notes.add") ? '<form id="note-form" class="row"><input id="note-in" class="grow" placeholder="Add a note"><button class="btn">Add</button></form>' : ""}${p.notes.map((n) => `<div class="small" style="margin-top:8px"><b>${esc(n.by)}</b> <span class="faint">${ago(n.at)}</span><div>${esc(n.body)}</div></div>`).join("")}</div>
  </div><div class="stack">
    <div class="card"><h3>Contact</h3><div class="kv"><div>Email</div><div>${esc(p.email || "—")} ${p.email ? badge(p.email_status, statusKind(p.email_status)) : ""}</div><div>Phone</div><div>${esc(p.phone || "—")}</div><div>LinkedIn</div><div class="small">${esc(p.linkedin_url || "—")}</div><div>ICP / Intent</div><div>${p.icp_score} / ${p.intent_score}</div><div>Source</div><div>${esc(p.source)}</div><div>CRM</div><div>${p.crm ? `${esc(p.crm.stage)} · ${esc(p.crm.system)} ${badge(p.crm.status)}` : "not synced"}</div></div>
      ${can("notes.add") ? `<hr><label><input type="checkbox" id="wa-opt" ${p.whatsapp_opt_in ? "checked" : ""}> WhatsApp opt-in recorded</label>` : ""}</div>
    <div class="card"><h3>Campaigns</h3>${p.campaigns.map((x) => `<div class="small">${esc(x.campaign)} ${badge(x.status)} <span class="faint">step ${x.step} ${esc(x.stop_reason)}</span></div>`).join("") || '<p class="muted small">Not in a campaign.</p>'}</div>
    <div class="card"><h3>Meetings</h3>${p.meetings.map((x) => `<div class="small">${esc(x.title)} · ${localDt(x.start_at)} ${badge(x.status, statusKind(x.status))}</div>`).join("") || '<p class="muted small">None.</p>'}</div>
    <div class="card"><h3>Source transparency</h3>${p.provenance.map((o) => `<div class="small"><b>${esc(o.field)}</b>: ${esc(o.provider)} · ${dt(o.observed_at)} · ${Math.round(o.confidence * 100)}%</div>`).join("") || '<p class="muted small">No observations.</p>'}</div>
  </div></div>`;
  on("#stage-sel", "change", (e) => api("/app/leads/" + id, { method: "PATCH", body: { stage: e.target.value } }).then(() => toast("Stage updated")).catch(fail));
  $$("[data-fb]").forEach((b) => b.addEventListener("click", () => api("/app/leads/" + id, { method: "PATCH", body: { feedback: b.dataset.fb } }).then(() => toast("Feedback recorded - it will influence future scoring")).catch(fail)));
  on("#note-form", "submit", async (e) => { e.preventDefault(); await api(`/app/leads/${id}/notes`, { method: "POST", body: { body: $("#note-in").value } }).then(() => pageLead(id)).catch(fail); });
  on("#wa-opt", "change", (e) => api("/app/leads/" + id, { method: "PATCH", body: { whatsapp_opt_in: e.target.checked } }).then(() => toast("Consent updated")).catch(fail));
  on("#enrich-one", "click", async (e) => { setBusy(e.target, true); try { await api("/app/leads/bulk", { method: "POST", body: { person_ids: [id], action: "enrich", phone: true } }); pageLead(id); } catch (err) { fail(err); setBusy(e.target, false); } });
  on("#book-one", "click", () => bookModal(id));
}

async function bookModal(personId) {
  const slots = await api("/app/meetings/slots");
  modal(`<h2>Book a meeting</h2><p class="muted small">Free slots from your booking availability (Settings → Autonomy & policy).</p><div class="chips" id="slot-chips">${slots.map((s) => `<span class="chip" data-s="${s}">${localDt(s)}</span>`).join("")}</div><div class="row" style="margin-top:16px"><button class="btn" data-close>Cancel</button></div>`);
  $$("[data-s]").forEach((c) => c.addEventListener("click", async () => { try { await api("/app/meetings", { method: "POST", body: { person_id: personId, start_at: c.dataset.s } }); toast("Meeting booked"); closeModal(); route(); } catch (e) { fail(e); } }));
}

function msgBubble(m) {
  return `<div class="msg ${m.direction}"><div class="meta">${m.direction === "outbound" ? "→" : "←"} ${esc(m.channel)} · ${dt(m.at)} · ${badge(m.status, m.status === "blocked" || m.status === "failed" ? "b-bad" : "")}${m.ai_generated ? " · AI " + esc(m.ai_model || "") : ""}${(m.evidence || []).some((e) => e.simulated) ? " · simulated prospect" : ""}</div>${m.subject ? `<b>${esc(m.subject)}</b>\n` : ""}${esc(m.content)}${m.error ? `<div class="small" style="color:var(--bad)">${esc(m.error)}</div>` : ""}</div>`;
}

/* ----- Missions ----- */
async function pageMissions(params) {
  V().innerHTML = loading();
  const list = await api("/app/missions");
  V().innerHTML = `<div class="page-head"><div><h1>GTM Missions</h1><p class="muted">A mission is your autonomous sales objective. The agent builds and manages campaigns under it.</p></div>${can("missions.manage") ? '<button class="btn primary" id="new-m">+ New mission</button>' : ""}</div>
  ${list.length ? `<div class="grid g3">${list.map((x) => `<div class="card click" style="cursor:pointer" data-href="#/missions/${x.id}"><div class="row between"><h2 style="margin:0">${esc(x.name)}</h2>${badge(x.status, statusKind(x.status))}</div>
    <p class="muted small" style="margin-top:6px">${esc(x.objective)} · ${esc(x.autonomy_mode.replace("_", " "))}</p>
    <div class="row small">${x.channels.map((c) => badge(c, "b-accent")).join("")}</div>
    <div class="grid g3" style="margin-top:12px"><div class="kpi"><div class="l">Campaigns</div><div class="v" style="font-size:18px">${x.campaigns}</div></div><div class="kpi"><div class="l">Hot</div><div class="v" style="font-size:18px">${x.hot}</div></div><div class="kpi"><div class="l">Meetings</div><div class="v" style="font-size:18px">${x.meetings}/${x.meeting_goal}</div></div></div>
    <div class="small faint">Budget ${fmt(x.budget_monthly)} GTU/mo · spent ${fmt(x.spent, 1)}</div></div>`).join("")}</div>`
    : empty("Your GTM engine is not running yet.", "Describe what you sell and who you want to reach. The agent turns it into a mission with campaigns.", can("missions.manage") ? '<button class="btn primary" id="new-m2">Create your first mission</button>' : "")}`;
  bindRows();
  on("#new-m", "click", missionModal); on("#new-m2", "click", missionModal);
  if (params.get("new") && can("missions.manage")) missionModal();
}

function missionModal() {
  const m = modal(`<div class="row between"><h2>New GTM mission</h2><button class="btn sm" data-close>Close</button></div>
    <p class="muted small">Tell the AI what you want. Example: “Find Indian companies with 50-500 employees likely to need CRM implementation, prioritise companies hiring sales teams, target founders and sales heads, email and WhatsApp, 20,000 monthly budget.”</p>
    <div class="field"><textarea id="m-ins" placeholder="Describe the mission…"></textarea></div>
    <div class="row"><button class="btn" id="m-plan">Preview plan</button></div><div id="m-plan-out" style="margin-top:12px"></div>`, { wide: true });
  on("#m-plan", "click", async (e) => {
    setBusy(e.target, true, "Planning");
    try {
      const p = await api("/app/missions/plan", { method: "POST", body: { instruction: $("#m-ins").value } });
      $("#m-plan-out", m).innerHTML = `<div class="card flat"><div class="grid g2"><div class="field"><label>Mission name</label><input id="m-name" value="${esc(p.name)}"></div><div class="field"><label>Monthly budget (GTU)</label><input id="m-budget" type="number" value="${p.budget_monthly}"></div></div>
        <div class="kv"><div>Target</div><div>${esc(p.summary)}</div><div>Personas</div><div>${p.personas.map((x) => badge(x)).join(" ")}</div><div>Signals to prioritise</div><div>${p.signals.map((x) => badge(x, "b-info")).join(" ") || "—"}</div></div>
        <div class="field" style="margin-top:10px"><label>Channels</label><div class="chips">${["email", "whatsapp", "linkedin", "sms", "voice"].map((c) => `<span class="chip ${p.channels.includes(c) ? "on" : ""}" data-ch="${c}">${c}</span>`).join("")}</div></div>
        <div class="grid g2"><div class="field"><label>Autonomy</label><select id="m-auto"><option value="full_autonomous">Full Autonomous (Level 4)</option><option value="autonomous">Autonomous</option><option value="copilot">Copilot</option><option value="assistant">Assistant (propose only)</option></select></div>
        <div class="field"><label>Meeting goal / month</label><input id="m-goal" type="number" value="10"></div></div>
        <label><input type="checkbox" id="m-aud" checked> Build audiences now (finds companies + decision makers)</label>
        <div class="row" style="margin-top:12px"><button class="btn primary" id="m-create">Create mission</button></div></div>`;
      $$("[data-ch]", m).forEach((c) => c.addEventListener("click", () => c.classList.toggle("on")));
      on("#m-create", "click", async (ev) => {
        setBusy(ev.target, true, "Creating mission & audiences");
        try {
          const r = await api("/app/missions", { method: "POST", body: { instruction: $("#m-ins").value, name: $("#m-name").value, budget_monthly: Number($("#m-budget").value), channels: $$("[data-ch].on", m).map((c) => c.dataset.ch), autonomy_mode: $("#m-auto").value, meeting_goal: Number($("#m-goal").value), auto_build_audience: $("#m-aud").checked } });
          closeModal(); toast(`Mission created with ${r.campaign_ids.length} campaigns`); location.hash = "#/missions/" + r.mission.id;
        } catch (err) { fail(err); setBusy(ev.target, false); }
      }, m);
    } catch (err) { fail(err); } setBusy(e.target, false);
  }, m);
}

async function pageMission(id) {
  V().innerHTML = loading();
  const x = await api("/app/missions/" + id);
  V().innerHTML = `<div class="page-head"><div><a href="#/missions" class="small">← Missions</a><h1>${esc(x.name)}</h1><p class="muted">${esc(x.instruction || x.objective)}</p></div>
    <div class="row">${badge(x.autonomy_mode.replace("_", " "), "b-accent")}${can("missions.manage") ? '<button class="btn" id="gen-c">Generate more campaigns</button>' : ""}</div></div>
  <div class="grid g4">${[["Budget / month", fmt(x.budget_monthly) + " GTU"], ["Spent", fmt(x.spent, 1) + " GTU"], ["Hot leads", x.hot], ["Meetings", `${x.meetings} / ${x.meeting_goal}`]].map(([l, v]) => `<div class="card kpi"><div class="l">${l}</div><div class="v">${v}</div></div>`).join("")}</div>
  <div class="grid g-side" style="margin-top:16px"><div class="card"><h2>Campaign portfolio</h2>${campaignCards(x.campaign_list)}</div>
  <div class="stack"><div class="card"><h3>ICP</h3>${critChips(x.icp)}<h3 style="margin-top:12px">Personas</h3>${x.personas.map((p) => badge(p)).join(" ")}<h3 style="margin-top:12px">Channels</h3>${x.channels.map((p) => badge(p, "b-accent")).join(" ")}${x.memory.length ? `<h3 style="margin-top:12px">Memory</h3><ul class="small">${x.memory.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>` : ""}</div>
  <div class="card"><h3>Agent decisions</h3>${decisionTable(x.decisions)}</div></div></div>`;
  bindRows();
  on("#gen-c", "click", async () => { try { await api(`/app/missions/${id}/campaigns`, { method: "POST" }); pageMission(id); } catch (e) { fail(e); } });
}

function campaignCards(list) {
  if (!list.length) return '<p class="muted">No campaigns yet.</p>';
  return `<div class="tbl-wrap"><table><thead><tr><th>Campaign</th><th>Status</th><th>Leads</th><th>Sent</th><th>Replies</th><th>Hot</th><th>Meetings</th><th>Spend</th><th>Cost/meeting</th></tr></thead><tbody>${list.map((c) => `<tr class="click" data-href="#/campaigns/${c.id}"><td><b>${esc(c.name)}</b><div class="small faint">${c.channels.join(" · ")}</div></td><td>${badge(c.status_label, statusKind(c.status_label))}</td><td>${c.stats.leads}</td><td>${c.stats.sent}</td><td>${c.stats.replies} <span class="faint small">${c.stats.reply_rate}%</span></td><td>${c.stats.hot}</td><td>${c.stats.meetings}</td><td>${fmt(c.stats.spend, 1)}</td><td>${c.stats.cost_per_meeting ?? "—"}</td></tr>`).join("")}</tbody></table></div>`;
}

/* ----- Campaigns ----- */
async function pageCampaigns() {
  V().innerHTML = loading();
  const list = await api("/app/campaigns");
  V().innerHTML = `<div class="page-head"><div><h1>Campaigns</h1><p class="muted">Statuses: Scaling · Running · Optimizing · Paused · Needs attention</p></div>${can("campaigns.manage") ? '<button class="btn primary" id="new-c">+ New campaign</button>' : ""}</div>
    ${list.length ? `<div class="card">${campaignCards(list)}</div>` : empty("No campaigns yet", "Campaigns are usually generated from a mission, or create one directly.", can("campaigns.manage") ? '<button class="btn primary" id="new-c2">Create campaign</button>' : "")}`;
  bindRows();
  const open = () => {
    const m = modal(`<h2>New campaign</h2><p class="muted small">“Create a campaign for startup founders in Tamil Nadu.” The AI determines filters, titles, geography and sequence.</p>
      <div class="field"><label>Describe the campaign</label><textarea id="c-ins"></textarea></div><div class="grid g2"><div class="field"><label>Budget (GTU)</label><input id="c-b" type="number" value="1000"></div><div class="field"><label>Daily send limit</label><input id="c-l" type="number" value="50"></div></div>
      <div class="field"><label>Channels</label><div class="chips">${["email", "whatsapp", "linkedin", "sms", "voice"].map((c) => `<span class="chip ${c === "email" ? "on" : ""}" data-ch="${c}">${c}</span>`).join("")}</div></div>
      <div class="row"><button class="btn primary" id="c-go">Create</button><button class="btn" data-close>Cancel</button></div>`);
    $$("[data-ch]", m).forEach((c) => c.addEventListener("click", () => c.classList.toggle("on")));
    on("#c-go", "click", async (e) => { setBusy(e.target, true); try { const c = await api("/app/campaigns", { method: "POST", body: { instruction: $("#c-ins").value, budget_total: Number($("#c-b").value), daily_send_limit: Number($("#c-l").value), channels: $$("[data-ch].on", m).map((c) => c.dataset.ch) } }); closeModal(); location.hash = "#/campaigns/" + c.id; } catch (err) { fail(err); setBusy(e.target, false); } }, m);
  };
  on("#new-c", "click", open); on("#new-c2", "click", open);
}

async function pageCampaign(id) {
  V().innerHTML = loading();
  const c = await api("/app/campaigns/" + id);
  const s = c.stats;
  const mg = can("campaigns.manage");
  V().innerHTML = `<div class="page-head"><div><a href="#/campaigns" class="small">← Campaigns</a><h1>${esc(c.name)} ${badge(c.status_label, statusKind(c.status_label))}</h1><p class="muted">${esc(c.objective)} · ${c.channels.join(" · ")} · ${esc(c.autonomy_mode.replace("_", " "))}</p></div>
    <div class="row">${mg ? `<button class="btn" id="aud-btn">Build audience</button><button class="btn" id="prev-btn">Review before launch</button>
      ${c.status === "RUNNING" ? '<button class="btn" id="run-btn">Run scheduler now</button><button class="btn" data-act="pause">Pause</button>' : c.status === "COMPLETED" || c.status === "ARCHIVED" ? "" : `<button class="btn primary" data-act="${c.status === "PAUSED" ? "resume" : "start"}">${c.status === "PAUSED" ? "Resume" : "Start campaign"}</button>`}` : ""}</div></div>
  ${c.status === "COMPLIANCE_PAUSED" ? '<div class="card" style="border-color:var(--bad);margin-bottom:16px"><b>Needs attention:</b> every lead in this campaign was blocked by policy (suppressed, already contacted by another campaign, or missing a sendable contact). Build a fresh audience or adjust the policy.</div>' : ""}
  <div class="grid g4">${[["Leads", s.leads], ["Sent", s.sent], ["Replies", `${s.replies} (${s.reply_rate}%)`], ["Hot leads", s.hot], ["Meetings", s.meetings], ["Spend", `${fmt(s.spend, 1)} / ${fmt(c.budget_total)} GTU`], ["Cost / lead", s.cost_per_lead ?? "—"], ["Blocked by policy", s.blocked]].map(([l, v]) => `<div class="card kpi"><div class="l">${l}</div><div class="v" style="font-size:20px">${v}</div></div>`).join("")}</div>
  <div class="grid g3" style="margin-top:16px">
    <div class="card"><h3>Configuration</h3><div class="kv small"><div>Audience</div><div>${critChips(c.audience)}</div><div>Target roles</div><div>${c.target_roles.map((r) => badge(r)).join(" ")}</div><div>Daily budget</div><div>${fmt(c.budget_daily, 1)} GTU</div><div>Daily send cap</div><div>${c.daily_send_limit}</div><div>Strategy</div><div>${esc(Object.entries(c.messaging_strategy || {}).map(([k, v]) => `${k}: ${v}`).join(", "))}</div></div>
      <h3 style="margin-top:12px">Sequence</h3>${c.sequence.map((st, i) => `<div class="row small" style="margin-bottom:4px"><span class="pill-step">${i + 1}</span> Day ${st.day} · ${badge(st.channel, "b-accent")} ${esc(st.type.replace("_", " "))}</div>`).join("")}</div>
    <div class="card"><h3>✦ AI insight</h3><p>${esc(c.insight.summary)}</p>${c.insight.top_segment ? `<div class="kv small"><div>Top segment</div><div>${esc(c.insight.top_segment)}</div><div>Best title</div><div>${esc(c.insight.best_title)}</div><div>Best channel</div><div>${esc(c.insight.best_channel)}</div><div>Best opening</div><div>${esc(c.insight.best_opening)}</div><div>Best CTA</div><div>${esc(c.insight.best_cta)}</div></div>` : ""}
      <h3 style="margin-top:12px">CTA experiment (A/B)</h3><p class="small muted">Only the CTA differs between variants, so causality stays clear.</p>${Object.entries(c.variants).map(([v, x]) => `<div class="small">Variant ${v}: ${x.leads} leads · ${x.replied} replied · <b>${x.reply_rate}%</b></div>`).join("")}</div>
    <div class="card"><h3>Agent decisions</h3>${c.decisions.length ? c.decisions.slice(0, 8).map((d) => `<div class="small" style="margin-bottom:6px"><b>${esc(d.action.replace(/_/g, " "))}</b> ${badge(d.decision, d.decision === "executed" ? "b-ok" : "b-warn")}<div class="muted">${esc(d.reason)}</div></div>`).join("") : '<p class="muted small">No decisions yet.</p>'}</div>
  </div>
  <div class="card" style="margin-top:16px"><h2>Leads (${c.leads.length})</h2>${c.leads.length ? `<div class="tbl-wrap"><table><thead><tr><th>Name</th><th>Company</th><th>Status</th><th>Step</th><th>Variant</th><th>Temp.</th><th>Next action</th><th>Stop reason</th></tr></thead><tbody>${c.leads.map((l) => `<tr class="click" data-href="#/leads/${l.id}"><td><b>${esc(l.name)}</b><div class="small faint">${esc(l.title)}</div></td><td>${esc(l.company)}</td><td>${badge(l.status)}</td><td>${l.step}/${c.sequence.length}</td><td>${l.variant}</td><td>${tempBadge(l.temperature)}</td><td class="small">${l.next_action_at ? dt(l.next_action_at) : "—"}</td><td class="small faint">${esc(l.stop_reason)}</td></tr>`).join("")}</tbody></table></div>` : `<p class="muted">No audience yet. Click “Build audience”.</p>`}</div>`;
  bindRows();
  on("#aud-btn", "click", async (e) => { setBusy(e.target, true, "Finding companies & people"); try { const r = await api(`/app/campaigns/${id}/audience`, { method: "POST", body: { limit: 40 } }); toast(`Added ${r.added} people from ${r.companies} companies` + (r.skipped_already_in_play ? ` · skipped ${r.skipped_already_in_play} already in another campaign` : "")); pageCampaign(id); } catch (err) { fail(err); setBusy(e.target, false); } });
  on("#prev-btn", "click", () => previewModal(id));
  on("#run-btn", "click", async (e) => { setBusy(e.target, true); try { const r = await api(`/app/campaigns/${id}/run-now`, { method: "POST" }); toast(`Sent ${r.sent} message(s)`); pageCampaign(id); } catch (err) { fail(err); setBusy(e.target, false); } });
  $$("[data-act]").forEach((b) => b.addEventListener("click", async () => { try { await api(`/app/campaigns/${id}/${b.dataset.act}`, { method: "POST" }); toast("Campaign " + b.dataset.act + (b.dataset.act.endsWith("e") ? "d" : "ed")); pageCampaign(id); } catch (e) { fail(e); } }));
}

async function previewModal(id) {
  const m = modal(`<div class="row between"><h2>Review before launch</h2><button class="btn sm" data-close>Close</button></div><div id="pv">${loading()}</div>`, { wide: true });
  try {
    const p = await api(`/app/campaigns/${id}/preview`);
    $("#pv", m).innerHTML = `<div class="grid g4">${[["Companies", p.companies], ["People", p.people], ["Est. spend", fmt(p.estimated_spend_gtu, 1) + " GTU"], ["Capacity / day", p.expected_capacity_per_day]].map(([l, v]) => `<div class="card kpi flat"><div class="l">${l}</div><div class="v" style="font-size:20px">${v}</div></div>`).join("")}</div>
      <h3 style="margin-top:16px">Top segments</h3><div class="chips">${p.top_segments.map((s) => `<span class="chip">${esc(s.segment)} · ${s.companies}</span>`).join("")}</div>
      <h3 style="margin-top:16px">Risks</h3><ul>${p.risks.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      <h3 style="margin-top:16px">Example messages</h3>${p.examples.map((x) => `<div class="card flat" style="margin-bottom:10px"><div class="small muted">To ${esc(x.to)} · ${esc(x.title)} · ${esc(x.company)} · personalization L${x.personalization_level} · ${esc(x.model)} · offer: ${esc(x.offer)}</div><b>${esc(x.subject)}</b><div style="white-space:pre-wrap">${esc(x.body)}</div>${x.evidence.length ? `<div class="small faint" style="margin-top:6px">Evidence: ${x.evidence.map((e) => esc(e.fact + " (" + e.source + ")")).join("; ")}</div>` : ""}</div>`).join("") || '<p class="muted">Build an audience to see examples.</p>'}`;
  } catch (e) { $("#pv", m).innerHTML = `<p style="color:var(--bad)">${esc(e.message)}</p>`; }
}

/* ----- Inbox (PRD 76-77) ----- */
async function pageInbox(convId, params) {
  const filter = params.get("f") || "all";
  V().innerHTML = loading();
  const r = await api("/app/inbox?filter=" + filter);
  const filters = [["all", "All"], ["need_reply", "Need reply"], ["hot", "Hot"], ["meetings", "Meetings"], ["human_review", `Human review (${r.counts.human_review})`], ["ai_handled", "AI handled"], ["unsubscribe", "Unsubscribe"]];
  const cur = convId || (r.results[0] && r.results[0].id);
  V().innerHTML = `<div class="page-head"><div><h1>Inbox</h1><p class="muted">AI-powered conversation operations: intent, sentiment, hot-lead detection, auto-replies under policy, human takeover.</p></div></div>
  <div class="chips" style="margin-bottom:12px">${filters.map(([k, l]) => `<a class="chip ${k === filter ? "on" : ""}" href="#/inbox?f=${k}">${esc(l)}</a>`).join("")}</div>
  ${r.results.length ? `<div class="inbox"><div>${r.results.map((c) => `<div class="list-item ${c.id === cur ? "active" : ""}" data-conv="${c.id}"><div class="row between"><b>${esc(c.person ? c.person.name : "")}</b><span class="small faint">${ago(c.last_message_at)}</span></div>
      <div class="small muted">${esc(c.company ? c.company.name : "")}</div><div class="small" style="margin-top:2px">${c.last_direction === "inbound" ? "← " : "→ "}${esc(c.last_message)}</div>
      <div class="row" style="margin-top:4px;gap:4px">${c.intent ? badge(c.intent, c.temperature === "hot" ? "b-hot" : "") : ""}${c.needs_review ? badge("review", "b-warn") : ""}${c.ai_mode === "human" ? badge("human", "b-info") : ""}</div></div>`).join("")}</div>
    <div class="pad" id="conv-pane">${loading()}</div><div class="pad" id="intel-pane"></div></div>`
    : empty("No conversations here yet.", "When campaigns send messages and prospects reply, conversations appear here.")}`;
  $$("[data-conv]").forEach((el) => el.addEventListener("click", () => (location.hash = `#/inbox/${el.dataset.conv}?f=${filter}`)));
  if (cur) loadConversation(cur);
}

async function loadConversation(id) {
  const c = await api("/app/inbox/" + id);
  const p = c.person || {};
  $("#conv-pane").innerHTML = `<div class="row between" style="margin-bottom:12px"><div><h2 style="margin:0">${esc(p.name)}</h2><div class="small muted">${esc(p.title)} · ${esc(c.company ? c.company.name : "")}</div></div>
    <div class="row">${can("conversations.takeover") ? (c.ai_mode === "ai" ? '<button class="btn sm" id="takeover">Take over</button>' : '<button class="btn sm" id="return-ai">Return to agent</button>') : ""}</div></div>
    ${c.ai_mode === "human" ? '<p class="small" style="color:var(--info)">You own this conversation. AI auto-send is stopped; the agent still drafts suggestions.</p>' : ""}
    ${c.messages.map(msgBubble).join("")}
    ${c.needs_review ? `<div class="card flat" style="border-color:var(--warn)"><b>⚠ Human review:</b> ${esc(c.review_reason)}</div>` : ""}
    ${can("conversations.reply") ? `<form id="reply-form" style="margin-top:12px"><label>${c.draft_reply ? "AI draft (edit before sending)" : "Reply"}</label><textarea id="reply-body" rows="6">${esc(c.draft_reply)}</textarea>
      <div class="row" style="margin-top:8px">${c.draft_reply && can("exceptions.approve") ? '<button type="button" class="btn primary" id="approve">Approve & send draft</button>' : ""}<button class="btn ${c.draft_reply ? "" : "primary"}">Send</button>
      <span class="grow"></span>${S.me.demo_mode ? '<button type="button" class="btn sm ghost" id="sim">Simulate prospect reply (test mode)</button>' : ""}</div></form>` : ""}`;
  $("#intel-pane").innerHTML = `<h3>Lead intelligence</h3><div class="kv small"><div>Company</div><div>${esc(c.company ? c.company.name : "—")}</div><div>ICP score</div><div>${c.company ? fmt(c.company.icp) : "—"}</div><div>Person</div><div>${esc(p.title)}</div><div>Stage</div><div>${badge(p.stage)}</div>
    <div>Intent</div><div>${c.intent ? badge(c.intent, c.temperature === "hot" ? "b-hot" : "") : "—"}</div><div>Lead score</div><div>${c.lead_score}</div><div>Sentiment</div><div>${esc(c.sentiment)}</div><div>Temperature</div><div>${tempBadge(c.temperature)}</div><div>Channel</div><div>${esc(c.channel)}</div><div>AI mode</div><div>${esc(c.ai_mode)}</div></div>
    ${c.summary ? `<h3 style="margin-top:14px">${c.temperature === "hot" ? "Why hot" : "AI summary"}</h3><p class="small">${esc(c.summary)}</p>` : ""}
    ${c.next_action ? `<h3 style="margin-top:14px">Recommended</h3><p class="small">${esc(c.next_action)}</p>` : ""}
    ${(c.memory && c.memory.proposed_slots) ? `<h3 style="margin-top:14px">Proposed slots</h3>${c.memory.proposed_slots.map((s) => `<div class="small">${localDt(s)}</div>`).join("")}` : ""}
    ${c.meetings.length ? `<h3 style="margin-top:14px">Meetings</h3>${c.meetings.map((x) => `<div class="small">${localDt(x.start_at)} ${badge(x.status, statusKind(x.status))}</div>`).join("")}` : ""}
    <div class="row" style="margin-top:14px"><a class="btn sm" href="#/leads/${p.id}">Lead profile</a>${can("meetings.book") ? '<button class="btn sm" id="book-c">Book meeting</button>' : ""}</div>`;
  on("#takeover", "click", () => api(`/app/inbox/${id}/takeover`, { method: "POST" }).then(() => loadConversation(id)).catch(fail));
  on("#return-ai", "click", () => api(`/app/inbox/${id}/return-to-ai`, { method: "POST" }).then(() => loadConversation(id)).catch(fail));
  on("#book-c", "click", () => bookModal(p.id));
  on("#approve", "click", async (e) => { setBusy(e.target, true); try { await api(`/app/inbox/${id}/approve`, { method: "POST", body: { body: $("#reply-body").value } }); toast("Sent"); loadConversation(id); } catch (err) { fail(err); setBusy(e.target, false); } });
  on("#reply-form", "submit", async (e) => { e.preventDefault(); const b = $("#reply-body").value.trim(); if (!b) return; try { await api(`/app/inbox/${id}/reply`, { method: "POST", body: { body: b } }); toast("Sent"); loadConversation(id); } catch (err) { fail(err); } });
  on("#sim", "click", () => {
    const m = modal(`<h2>Simulate a prospect reply</h2><p class="muted small">Sandbox testing for the reply agent. Pick a sample or write your own.</p>
      <div class="chips">${["Sounds interesting. Can we talk Friday afternoon?", "Please send pricing.", "We're currently evaluating CRM solutions. What's the implementation timeline?", "Not interested, thanks.", "I'm out of office until 15 December.", "Speak to Priya Nair in our operations team, she handles this.", "STOP", "Can you share your SOC 2 report and sign our MSA?", "Tuesday 11am works for me."].map((t) => `<span class="chip" data-t="${esc(t)}">${esc(t)}</span>`).join("")}</div>
      <div class="field" style="margin-top:12px"><textarea id="sim-t"></textarea></div><div class="row"><button class="btn primary" id="sim-go">Send as prospect</button><button class="btn" data-close>Cancel</button></div><div id="sim-res"></div>`);
    $$("[data-t]", m).forEach((c) => c.addEventListener("click", () => ($("#sim-t", m).value = c.dataset.t)));
    on("#sim-go", "click", async (e) => { setBusy(e.target, true); try { const r = await api(`/app/inbox/${id}/simulate-reply`, { method: "POST", body: { text: $("#sim-t", m).value } }); closeModal(); toast(`Classified ${r.classification.intent} (${Math.round(r.classification.confidence * 100)}%) · ${r.actions.join(", ") || (r.needs_review ? "sent to human review" : "no action")}`); loadConversation(id); } catch (err) { fail(err); setBusy(e.target, false); } }, m);
  });
}

/* ----- Meetings ----- */
async function pageMeetings() {
  V().innerHTML = loading();
  const [list, slots] = await Promise.all([api("/app/meetings"), api("/app/meetings/slots")]);
  const up = list.filter((x) => x.status === "booked");
  V().innerHTML = `<div class="page-head"><div><h1>Meetings</h1><p class="muted">Native booking with your availability. The booking agent proposes slots, confirms, creates the event and updates the CRM.</p></div></div>
  <div class="grid g-side"><div class="card">${list.length ? `<div class="tbl-wrap"><table><thead><tr><th>When (workspace time)</th><th>Meeting</th><th>Status</th><th>Booked by</th><th></th></tr></thead><tbody>${list.map((x) => `<tr><td>${localDt(x.start_at)}<div class="small faint">${esc(x.timezone)}</div></td><td><b>${esc(x.title)}</b><div class="small faint">${esc(x.location)}</div></td><td>${badge(x.status, statusKind(x.status))}</td><td class="small">${x.booked_by === "agent" ? "✦ AI agent" : "user"}</td>
      <td>${can("meetings.book") && x.status === "booked" ? `<select data-m="${x.id}" style="width:auto" class="sm"><option value="">Update…</option><option value="completed">Completed</option><option value="no_show">No-show</option><option value="cancelled">Cancel</option></select>` : ""}</td></tr>`).join("")}</tbody></table></div>`
    : '<div class="empty"><h2>No meetings yet</h2><p>When prospects reply with interest, the agent books meetings here.</p></div>'}</div>
  <div class="stack"><div class="card kpi"><div class="l">Upcoming</div><div class="v">${up.length}</div></div><div class="card"><h3>Next free slots</h3>${slots.map((s) => `<div class="small">${localDt(s)}</div>`).join("")}<p class="small faint" style="margin-top:8px">Change hours in Settings → Autonomy & policy.</p></div></div></div>`;
  $$("[data-m]").forEach((s) => s.addEventListener("change", () => s.value && api("/app/meetings/" + s.dataset.m, { method: "PATCH", body: { status: s.value } }).then(() => { toast("Updated"); pageMeetings(); }).catch(fail)));
}

async function pageLinkedIn() {
  V().innerHTML = loading();
  const list = await api("/app/linkedin-tasks");
  V().innerHTML = `<div class="page-head"><div><h1>LinkedIn Task Assistant</h1><p class="muted">The platform never scrapes or automates LinkedIn without approved API access. Instead, the agent queues tasks with a reason and a suggested message, and records the outcome.</p></div></div>
  ${list.length ? `<div class="grid g2">${list.map((t) => `<div class="card"><div class="row between"><b>${esc(t.action === "connect" ? "Connect with" : t.action)} ${esc(t.person ? t.person.name : "")}</b>${badge(t.status, t.status === "completed" ? "b-ok" : t.status === "skipped" ? "" : "b-info")}</div>
    <div class="small muted">${esc(t.person ? t.person.title : "")}</div><p class="small"><b>Reason:</b> ${esc(t.reason)}</p><div class="small" style="white-space:pre-wrap;background:var(--surface-2);padding:8px;border-radius:6px">${esc(t.message)}</div>
    ${t.status === "open" && can("conversations.reply") ? `<div class="row" style="margin-top:8px"><button class="btn sm" data-copy="${esc(t.message)}">Copy message</button><button class="btn sm primary" data-t="${t.id}" data-s="completed">Mark completed</button><button class="btn sm" data-t="${t.id}" data-s="skipped">Skip</button></div>` : ""}</div>`).join("")}</div>`
    : empty("No LinkedIn tasks", "Add LinkedIn to a campaign's channels and the agent will create tasks here.")}`;
  $$("[data-copy]").forEach((b) => b.addEventListener("click", () => navigator.clipboard.writeText(b.dataset.copy).then(() => toast("Copied"))));
  $$("[data-t]").forEach((b) => b.addEventListener("click", () => api("/app/linkedin-tasks/" + b.dataset.t, { method: "PATCH", body: { status: b.dataset.s } }).then(pageLinkedIn).catch(fail)));
}

async function pageAnalytics() {
  V().innerHTML = loading();
  const d = await api("/app/analytics/dashboard");
  const max = Math.max(1, ...d.daily.map((x) => x.replies + x.hot + x.meetings));
  V().innerHTML = `<div class="page-head"><div><h1>Analytics</h1><p class="muted">North star: qualified meetings per month. Secondary: cost per qualified meeting.</p></div></div>
  <div class="grid g4">${Object.entries(d.kpis).map(([k, v]) => `<div class="card kpi"><div class="l">${k.replace(/_/g, " ")}</div><div class="v" style="font-size:20px">${v ?? "—"}</div></div>`).join("")}</div>
  <div class="grid g2" style="margin-top:16px"><div class="card"><h2>Funnel</h2>${funnel(d.funnel)}</div>
  <div class="card"><h2>Replies, hot leads & meetings (14 days)</h2><div class="spark">${d.daily.map((x) => `<div title="${x.date}: ${x.replies} replies, ${x.hot} hot, ${x.meetings} meetings" style="height:${(100 * (x.replies + x.hot + x.meetings)) / max}%"></div>`).join("")}</div><div class="row between small faint"><span>${d.daily[0].date}</span><span>${d.daily[13].date}</span></div></div></div>
  <div class="card" style="margin-top:16px"><h2>Campaign performance</h2>${campaignCards(d.campaigns)}</div>`;
  bindRows();
}

/* ----- Integrations: admin-held credential vault ----- */
async function pageIntegrations() {
  V().innerHTML = loading();
  const [secrets, status, keys, hooks, boxes] = await Promise.all([api("/app/integrations/secrets"), api("/app/integrations/status"),
    can("apikeys.manage") ? api("/app/api-keys") : [], can("webhooks.manage") ? api("/app/webhooks") : null, api("/app/mailboxes")]);
  const groups = {};
  secrets.forEach((s) => (groups[s.group] = groups[s.group] || []).push(s));
  V().innerHTML = `<div class="page-head"><div><h1>Integrations</h1><p class="muted">API keys and credentials are held by admins only. They are encrypted at rest, never shown again after saving, never sent to the browser, and never placed in an AI prompt.</p></div></div>
  <div class="grid g4">${Object.entries(status).map(([k, v]) => `<div class="card kpi"><div class="l">${esc(k)}</div><div class="v" style="font-size:15px">${esc(String(v))}</div></div>`).join("")}</div>
  <div class="tabs" style="margin-top:16px"><a class="active" data-tab="vault">Credential vault</a><a data-tab="ai">AI model</a><a data-tab="email">Email (Resend)</a><a data-tab="keys">Public API keys</a><a data-tab="hooks">Webhooks</a></div>
  <div data-pane="vault">${Object.entries(groups).map(([g, items]) => `<div class="card" style="margin-bottom:12px"><h2>${esc(g)}</h2>${items.map((s) => `<form class="row" data-secret="${s.key}" style="margin-bottom:8px"><div style="width:260px"><b class="small">${esc(s.label)}</b><div class="mono faint">${esc(s.key)}</div></div>
      <input class="grow" type="${s.secret ? "password" : "text"}" placeholder="${s.configured ? "Configured " + esc(s.masked) + " - enter a new value to replace" : s.inherited_from_platform ? "Using platform default (set by AI Automation Labs)" : "Not set"}" autocomplete="off">
      <button class="btn sm primary">Save</button>${s.configured ? `<button type="button" class="btn sm danger" data-del="${s.key}">Remove</button>` : ""}${s.configured ? badge("configured", "b-ok") : s.inherited_from_platform ? badge("platform", "b-info") : ""}</form>`).join("")}</div>`).join("")}</div>
  <div data-pane="ai" hidden><div class="card"><h2>AI model routing</h2><p class="muted small">The LLM gateway is vendor-neutral. Without a key, the platform runs on deterministic rules (search parsing, reply classification, templates) so it keeps working. Default Anthropic model: claude-opus-5-5.</p>
    <form id="ai-form" class="grid g3"><div><label>Preferred vendor</label><select name="vendor"><option value="">Auto (first configured)</option><option value="anthropic">Anthropic</option><option value="openai">OpenAI</option></select></div><div><label>Model override (optional)</label><input name="model" placeholder="e.g. claude-opus-5-5"></div><div style="align-self:end"><button class="btn primary">Save</button></div></form></div></div>
  <div data-pane="email" hidden><div class="card" id="resend-card" style="margin-bottom:16px">${loading()}</div><div class="card"><h2>Sending mailboxes</h2><p class="small muted">Resend mailboxes send real email from a verified domain and start in WARMING with a conservative daily cap.  Bounces and spam complaints lower mailbox health, and degraded mailboxes stop receiving new work automatically.</p>
    <div class="tbl-wrap"><table><thead><tr><th>Address</th><th>Provider</th><th>Status</th><th>Health</th><th>Today</th><th>Total</th><th>Replies</th></tr></thead><tbody>${boxes.map((b) => `<tr><td>${esc(b.address)}</td><td>${esc(b.provider)}</td><td>${badge(b.status, statusKind(b.status))}</td><td>${b.health}</td><td>${b.sent_today}/${b.daily_limit}</td><td>${b.sent_total}</td><td>${b.replies}</td></tr>`).join("")}</tbody></table></div>
    <form id="mbx-form" class="row" style="margin-top:12px"><input name="address" type="email" placeholder="sender@yourdomain.com" style="max-width:300px" required><select name="provider" style="width:auto"><option value="resend">resend</option><option value="smtp">smtp (alternative)</option></select><input name="daily_limit" type="number" value="40" style="width:100px"><button class="btn">Add mailbox</button></form></div></div>
  <div data-pane="keys" hidden><div class="card"><h2>Public API keys</h2>${can("apikeys.manage") ? `<p class="small muted">Use <span class="mono">Authorization: Bearer &lt;key&gt;</span> or <span class="mono">X-API-Key</span> against <span class="mono">/api/v1</span>. Interactive docs: <a href="/docs" target="_blank">/docs</a>. Keys are stored hashed and shown once.</p>
    <form id="key-form" class="row"><input name="name" placeholder="Key name, e.g. Zapier" style="max-width:260px" required><select name="role" style="width:auto"><option value="gtm_manager">acts as GTM Manager</option><option value="sales_user">acts as Sales User</option><option value="viewer">acts as Viewer (read-only)</option><option value="admin">acts as Admin</option></select><button class="btn primary">Create key</button></form>
    <div class="tbl-wrap" style="margin-top:12px"><table><thead><tr><th>Name</th><th>Prefix</th><th>Role</th><th>Last used</th><th></th></tr></thead><tbody>${keys.map((k) => `<tr><td>${esc(k.name)}</td><td class="mono">${esc(k.prefix)}</td><td>${esc(k.role)}</td><td class="small">${k.last_used_at ? dt(k.last_used_at) : "never"}</td><td>${k.revoked ? badge("revoked", "b-bad") : `<button class="btn sm danger" data-revoke="${k.id}">Revoke</button>`}</td></tr>`).join("")}</tbody></table></div>` : lock("apikeys.manage")}</div></div>
  <div data-pane="hooks" hidden><div class="card"><h2>Webhooks</h2>${hooks ? `<p class="small muted">Signed with HMAC-SHA256 in <span class="mono">X-GTM-Signature</span>. Retries up to 5 times.</p>
    <form id="hook-form" class="row"><input name="url" placeholder="https://example.com/hooks/gtm" class="grow" required><select name="events" multiple size="4" style="width:260px">${hooks.events.map((e) => `<option>${e}</option>`).join("")}</select><button class="btn primary">Add endpoint</button></form>
    ${hooks.endpoints.map((e) => `<div class="row between small" style="margin-top:8px"><span class="mono">${esc(e.url)}</span><span>${esc(e.events.join(", ") || "all events")}</span><button class="btn sm danger" data-hook="${e.id}">Delete</button></div>`).join("")}
    <h3 style="margin-top:16px">Recent deliveries</h3>${hooks.deliveries.map((d) => `<div class="small">${esc(d.event)} ${badge(d.status, d.status === "delivered" ? "b-ok" : d.status === "failed" ? "b-bad" : "")} code ${d.code} · attempts ${d.attempts} · ${ago(d.created_at)}</div>`).join("") || '<p class="muted small">None yet.</p>'}` : lock("webhooks.manage")}</div></div>`;
  $$("[data-tab]").forEach((t) => t.addEventListener("click", () => { $$("[data-tab]").forEach((x) => x.classList.toggle("active", x === t)); $$("[data-pane]").forEach((p) => (p.hidden = p.dataset.pane !== t.dataset.tab)); if (t.dataset.tab === "email") loadResend(); }));
  $$("[data-secret]").forEach((f) => f.addEventListener("submit", async (e) => { e.preventDefault(); const v = $("input", f).value; if (!v) return toast("Enter a value", true); try { await api("/app/integrations/secrets", { method: "PUT", body: { key: f.dataset.secret, value: v } }); toast("Saved & encrypted"); pageIntegrations(); } catch (err) { fail(err); } }));
  $$("[data-del]").forEach((b) => b.addEventListener("click", async () => { if (!confirm("Remove this credential?")) return; await api("/app/integrations/secrets", { method: "PUT", body: { key: b.dataset.del, value: "" } }).then(pageIntegrations).catch(fail); }));
  on("#ai-form", "submit", async (e) => { e.preventDefault(); await api("/app/integrations/ai", { method: "PUT", body: Object.fromEntries(new FormData(e.target)) }).then(() => toast("Saved")).catch(fail); });
  on("#mbx-form", "submit", async (e) => { e.preventDefault(); const f = Object.fromEntries(new FormData(e.target)); f.daily_limit = Number(f.daily_limit); await api("/app/mailboxes", { method: "POST", body: f }).then(pageIntegrations).catch(fail); });
  on("#key-form", "submit", async (e) => { e.preventDefault(); try { const r = await api("/app/api-keys", { method: "POST", body: Object.fromEntries(new FormData(e.target)) }); modal(`<h2>API key created</h2><p class="muted">${esc(r.note)}</p><pre class="code">${esc(r.key)}</pre><div class="row" style="margin-top:12px"><button class="btn" id="copy-key">Copy</button><button class="btn primary" data-close>Done</button></div>`); on("#copy-key", "click", () => navigator.clipboard.writeText(r.key).then(() => toast("Copied"))); $(".overlay").addEventListener("click", () => pageIntegrations()); } catch (err) { fail(err); } });
  $$("[data-revoke]").forEach((b) => b.addEventListener("click", async () => { if (confirm("Revoke this key? Integrations using it will stop working.")) await api("/app/api-keys/" + b.dataset.revoke, { method: "DELETE" }).then(pageIntegrations).catch(fail); }));
  on("#hook-form", "submit", async (e) => { e.preventDefault(); const f = e.target; try { const r = await api("/app/webhooks", { method: "POST", body: { url: f.url.value, events: Array.from(f.events.selectedOptions).map((o) => o.value) } }); modal(`<h2>Webhook signing secret</h2><p class="muted">${esc(r.note)}</p><pre class="code">${esc(r.signing_secret)}</pre><button class="btn primary" data-close style="margin-top:12px">Done</button>`); } catch (err) { fail(err); } });
  $$("[data-hook]").forEach((b) => b.addEventListener("click", () => api("/app/webhooks/" + b.dataset.hook, { method: "DELETE" }).then(pageIntegrations).catch(fail)));
}

async function loadResend() {
  const el = $("#resend-card");
  if (!el) return;
  try {
    const r = await api("/app/integrations/resend");
    const step = (ok, n, title, body) => `<div class="row" style="align-items:flex-start;margin-bottom:12px;flex-wrap:nowrap"><span class="pill-step" style="${ok ? "background:var(--ok-bg);color:var(--ok)" : ""}">${ok ? "✓" : n}</span><div class="grow"><b>${title}</b><div class="small muted">${body}</div></div></div>`;
    const verified = r.domains.filter((d) => d.status === "verified");
    el.innerHTML = `<h2>Resend email setup</h2><p class="small muted">Resend is the recommended email provider. All keys go in the Credential vault (admins only, encrypted). Nothing is hardcoded.</p>
      ${step(r.api_key, 1, "Add your Resend API key", r.api_key ? "Saved in the vault." : 'Create one at resend.com → API Keys, then paste it into Credential vault → <span class="mono">RESEND_API_KEY</span>.')}
      ${step(verified.length > 0, 2, "Verify a sending domain (SPF / DKIM)", r.error ? `<span style="color:var(--bad)">${esc(r.error)}</span>` : r.domains.length ? r.domains.map((d) => `${esc(d.name)} ${badge(d.status, d.status === "verified" ? "b-ok" : "b-warn")}`).join(" &nbsp; ") : r.api_key ? "No domains yet. Add your domain in Resend → Domains and add the DNS records it shows." : "Available once the API key is saved.")}
      ${step(r.mailboxes.length > 0, 3, "Add a sending mailbox", r.mailboxes.length ? r.mailboxes.map(esc).join(", ") : "Below: add an address on a verified domain with provider <b>resend</b>.")}
      ${step(r.webhook_secret, 4, "Connect the webhook (deliveries, bounces, complaints, replies)", `In Resend → Webhooks, add this endpoint:<pre class="code" style="margin:6px 0">${esc(r.webhook_url)}</pre>Subscribe to: <span class="mono">${r.webhook_events.join(", ")}</span>. Then paste its signing secret into the vault as <span class="mono">RESEND_WEBHOOK_SECRET</span>. Note: the server must be reachable from the internet (deploy it, or use a tunnel for local testing).`)}
      ${step(!!r.reply_to, 5, "Receive prospect replies", r.reply_to ? `Replies go to <span class="mono">${esc(r.reply_to)}</span> and are handled by the reply agent.` : 'Enable receiving in Resend (your domain or the free <span class="mono">&lt;id&gt;.resend.app</span> address), then set it in the vault as <span class="mono">RESEND_REPLY_TO</span>.')}
      ${r.mailboxes.length ? `<form id="rs-test" class="row" style="margin-top:4px"><input name="to" type="email" placeholder="Send a test email to…" style="max-width:300px" required><button class="btn">Send test email</button></form>` : ""}`;
    on("#rs-test", "submit", async (e) => {
      e.preventDefault(); const b = $("button", e.target); setBusy(b, true, "Sending");
      try { const x = await api("/app/integrations/resend/test", { method: "POST", body: { to: e.target.to.value } }); toast(`Sent from ${x.from} (Resend id ${x.id})`); } catch (err) { fail(err); }
      setBusy(b, false);
    });
  } catch (e) { el.innerHTML = `<p class="muted">${esc(e.message)}</p>`; }
}

async function pageDataSources() {
  V().innerHTML = loading();
  const list = await api("/app/providers/status");
  let lo = null;
  if (can("integrations.manage")) { try { lo = await api("/app/providers/leadocean"); } catch { lo = null; } }
  const loCard = !lo ? "" : !lo.configured
    ? `<div class="card" style="margin-bottom:16px"><h2>LeadOcean</h2><p class="muted">Not connected yet, so company and people search won't return anything. Paste your LeadOcean API key into <a href="#/integrations">Integrations → Credential vault</a> as <span class="mono">LEADOCEAN_API_KEY</span>.</p></div>`
    : lo.ok ? `<div class="card" style="margin-bottom:16px"><div class="row between"><h2 style="margin:0">LeadOcean ${badge("connected", "b-ok")}</h2><span class="small muted">Plan: ${esc(lo.plan || "—")}</span></div>
        <div class="grid g3" style="margin-top:12px"><div class="kpi"><div class="l">Records used this month</div><div class="v" style="font-size:20px">${fmt(lo.records_used)}</div></div>
        <div class="kpi"><div class="l">Records remaining</div><div class="v" style="font-size:20px">${lo.records_remaining === null ? "fair use" : fmt(lo.records_remaining)}</div></div>
        <div class="kpi"><div class="l">Resets</div><div class="v" style="font-size:15px">${lo.resets_at ? dt(lo.resets_at) : "—"}</div></div></div>
        <p class="small muted" style="margin-top:8px">Find Companies, Find People, campaign audiences and email/phone enrichment now use real LeadOcean data. Every company or person returned and every email/phone lookup uses 1 LeadOcean record.</p></div>`
    : `<div class="card" style="margin-bottom:16px;border-color:var(--bad)"><h2>LeadOcean ${badge("error", "b-bad")}</h2><p>${esc(lo.error)}</p></div>`;
  V().innerHTML = `<div class="page-head"><div><h1>Data Sources</h1><p class="muted">Provider-independent: every capability routes through an orchestrator with priority, failover and licence rules. Once a real provider is connected, the platform never falls back to the demo data.</p></div></div>${loCard}
  <div class="tbl-wrap"><table><thead><tr><th>Provider</th><th>Capability</th><th>Status</th><th>Priority</th><th>Licence</th><th>Credential</th></tr></thead><tbody>${list.map((p) => `<tr><td><b>${esc(p.name)}</b><div class="small faint">${esc(p.description)}</div></td><td>${esc(p.kind.replace(/_/g, " "))}</td><td>${badge(p.enabled ? "enabled" : "disabled", p.enabled ? "b-ok" : "")}</td><td>${p.priority}</td>
  <td class="small">${Object.entries(p.license || {}).map(([k, v]) => `${esc(k.replace(/_/g, " "))}: <b>${esc(String(v))}</b>`).join("<br>") || "—"}</td><td>${p.needs_credential ? (p.credential_configured ? badge("configured", "b-ok") : `${badge("missing", "b-warn")} <span class="small mono">${esc(p.needs_credential)}</span>`) : "—"}</td></tr>`).join("")}</tbody></table></div>
  `;
}

async function pageBilling() {
  V().innerHTML = loading();
  const b = await api("/app/billing");
  const cats = ["data", "ai", "email", "whatsapp", "sms", "voice", "research", "api"];
  const warn = b.pct_used >= 100 ? "Upgrade or buy additional usage." : b.pct_used >= 95 ? "Your GTM engine may slow down soon." : b.pct_used >= 80 ? "You're approaching your monthly limit." : "";
  V().innerHTML = `<div class="page-head"><div><h1>Usage & Billing</h1><p class="muted">Everything is metered in GTM Units (GTU). Backend enforces quotas; no action runs on a negative balance.</p></div></div>
  ${warn ? `<div class="card" style="border-color:var(--warn);margin-bottom:16px"><b>⚠ ${esc(warn)}</b></div>` : ""}
  <div class="grid g4">${[["Plan", b.plan], ["Balance", fmt(b.balance, 1) + " GTU"], ["Used (30 days)", fmt(b.used_30d, 1) + " GTU"], ["Est. month-end usage", fmt(b.estimated_month_end, 1) + " GTU"]].map(([l, v]) => `<div class="card kpi"><div class="l">${l}</div><div class="v" style="font-size:20px">${esc(v)}</div></div>`).join("")}</div>
  <div class="grid g2" style="margin-top:16px"><div class="card"><h2>Usage by category (30 days)</h2>${cats.map((c) => { const u = b.usage[c] || { gtu: 0, quantity: 0 }; return `<div class="funnel-row"><span class="muted">${c}</span><div><div class="f" style="width:${Math.max(1, (100 * u.gtu) / Math.max(1, b.used_30d))}%"></div></div><b style="text-align:right">${fmt(u.gtu, 1)}</b></div>`; }).join("")}</div>
  <div class="card"><h2>Price list</h2><div class="tbl-wrap" style="max-height:300px"><table><thead><tr><th>Action</th><th>GTU</th></tr></thead><tbody>${b.prices.map((p) => `<tr><td>${esc(p.meter.replace(/_/g, " "))}</td><td>${p.gtu}</td></tr>`).join("")}</tbody></table></div></div></div>
  ${b.internal_mode ? '<div class="card" style="margin-top:16px"><p class="muted">Internal V1 mode: this organization runs on the internal plan, so plans and payments are hidden.</p></div>' : `<div class="card" style="margin-top:16px"><h2>Plans</h2><div class="grid g3">${b.plans.map((p) => `<div class="card flat"><div class="row between"><b>${esc(p.name)}</b>${p.code === b.plan ? badge("current", "b-ok") : ""}</div><div class="kpi"><div class="v">${p.price_usd ? "$" + p.price_usd : "Custom"}</div><div class="l">/month · ${fmt(p.monthly_gtu)} GTU · ${p.limits.users} users</div></div>
    <ul class="small">${Object.entries(p.entitlements).map(([k, v]) => `<li style="${v ? "" : "color:var(--faint);text-decoration:line-through"}">${esc(k.replace(/_/g, " "))}</li>`).join("")}</ul>${b.can_manage && p.code !== b.plan ? `<button class="btn sm primary" data-plan="${p.code}">Switch to ${esc(p.name)}</button>` : ""}</div>`).join("")}</div>
    ${b.can_manage ? '<div class="row" style="margin-top:12px"><input id="topup" type="number" value="1000" style="width:140px"><button class="btn" id="topup-btn">Buy additional GTU</button></div>' : lock("billing.manage")}</div>`}
  <div class="card" style="margin-top:16px"><h2>Transactions</h2>${b.transactions.slice(0, 25).map((t) => `<div class="row between small"><span>${esc(t.reason)}</span><span style="color:${t.amount < 0 ? "var(--bad)" : "var(--ok)"}">${t.amount > 0 ? "+" : ""}${fmt(t.amount, 2)}</span></div>`).join("")}</div>`;
  $$("[data-plan]").forEach((x) => x.addEventListener("click", () => api("/app/billing/plan", { method: "POST", body: { plan: x.dataset.plan } }).then((r) => { toast(r.note); boot(); }).catch(fail)));
  on("#topup-btn", "click", () => api("/app/billing/topup", { method: "POST", body: { gtu: Number($("#topup").value) } }).then(() => { toast("Credits added"); boot(); }).catch(fail));
}

/* ----- Settings ----- */
async function pageSettings(params) {
  const tab = params.get("tab") || "general";
  const tabs = [["general", "General", "dashboard.view"], ["users", "Users & roles", "users.manage"], ["policy", "Autonomy & policy", "dashboard.view"], ["scoring", "Scoring & ICP", "dashboard.view"], ["offers", "Offers & proof", "leads.view"], ["suppression", "Suppression", "leads.view"], ["audit", "Audit log", "audit.view"], ["account", "My account", null]].filter((t) => !t[2] || can(t[2]));
  V().innerHTML = `<div class="page-head"><div><h1>Settings</h1><p class="muted">Your role: <b>${esc(S.me.role_label)}</b>. Items you can't change are read-only.</p></div></div>
    <div class="tabs">${tabs.map(([k, l]) => `<a href="#/settings?tab=${k}" class="${k === tab ? "active" : ""}">${l}</a>`).join("")}</div><div id="set-body">${loading()}</div>`;
  const body = $("#set-body");
  const roDis = can("workspace.manage") ? "" : "disabled";
  if (tab === "general") {
    const s = await api("/app/settings");
    body.innerHTML = `<div class="grid g2"><div class="card"><h2>Organization</h2><form id="org-form">${["name", "website", "industry", "country", "timezone", "sender_name"].map((k) => `<div class="field"><label>${k.replace("_", " ")}</label><input name="${k}" value="${esc(s.organization[k] || "")}" ${roDis}></div>`).join("")}${roDis ? lock("workspace.manage") : '<button class="btn primary">Save</button>'}</form></div>
      <div class="card"><h2>AI business understanding</h2>${s.organization.profile && s.organization.profile.company_description ? `<p>${esc(s.organization.profile.company_description)}</p><div class="kv small"><div>Industries served</div><div>${(s.organization.profile.industries_served || []).map(esc).join(", ")}</div><div>Likely buyer</div><div>${(s.organization.profile.likely_buyer || []).map(esc).join(", ")}</div><div>Buying motion</div><div>${esc(s.organization.profile.buying_motion)}</div><div>Objections</div><div>${(s.organization.profile.objections || []).map(esc).join("; ")}</div></div>` : '<p class="muted">Not analysed yet.</p>'}${can("workspace.manage") ? '<a class="btn" href="#/onboarding" style="margin-top:12px">Re-analyse website</a>' : ""}</div></div>`;
    on("#org-form", "submit", async (e) => { e.preventDefault(); await api("/app/settings", { method: "PUT", body: { organization: Object.fromEntries(new FormData(e.target)) } }).then(() => toast("Saved")).catch(fail); });
  } else if (tab === "users") {
    const [users, roles] = await Promise.all([api("/app/users"), api("/app/roles")]);
    const assignable = S.me.role === "owner" ? roles.roles.map((r) => r.code) : roles.roles.map((r) => r.code).filter((r) => r !== "owner");
    body.innerHTML = `<div class="card"><div class="row between"><h2>Users</h2><button class="btn primary" id="inv-btn">Invite user</button></div><div class="tbl-wrap"><table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th>Last login</th><th></th></tr></thead><tbody>
      ${users.map((u) => `<tr><td>${esc(u.name)}</td><td>${esc(u.email)}</td><td>${u.id === S.me.user.id || (u.role === "owner" && S.me.role !== "owner") ? badge(u.role_label, "b-accent") : `<select data-role="${u.id}" style="width:auto">${roles.roles.filter((r) => assignable.includes(r.code)).map((r) => `<option value="${r.code}" ${r.code === u.role ? "selected" : ""}>${r.label}</option>`).join("")}</select>`}</td>
      <td>${badge(u.active ? "active" : "deactivated", u.active ? "b-ok" : "b-bad")}</td><td class="small">${u.last_login_at ? dt(u.last_login_at) : "never"}</td><td>${u.id !== S.me.user.id && (u.role !== "owner" || S.me.role === "owner") ? `<button class="btn sm" data-toggle="${u.id}" data-active="${u.active}">${u.active ? "Deactivate" : "Activate"}</button> <button class="btn sm danger" data-rm="${u.id}">Remove</button>` : ""}</td></tr>`).join("")}</tbody></table></div></div>
      <div class="card" style="margin-top:16px"><h2>Role permissions</h2><p class="small muted">Enforced on every API call. API keys act with the role chosen when the admin creates them.</p><div class="tbl-wrap"><table><thead><tr><th>Permission</th>${roles.roles.map((r) => `<th>${esc(r.label)}</th>`).join("")}</tr></thead><tbody>
      ${Object.entries(roles.permission_catalog).map(([p, d]) => `<tr><td class="small"><b>${esc(p)}</b><div class="faint">${esc(d)}</div></td>${roles.roles.map((r) => `<td style="text-align:center">${r.permissions.includes(p) ? "✓" : "<span class='faint'>—</span>"}</td>`).join("")}</tr>`).join("")}</tbody></table></div></div>`;
    on("#inv-btn", "click", () => {
      const m = modal(`<h2>Invite user</h2><form id="inv-form"><div class="field"><label>Name</label><input name="name" required></div><div class="field"><label>Email</label><input name="email" type="email" required></div><div class="field"><label>Role</label><select name="role">${roles.roles.filter((r) => assignable.includes(r.code)).map((r) => `<option value="${r.code}" ${r.code === "sales_user" ? "selected" : ""}>${r.label}</option>`).join("")}</select></div><div class="row"><button class="btn primary">Invite</button><button type="button" class="btn" data-close>Cancel</button></div></form><div id="inv-res"></div>`);
      on("#inv-form", "submit", async (e) => { e.preventDefault(); try { const r = await api("/app/users", { method: "POST", body: Object.fromEntries(new FormData(e.target)) }); $("#inv-res", m).innerHTML = r.temporary_password ? `<p style="margin-top:12px">Temporary password (share securely, shown once):</p><pre class="code">${esc(r.temporary_password)}</pre>` : "<p>Added.</p>"; e.target.remove(); } catch (err) { fail(err); } }, m);
    });
    $$("[data-role]").forEach((s) => s.addEventListener("change", () => api("/app/users/" + s.dataset.role, { method: "PATCH", body: { role: s.value } }).then(() => toast("Role updated")).catch((e) => { fail(e); route(); })));
    $$("[data-toggle]").forEach((b) => b.addEventListener("click", () => api("/app/users/" + b.dataset.toggle, { method: "PATCH", body: { active: b.dataset.active !== "true" } }).then(route).catch(fail)));
    $$("[data-rm]").forEach((b) => b.addEventListener("click", () => confirm("Remove this user from the organization?") && api("/app/users/" + b.dataset.rm, { method: "DELETE" }).then(route).catch(fail)));
  } else if (tab === "policy") {
    const s = await api("/app/settings");
    const p = s.policy;
    const auto = can("autonomy.configure") ? "" : "disabled";
    body.innerHTML = `<div class="grid g2"><div class="card"><h2>Autopilot</h2><p class="small muted">High-risk settings can stay disabled by policy. The agent can never bypass suppression, hard budgets, legal restrictions or channel rules.</p>
      ${Object.entries(p.autopilot).map(([k, v]) => `<label style="font-weight:500;color:var(--text)"><input type="checkbox" data-ap="${k}" ${v ? "checked" : ""} ${auto}> ${k.replace(/_/g, " ")}</label>`).join("")}
      <div class="field" style="margin-top:12px"><label>Auto-reply confidence threshold</label><input id="p-conf" type="number" step="0.05" min="0" max="1" value="${p.auto_reply_confidence}" ${roDis}></div>
      <div class="field"><label>Budget changes the agent may make without approval (GTU/day)</label><input id="p-blim" type="number" value="${p.budget_change_autonomous_limit}" ${auto}></div></div>
      <div class="card"><h2>Compliance & delivery safety</h2><div class="field"><label>Sendable email statuses</label><input id="p-send" value="${esc(p.sendable_email_statuses.join(", "))}" ${roDis}></div>
      <div class="field"><label>Don't re-contact within (days)</label><input id="p-rec" type="number" value="${p.recontact_days}" ${roDis}></div>
      <div class="field"><label>Blocked countries (comma separated)</label><input id="p-bc" value="${esc(p.blocked_countries.join(", "))}" ${roDis}></div>
      <div class="field"><label>Countries requiring human review</label><input id="p-rc" value="${esc(p.review_countries.join(", "))}" ${roDis}></div>
      <div class="grid g2"><div class="field"><label>Send window start hour</label><input id="p-sh" type="number" value="${p.send_window.start_hour}" ${roDis}></div><div class="field"><label>Send window end hour</label><input id="p-eh" type="number" value="${p.send_window.end_hour}" ${roDis}></div></div>
      <h3>Booking availability</h3><div class="grid g3"><div class="field"><label>Duration (min)</label><input id="b-d" type="number" value="${p.booking.duration}" ${roDis}></div><div class="field"><label>Start hour</label><input id="b-s" type="number" value="${p.booking.start_hour}" ${roDis}></div><div class="field"><label>End hour</label><input id="b-e" type="number" value="${p.booking.end_hour}" ${roDis}></div></div>
      <div class="field"><label>Meeting location</label><input id="b-l" value="${esc(p.booking.location)}" ${roDis}></div>
      ${S.me.demo_mode ? `<label style="font-weight:500;color:var(--text)"><input type="checkbox" id="p-sim" ${p.sandbox_auto_replies ? "checked" : ""} ${roDis}> Test mode: simulate prospect replies</label>` : ""}</div></div>
      ${roDis ? lock("workspace.manage") : '<button class="btn primary" id="pol-save" style="margin-top:16px">Save policy</button>'}`;
    on("#pol-save", "click", async () => {
      const list = (id) => $(id).value.split(",").map((x) => x.trim()).filter(Boolean);
      const pol = { auto_reply_confidence: Number($("#p-conf").value), sendable_email_statuses: list("#p-send"), recontact_days: Number($("#p-rec").value), blocked_countries: list("#p-bc"), review_countries: list("#p-rc"),
        send_window: { ...p.send_window, start_hour: Number($("#p-sh").value), end_hour: Number($("#p-eh").value) }, booking: { ...p.booking, duration: Number($("#b-d").value), start_hour: Number($("#b-s").value), end_hour: Number($("#b-e").value), location: $("#b-l").value }, sandbox_auto_replies: $("#p-sim") ? $("#p-sim").checked : false };
      if (can("autonomy.configure")) { pol.autopilot = Object.fromEntries($$("[data-ap]").map((c) => [c.dataset.ap, c.checked])); pol.budget_change_autonomous_limit = Number($("#p-blim").value); }
      await api("/app/settings", { method: "PUT", body: { policy: pol } }).then(() => toast("Policy saved")).catch(fail);
    });
  } else if (tab === "scoring") {
    const s = await api("/app/settings");
    body.innerHTML = `<div class="grid g2"><div class="card"><h2>Opportunity score weights</h2><p class="small muted">Must sum to 1.0.</p>${Object.entries(s.score_weights).map(([k, v]) => `<div class="field"><label>${k}</label><input type="number" step="0.05" data-w="${k}" value="${v}" ${roDis}></div>`).join("")}${roDis ? "" : '<button class="btn primary" id="w-save">Save weights</button>'}</div>
      <div class="card"><h2>Workspace ICP</h2><p class="small muted">Used to score companies when no mission ICP applies.</p><textarea id="icp-json" rows="10" class="mono" ${roDis}>${esc(JSON.stringify(s.icp, null, 2))}</textarea>
      <h2 style="margin-top:16px">Agent memory</h2><p class="small muted">Preferences the agent applies to all future missions, e.g. “We do not target hospitals”. One per line.</p><textarea id="mem" ${roDis}>${esc((s.memory || []).join("\n"))}</textarea>${roDis ? lock("workspace.manage") : '<button class="btn primary" id="icp-save" style="margin-top:8px">Save ICP & memory</button>'}</div></div>`;
    on("#w-save", "click", () => api("/app/settings", { method: "PUT", body: { score_weights: Object.fromEntries($$("[data-w]").map((i) => [i.dataset.w, Number(i.value)])) } }).then(() => toast("Saved")).catch(fail));
    on("#icp-save", "click", () => { let icp; try { icp = JSON.parse($("#icp-json").value || "{}"); } catch { return toast("ICP must be valid JSON", true); } api("/app/settings", { method: "PUT", body: { icp, memory: $("#mem").value.split("\n").map((x) => x.trim()).filter(Boolean) } }).then(() => toast("Saved")).catch(fail); });
  } else if (tab === "offers") {
    const o = await api("/app/offers");
    const mg = can("offers.manage");
    body.innerHTML = `<div class="grid g2"><div class="card"><div class="row between"><h2>Offer library</h2>${mg ? '<button class="btn sm primary" id="off-new">Add offer</button>' : ""}</div><p class="small muted">Offer routing: prospects in an offer's industries receive that offer.</p>
      ${o.offers.map((x) => `<div class="card flat" style="margin-bottom:8px"><div class="row between"><b>${esc(x.name)}</b>${mg ? `<button class="btn sm danger" data-doff="${x.id}">Delete</button>` : ""}</div><p class="small">${esc(x.description)}</p><div class="small muted">Problems: ${x.problems.map(esc).join("; ")}<br>Benefits: ${x.benefits.map(esc).join("; ")}<br>Industries: ${x.industries.map(esc).join(", ") || "all"}<br>CTA: ${esc(x.cta)}${x.pricing ? "<br>Pricing: " + esc(x.pricing) : ""}</div></div>`).join("")}</div>
      <div class="card"><h2>Proof library & claim governance</h2><p class="small muted">The messaging agent may only cite <b>approved</b> claims and never invents numbers.</p>${mg ? '<form id="proof-form" class="row"><input name="claim" class="grow" placeholder="e.g. Cut lead response time from 6 hours to 5 minutes for a Pune realty firm" required><input name="source" placeholder="source" style="width:140px"><button class="btn">Add</button></form>' : ""}
      ${o.proof.map((x) => `<div class="row between small" style="margin-top:8px"><span>${esc(x.claim)} <span class="faint">${esc(x.source)}</span></span>${can("workspace.manage") ? `<label style="margin:0"><input type="checkbox" data-appr="${x.id}" ${x.approved ? "checked" : ""}> approved</label>` : badge(x.approved ? "approved" : "pending", x.approved ? "b-ok" : "b-warn")}</div>`).join("") || '<p class="muted small">No proof items yet.</p>'}</div></div>`;
    on("#off-new", "click", () => {
      const m = modal(`<h2>New offer</h2><form id="of">${[["name", "Name"], ["description", "Description"], ["problems", "Problems solved (; separated)"], ["benefits", "Benefits (; separated)"], ["industries", "Industries (; separated, optional)"], ["cta", "Call to action"], ["pricing", "Pricing guidance (optional)"]].map(([k, l]) => `<div class="field"><label>${l}</label><input name="${k}" ${k === "name" ? "required" : ""}></div>`).join("")}<div class="row"><button class="btn primary">Save</button><button type="button" class="btn" data-close>Cancel</button></div></form>`);
      on("#of", "submit", async (e) => { e.preventDefault(); const f = Object.fromEntries(new FormData(e.target)); ["problems", "benefits", "industries"].forEach((k) => (f[k] = f[k].split(";").map((x) => x.trim()).filter(Boolean))); if (!f.cta) delete f.cta; await api("/app/offers", { method: "POST", body: f }).then(() => { closeModal(); route(); }).catch(fail); }, m);
    });
    $$("[data-doff]").forEach((b) => b.addEventListener("click", () => api("/app/offers/" + b.dataset.doff, { method: "DELETE" }).then(route).catch(fail)));
    on("#proof-form", "submit", async (e) => { e.preventDefault(); await api("/app/proof", { method: "POST", body: Object.fromEntries(new FormData(e.target)) }).then(route).catch(fail); });
    $$("[data-appr]").forEach((c) => c.addEventListener("change", () => api("/app/proof/" + c.dataset.appr, { method: "PATCH", body: { approved: c.checked } }).then(() => toast("Updated")).catch(fail)));
  } else if (tab === "suppression") {
    const list = await api("/app/suppression");
    body.innerHTML = `<div class="card"><h2>Universal suppression</h2><p class="small muted">Overrides everything. “Never contact anyone from microsoft.com” → add a domain. STOP replies are added automatically.</p>
      ${can("suppression.manage") ? `<form id="sup-form" class="row"><select name="kind" style="width:auto"><option>email</option><option>domain</option><option>phone</option><option>linkedin</option></select><input name="value" class="grow" placeholder="value" required><select name="channel" style="width:auto"><option>all</option><option>email</option><option>whatsapp</option><option>sms</option><option>voice</option></select><input name="reason" placeholder="reason" style="width:180px"><button class="btn primary">Add</button></form>` : lock("suppression.manage")}
      <div class="tbl-wrap" style="margin-top:12px"><table><thead><tr><th>Type</th><th>Value</th><th>Channel</th><th>Reason</th><th>Scope</th><th>Added</th><th></th></tr></thead><tbody>${list.map((s) => `<tr><td>${badge(s.kind)}</td><td class="mono">${esc(s.value)}</td><td>${esc(s.channel)}</td><td class="small">${esc(s.reason)}</td><td>${esc(s.scope)}</td><td class="small">${ago(s.created_at)}</td><td>${can("suppression.manage") ? `<button class="btn sm" data-dsup="${s.id}">Remove</button>` : ""}</td></tr>`).join("")}</tbody></table></div></div>`;
    on("#sup-form", "submit", async (e) => { e.preventDefault(); const f = Object.fromEntries(new FormData(e.target)); if (!f.reason) f.reason = "manual"; try { const r = await api("/app/suppression", { method: "POST", body: f }); toast(`Suppressed. ${r.stopped_sequences} active sequence(s) stopped.`); route(); } catch (err) { fail(err); } });
    $$("[data-dsup]").forEach((b) => b.addEventListener("click", () => confirm("Remove from suppression? The contact may be messaged again.") && api("/app/suppression/" + b.dataset.dsup, { method: "DELETE" }).then(route).catch(fail)));
  } else if (tab === "audit") {
    const list = await api("/app/audit");
    body.innerHTML = `<div class="tbl-wrap"><table><thead><tr><th>When</th><th>Action</th><th>Actor</th><th>Entity</th><th>Details</th><th>Request</th></tr></thead><tbody>${list.map((a) => `<tr><td class="small">${dt(a.at)}</td><td><b class="small">${esc(a.action)}</b></td><td class="small">${esc(a.actor)} <span class="faint">${esc(a.actor_type)}</span></td><td class="small mono">${esc(a.entity)}</td><td class="small mono" style="max-width:340px;overflow:hidden;text-overflow:ellipsis">${esc(JSON.stringify(a.details))}</td><td class="mono small faint">${esc(a.request_id)}</td></tr>`).join("")}</tbody></table></div>`;
  } else if (tab === "account") {
    body.innerHTML = `<div class="card" style="max-width:480px"><h2>Change password</h2><form id="pw-form"><div class="field"><label>Current password</label><input name="current_password" type="password" required></div><div class="field"><label>New password (min 8)</label><input name="new_password" type="password" minlength="8" required></div><button class="btn primary">Update password</button></form></div>`;
    on("#pw-form", "submit", async (e) => { e.preventDefault(); await api("/app/auth/password", { method: "POST", body: Object.fromEntries(new FormData(e.target)) }).then(() => { toast("Password updated"); e.target.reset(); }).catch(fail); });
  }
}

async function pageOnboarding() {
  V().innerHTML = `<div class="page-head"><div><h1>Turn your website into an autonomous sales engine.</h1><p class="muted">Enter your website. The research agent learns what you sell, proposes your ICP and creates your offer library. The goal is your first campaign in about 10 minutes.</p></div></div>
  <div class="grid g-side"><div class="card"><form id="ob"><div class="field"><label>Website</label><input name="website" value="${esc(S.me.organization.website || "")}" placeholder="https://yourcompany.com"></div>
    <div class="field"><label>What does your business do? (optional, helps if the site is thin)</label><textarea name="description" placeholder="We build AI automation, CRM automation and custom software for Indian SMBs…"></textarea></div>
    ${can("workspace.manage") ? '<button class="btn primary">Analyse my business</button>' : lock("workspace.manage")}</form><div id="ob-res" style="margin-top:16px"></div></div>
  <div class="card"><h3>Setup steps</h3>${["Company", "Website", "Offer", "Target market", "Connect channels", "Connect calendar", "Connect CRM", "Set budget", "Start autonomous GTM"].map((s, i) => `<div class="row small" style="margin-bottom:6px"><span class="pill-step">${i + 1}</span>${s}</div>`).join("")}</div></div>`;
  on("#ob", "submit", async (e) => {
    e.preventDefault(); const b = $("button", e.target); setBusy(b, true, "Researching your business");
    try {
      const r = await api("/app/onboarding/analyze", { method: "POST", body: Object.fromEntries(new FormData(e.target)) });
      const p = r.profile;
      $("#ob-res").innerHTML = `<div class="card flat brief"><h2>Here's what I learned</h2><p>${esc(p.company_description)}</p><div class="kv small"><div>Offers</div><div>${(p.primary_offers || []).map((o) => esc(o.name)).join(", ")}</div><div>Industries served</div><div>${(p.industries_served || []).map(esc).join(", ")}</div><div>Likely buyer</div><div>${(p.likely_buyer || []).map(esc).join(", ")}</div><div>Proposed ICP</div><div>${critChips(p.icp || {})}</div><div>Pages analysed</div><div>${(p.pages_analyzed || []).length} · ${esc(p.model)}</div></div>
        <p class="small" style="margin-top:8px">${r.offers_created.length ? "Created offers: " + r.offers_created.map(esc).join(", ") : ""}</p><a class="btn primary" href="#/missions?new=1">Next: create your first mission →</a></div>`;
      boot(true);
    } catch (err) { fail(err); } setBusy(b, false);
  });
}

/* ----- Platform super admin ----- */
async function pagePlatform(params) {
  const tab = params.get("tab") || "tenants";
  const tabs = [["tenants", "Tenants"], ["providers", "Providers"], ["pricing", "Pricing & plans"], ["secrets", "Platform credentials"], ["health", "Health & queues"], ["trace", "Request trace"]];
  V().innerHTML = `<div class="page-head"><div><h1>Platform Admin</h1><p class="muted">Internal AI Automation Labs console. Tenant conversation content is deliberately not visible here.</p></div></div>
    <div class="tabs">${tabs.map(([k, l]) => `<a href="#/platform?tab=${k}" class="${k === tab ? "active" : ""}">${l}</a>`).join("")}</div><div id="pf">${loading()}</div>`;
  const el = $("#pf");
  if (tab === "tenants") {
    const t = await api("/app/platform/tenants");
    el.innerHTML = `<div class="tbl-wrap"><table><thead><tr><th>Organization</th><th>Plan</th><th>Status</th><th>Users</th><th>Campaigns</th><th>Msgs 30d</th><th>Balance</th><th>GTU 30d</th><th>Provider $ 30d</th><th>AI calls / k-tokens / $</th><th></th></tr></thead><tbody>${t.map((o) => `<tr><td><b>${esc(o.name)}</b><div class="small faint mono">${o.id}</div></td><td>${esc(o.plan)}</td><td>${badge(o.status, statusKind(o.status))}</td><td>${o.users}</td><td>${o.campaigns}</td><td>${o.messages_30d}</td><td>${fmt(o.balance)}</td><td>${fmt(o.gtu_30d, 1)}</td><td>$${o.provider_cost_30d}</td><td class="small">${o.ai_calls_30d} / ${o.ai_ktokens_30d} / $${o.ai_cost_30d}</td>
      <td><button class="btn sm" data-grant="${o.id}">Grant GTU</button> <button class="btn sm ${o.status === "active" ? "danger" : ""}" data-st="${o.id}" data-to="${o.status === "active" ? "suspended" : "active"}">${o.status === "active" ? "Suspend" : "Activate"}</button></td></tr>`).join("")}</tbody></table></div>`;
    $$("[data-grant]").forEach((b) => b.addEventListener("click", () => { const n = prompt("GTU to grant:", "1000"); if (n) api("/app/platform/tenants/" + b.dataset.grant, { method: "PATCH", body: { grant_gtu: Number(n), reason: "manual grant" } }).then(route).catch(fail); }));
    $$("[data-st]").forEach((b) => b.addEventListener("click", () => confirm(`${b.dataset.to} this tenant?`) && api("/app/platform/tenants/" + b.dataset.st, { method: "PATCH", body: { status: b.dataset.to } }).then(route).catch(fail)));
  } else if (tab === "providers") {
    const ps = await api("/app/platform/providers");
    el.innerHTML = `<div class="tbl-wrap"><table><thead><tr><th>Provider</th><th>Kind</th><th>Enabled</th><th>Priority</th><th>Cost $</th><th>Calls</th><th>Success</th><th>Latency</th><th>Config</th></tr></thead><tbody>${ps.map((p) => `<tr><td><b>${esc(p.name)}</b><div class="small faint">${esc(p.description)}</div></td><td>${esc(p.kind)}</td><td><input type="checkbox" data-en="${p.id}" ${p.enabled ? "checked" : ""}></td><td><input type="number" data-pr="${p.id}" value="${p.priority}" style="width:70px"></td><td>${p.cost_usd}</td><td>${p.calls}</td><td>${p.success_rate ?? "—"}%</td><td>${p.avg_latency_ms}ms</td><td>${p.id === "external_rest_companies" ? `<input data-cfg="${p.id}" value="${esc((p.config || {}).endpoint || "")}" placeholder="https://provider/api/search">` : ""}</td></tr>`).join("")}</tbody></table></div><p class="small muted" style="margin-top:8px">Lower priority runs first. Failover moves to the next provider automatically.</p>`;
    $$("[data-en]").forEach((c) => c.addEventListener("change", () => api("/app/platform/providers/" + c.dataset.en, { method: "PATCH", body: { enabled: c.checked } }).then(() => toast("Saved")).catch(fail)));
    $$("[data-pr]").forEach((c) => c.addEventListener("change", () => api("/app/platform/providers/" + c.dataset.pr, { method: "PATCH", body: { priority: Number(c.value) } }).then(() => toast("Saved")).catch(fail)));
    $$("[data-cfg]").forEach((c) => c.addEventListener("change", () => api("/app/platform/providers/" + c.dataset.cfg, { method: "PATCH", body: { config: { endpoint: c.value } } }).then(() => toast("Saved")).catch(fail)));
  } else if (tab === "pricing") {
    const p = await api("/app/platform/pricing");
    el.innerHTML = `<div class="grid g2"><div class="card"><h2>Meter prices</h2><div class="tbl-wrap"><table><thead><tr><th>Meter</th><th>Category</th><th>GTU</th><th>Provider cost $</th></tr></thead><tbody>${p.meters.map((x) => `<tr><td>${esc(x.meter)}</td><td>${esc(x.category)}</td><td><input type="number" step="0.1" data-mg="${x.meter}" value="${x.gtu}" style="width:80px"></td><td><input type="number" step="0.001" data-mc="${x.meter}" value="${x.provider_cost_usd}" style="width:90px"></td></tr>`).join("")}</tbody></table></div></div>
      <div class="card"><h2>Plans & entitlements</h2>${p.plans.map((x) => `<div class="card flat" style="margin-bottom:8px"><b>${esc(x.name)}</b> <span class="mono small">${x.code}</span><div class="grid g2" style="margin-top:6px"><div class="field"><label>Price $</label><input data-pp="${x.code}" type="number" value="${x.price_usd}"></div><div class="field"><label>Monthly GTU</label><input data-pg="${x.code}" type="number" value="${x.monthly_gtu}"></div></div><div class="chips">${Object.entries(x.entitlements).map(([k, v]) => `<span class="chip ${v ? "on" : ""}" data-ent="${x.code}" data-k="${k}">${k}</span>`).join("")}</div></div>`).join("")}</div></div>`;
    $$("[data-mg]").forEach((i) => i.addEventListener("change", () => api("/app/platform/pricing/meters/" + i.dataset.mg, { method: "PATCH", body: { gtu: Number(i.value) } }).then(() => toast("Saved")).catch(fail)));
    $$("[data-mc]").forEach((i) => i.addEventListener("change", () => api("/app/platform/pricing/meters/" + i.dataset.mc, { method: "PATCH", body: { provider_cost_usd: Number(i.value) } }).then(() => toast("Saved")).catch(fail)));
    $$("[data-pp]").forEach((i) => i.addEventListener("change", () => api("/app/platform/pricing/plans/" + i.dataset.pp, { method: "PATCH", body: { price_usd: Number(i.value) } }).then(() => toast("Saved")).catch(fail)));
    $$("[data-pg]").forEach((i) => i.addEventListener("change", () => api("/app/platform/pricing/plans/" + i.dataset.pg, { method: "PATCH", body: { monthly_gtu: Number(i.value) } }).then(() => toast("Saved")).catch(fail)));
    $$("[data-ent]").forEach((c) => c.addEventListener("click", () => { c.classList.toggle("on"); const code = c.dataset.ent; const ent = Object.fromEntries($$(`[data-ent="${code}"]`).map((x) => [x.dataset.k, x.classList.contains("on")])); api("/app/platform/pricing/plans/" + code, { method: "PATCH", body: { entitlements: ent } }).then(() => toast("Entitlements saved")).catch(fail); }));
  } else if (tab === "secrets") {
    const s = await api("/app/platform/secrets");
    el.innerHTML = `<div class="card"><h2>Platform credentials</h2><p class="small muted">Shared defaults used by every tenant that hasn't configured its own key (e.g. the platform's AI key). Encrypted at rest; values are never displayed again.</p>${s.map((x) => `<form class="row" data-ps="${x.key}" style="margin-bottom:8px"><div style="width:260px"><b class="small">${esc(x.label)}</b><div class="mono faint">${esc(x.key)}</div></div><input class="grow" type="${x.secret ? "password" : "text"}" placeholder="${x.configured ? "Configured " + esc(x.masked) : "Not set"}" autocomplete="off"><button class="btn sm primary">Save</button>${x.configured ? `<button type="button" class="btn sm danger" data-pdel="${x.key}">Remove</button>` : ""}</form>`).join("")}</div>`;
    $$("[data-ps]").forEach((f) => f.addEventListener("submit", async (e) => { e.preventDefault(); const v = $("input", f).value; if (!v) return; await api("/app/platform/secrets", { method: "PUT", body: { key: f.dataset.ps, value: v } }).then(() => { toast("Saved"); route(); }).catch(fail); }));
    $$("[data-pdel]").forEach((b) => b.addEventListener("click", () => api("/app/platform/secrets", { method: "PUT", body: { key: b.dataset.pdel, value: "" } }).then(route).catch(fail)));
  } else if (tab === "health") {
    const [h, jobs] = await Promise.all([api("/app/platform/health"), api("/app/platform/jobs")]);
    el.innerHTML = `<div class="grid g4">${[["Worker last tick", h.worker_last_tick ? ago(h.worker_last_tick) : "not running"], ["Tenants", h.tenants], ["Users", h.users], ["Abuse signals", h.abuse_signals.length]].map(([l, v]) => `<div class="card kpi"><div class="l">${l}</div><div class="v" style="font-size:20px">${v}</div></div>`).join("")}</div>
      <div class="grid g3" style="margin-top:16px"><div class="card"><h2>Queue</h2>${Object.entries(h.queue).map(([k, v]) => `<div class="row between small"><span>${k}</span><b>${v}</b></div>`).join("") || "<p class='muted small'>empty</p>"}</div>
      <div class="card"><h2>Channels (last hour)</h2>${Object.entries(h.channels_last_hour).map(([k, v]) => `<div class="small"><b>${k}</b>: ${v.sent} sent · ${v.failed} failed · ${v.blocked} blocked</div>`).join("") || "<p class='muted small'>No traffic</p>"}</div>
      <div class="card"><h2>Email infrastructure</h2>${Object.entries(h.mailboxes).map(([k, v]) => `<div class="row between small"><span>${k}</span><b>${v}</b></div>`).join("")}<h3 style="margin-top:12px">Webhooks</h3>${Object.entries(h.webhooks).map(([k, v]) => `<div class="row between small"><span>${k}</span><b>${v}</b></div>`).join("") || "<p class='muted small'>none</p>"}</div></div>
      <div class="card" style="margin-top:16px"><h2>Recent jobs</h2><div class="tbl-wrap"><table><thead><tr><th>Job</th><th>Tenant</th><th>Type</th><th>Status</th><th>Attempts</th><th>Error</th></tr></thead><tbody>${jobs.map((j) => `<tr><td class="mono small">${j.id}</td><td class="mono small">${j.org}</td><td>${esc(j.type)}</td><td>${badge(j.status, statusKind(j.status))}</td><td>${j.attempts}</td><td class="small">${esc(j.error)}</td></tr>`).join("")}</tbody></table></div></div>`;
  } else if (tab === "trace") {
    el.innerHTML = `<div class="card"><form id="tr" class="row"><input id="rid" class="grow" placeholder="req_… (every API response carries X-Request-Id)"><button class="btn primary">Trace</button></form><div id="tr-res" style="margin-top:12px"></div></div>`;
    on("#tr", "submit", async (e) => { e.preventDefault(); const r = await api("/app/platform/audit?request_id=" + encodeURIComponent($("#rid").value)); $("#tr-res").innerHTML = `<pre class="code">${esc(JSON.stringify(r, null, 2))}</pre>`; });
  }
}

/* ---------- router ---------- */
const ROUTES = [
  [/^#\/home$/, pageHome, "dashboard.view"], [/^#\/command$/, pageCommandCenter, "agents.monitor"],
  [/^#\/companies$/, pageCompanies, "leads.view"], [/^#\/companies\/(\w+)$/, pageCompany, "leads.view"],
  [/^#\/people$/, pagePeople, "leads.view"], [/^#\/research$/, pageResearch, "leads.view"],
  [/^#\/leads$/, (_, p) => pageLeads(p), "leads.view"], [/^#\/leads\/(\w+)$/, pageLead, "leads.view"],
  [/^#\/missions$/, (_, p) => pageMissions(p), "leads.view"], [/^#\/missions\/(\w+)$/, pageMission, "leads.view"],
  [/^#\/campaigns$/, pageCampaigns, "leads.view"], [/^#\/campaigns\/(\w+)$/, pageCampaign, "leads.view"],
  [/^#\/inbox$/, (_, p) => pageInbox(null, p), "conversations.view"], [/^#\/inbox\/(\w+)$/, (id, p) => pageInbox(id, p), "conversations.view"],
  [/^#\/meetings$/, pageMeetings, "meetings.view"], [/^#\/linkedin$/, pageLinkedIn, "leads.view"], [/^#\/analytics$/, pageAnalytics, "dashboard.view"],
  [/^#\/integrations$/, pageIntegrations, "integrations.manage"], [/^#\/data-sources$/, pageDataSources, "leads.view"],
  [/^#\/billing$/, pageBilling, "billing.view"], [/^#\/settings$/, (_, p) => pageSettings(p), "dashboard.view"],
  [/^#\/onboarding$/, pageOnboarding, "dashboard.view"], [/^#\/platform$/, (_, p) => pagePlatform(p), "platform"],
];

async function route() {
  if (!S.me) return;
  closeModal();
  const [path, qs] = (location.hash || "#/home").split("?");
  const params = new URLSearchParams(qs || "");
  $$("[data-nav]").forEach((a) => a.classList.toggle("active", path === a.dataset.nav || path.startsWith(a.dataset.nav + "/")));
  const sb = $("#sidebar"); if (sb) sb.classList.remove("open");
  for (const [re, fn, perm] of ROUTES) {
    const m = path.match(re);
    if (!m) continue;
    if (perm === "platform" ? !S.me.user.is_platform_admin : !can(perm)) {
      V().innerHTML = empty("You don't have access to this page", `Your role (${S.me.role_label || "none"}) doesn't include ${perm}. Ask an admin if you need it.`);
      return;
    }
    try { await fn(m[1] || params, params); } catch (e) { V().innerHTML = `<div class="card"><h2>Something went wrong</h2><p class="muted">${esc(e.message)}</p></div>`; }
    return;
  }
  location.hash = S.me.organization ? "#/home" : "#/platform";
}

async function boot(silent = false) {
  if (!S.token) return render();
  try {
    S.me = await api("/app/me");
    if (S.me.workspace) { S.ws = S.me.workspace.id; localStorage.setItem("gtm_ws", S.ws); }
  } catch { S.me = null; return render(); }
  if (silent && $("#view")) return;   // refresh profile only; keep the current page as it is
  render();
}

function render() {
  const hash = location.hash || "";
  if (!S.me) {
    const mode = hash.startsWith("#/signup") ? "signup" : "login";
    $("#app").innerHTML = authView(mode);
    bindAuth(mode);
    return;
  }
  if (S.me.user.must_change_password) { $("#app").innerHTML = forcePasswordView(); bindForcePassword(); return; }
  if (hash.startsWith("#/login") || hash.startsWith("#/signup") || !hash) location.hash = S.me.organization ? "#/home" : "#/platform";
  $("#app").innerHTML = shell();
  bindShell();
  route();
  pollBadges();
}

let pollTimer = null;
function pollBadges() {
  clearInterval(pollTimer);
  const tick = async () => {
    if (!S.me || !S.me.organization || !can("conversations.view")) return;
    try {
      const r = await api("/app/inbox?filter=human_review");
      const el = $("#nav-review"); if (el) { el.hidden = !r.counts.human_review; el.textContent = r.counts.human_review; }
      const n = await api("/app/notifications"); const unread = n.filter((x) => !x.read).length;
      const nc = $("#notif-count"); if (nc) nc.textContent = unread || "";
    } catch { /* ignore */ }
  };
  tick(); pollTimer = setInterval(tick, 20000);
}

window.addEventListener("hashchange", () => (S.me && !S.me.user.must_change_password ? route() : render()));
try { const t = localStorage.getItem("gtm_theme"); if (t) document.documentElement.dataset.theme = t; } catch { /* ignore */ }
boot();
