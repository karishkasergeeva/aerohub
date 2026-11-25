// AeroHub - веб-клієнт диспетчера AOC і планувальника (без збирання, чистий JavaScript).
// Звертається до API Gateway (той самий домен) і отримує оновлення через WebSocket FIDS Service.
"use strict";

const API = "/api/v1";
const STATUS_UK = { SCHEDULED: "Заплановано", CHECK_IN: "Реєстрація", BOARDING: "Посадка", DEPARTED: "Відправлено",
  RETURNED: "Повернувся", ARRIVED: "Прибув", DELAYED: "Затримано", CANCELLED: "Скасовано", DIVERTED: "Перенаправлено" };
// Дзеркало правил переходів сервера (app/domain.py) - лише для підказки у списку
const TRANSITIONS = {
  D: { SCHEDULED: ["CHECK_IN", "DELAYED", "CANCELLED"], CHECK_IN: ["BOARDING", "DELAYED", "CANCELLED"],
       BOARDING: ["DEPARTED", "DELAYED", "CANCELLED"], DELAYED: ["DELAYED", "CHECK_IN", "BOARDING", "DEPARTED", "CANCELLED"],
       DEPARTED: ["RETURNED"], RETURNED: ["BOARDING", "DELAYED", "CANCELLED"] },
  A: { SCHEDULED: ["DELAYED", "ARRIVED", "DIVERTED", "CANCELLED"], DELAYED: ["DELAYED", "ARRIVED", "DIVERTED", "CANCELLED"] },
};

const state = { token: sessionStorage.getItem("token"), tz: sessionStorage.getItem("tz") || "Europe/Kyiv", user: null, flights: new Map(), board: new Map(), editing: null };
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// Час показується в поясі аеропорту (Europe/Kyiv) або в UTC; сервер завжди зберігає UTC (дефект D-13)
const tz = () => state.tz;
function parts(ms) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: tz(), hourCycle: "h23", year: "numeric", month: "2-digit",
    day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  return p;
}
const offset = (ms) => { const p = parts(ms); return Date.UTC(+p.year, p.month - 1, +p.day, +p.hour, +p.minute, +p.second) - Math.floor(ms / 1000) * 1000; };
const hm = (iso) => { if (!iso) return ""; const p = parts(Date.parse(iso)); return `${p.hour}:${p.minute}`; };
const toLocalInput = (iso) => { if (!iso) return ""; const p = parts(Date.parse(iso)); return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`; };
function fromLocalInput(v) {
  if (!v) return null;
  const wall = Date.parse(v + ":00Z"), first = wall - offset(wall);
  return new Date(wall - offset(first)).toISOString().replace(".000Z", "Z");
}
const today = () => { const p = parts(Date.now()); return `${p.year}-${p.month}-${p.day}`; };

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(API + path, { ...opts, headers });
  if (res.status === 401) { logout(); throw new Error("Сесія завершилась, увійдіть знову"); }
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body));
  return body;
}

function toast(msg, bad = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (bad ? " bad" : ""); t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 4000);
}

// ---------- Вхід ----------
async function login(e) {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    const r = await api("/auth/token", { method: "POST", json: { username: fd.get("username"), password: fd.get("password") } });
    state.token = r.access_token; sessionStorage.setItem("token", r.access_token);
    start();
  } catch (err) { $("#login-error").textContent = err.message; }
}
function logout() { state.token = null; sessionStorage.removeItem("token"); location.reload(); }

async function start() {
  try { state.user = await api("/auth/me"); } catch { $("#login-view").hidden = false; return; }
  $("#login-view").hidden = true; $("#app-view").hidden = false;
  $("#whoami").textContent = `${state.user.username} (${state.user.roles.join(", ")})`;
  $("#f-date").value = $("#r-date").value = $("#a-date").value = $("#t-date").value = today();
  loadFlights(); connectWs();
}

// ---------- Рейси ----------
async function loadFlights() {
  const p = new URLSearchParams({ date: $("#f-date").value });
  for (const [k, sel] of [["direction", "#f-dir"], ["status", "#f-status"], ["q", "#f-q"]]) if ($(sel).value) p.set(k, $(sel).value);
  const [flights, board] = await Promise.all([api(`/flights?${p}`), api(`/public/flights?date=${$("#f-date").value}`)]);
  state.flights = new Map(flights.map((f) => [f.id, f]));
  state.board = new Map(board.map((b) => [b.id, b]));
  renderFlights();
}

function renderFlights(flashId) {
  const rows = [...state.flights.values()].sort((a, b) => a.scheduled_at.localeCompare(b.scheduled_at));
  $("#flights-body").innerHTML = rows.map((f) => {
    const b = state.board.get(f.id) || {};
    const res = f.direction === "D" ? b.desks : b.belt;
    return `<tr data-id="${f.id}" class="${f.id === flashId ? "flash" : ""}">
      <td><b>${esc(f.flight_no)}</b></td><td>${f.direction === "D" ? "↗ Відпр." : "↘ Приб."}</td><td>${esc(f.station)}</td>
      <td>${hm(f.scheduled_at)}</td><td>${hm(f.estimated_at)}</td><td>${hm(f.actual_at)}</td>
      <td><span class="badge st-${f.status}">${STATUS_UK[f.status] || f.status}</span></td>
      <td>${esc(f.aircraft_type)}</td><td>${esc(b.stand)}</td><td>${esc(b.gate)}</td><td>${esc(res)}</td><td class="muted">${f.version}</td>
      <td class="row-actions">
        <button class="secondary" data-act="status">Статус</button><button class="secondary" data-act="edit">Змінити</button>
        <button class="secondary" data-act="history">Історія</button><button class="danger" data-act="delete">✕</button></td></tr>`;
  }).join("") || `<tr><td colspan="13" class="muted">Рейсів на цю дату немає. Створіть рейс або імпортуйте розклад.</td></tr>`;
}

function openFlightForm(f) {
  state.editing = f || null;
  const form = $("#flight-form"); form.reset(); $("#flight-error").textContent = "";
  $("#flight-form-title").textContent = f ? `Зміна рейсу ${f.flight_no} (версія ${f.version})` : "Новий рейс";
  for (const n of ["airline", "number", "direction"]) form.elements[n].disabled = !!f;
  if (f) {
    for (const n of ["airline", "number", "direction", "station", "aircraft_type", "terminal"]) form.elements[n].value = f[n] ?? "";
    form.elements.scheduled_at.value = toLocalInput(f.scheduled_at);
    form.elements.estimated_at.value = toLocalInput(f.estimated_at);
    form.elements.schengen.value = String(f.schengen);
  } else form.elements.scheduled_at.value = `${$("#f-date").value}T12:00`;
  $("#dlg-flight").showModal();
}

async function saveFlight(e) {
  if (e.submitter?.value === "cancel") return;
  e.preventDefault();
  const el = e.target.elements;
  const data = { station: el.station.value, aircraft_type: el.aircraft_type.value, scheduled_at: fromLocalInput(el.scheduled_at.value),
    terminal: el.terminal.value || null, schengen: el.schengen.value === "true" };
  try {
    if (state.editing) {
      const f = state.editing;
      const patch = { version: f.version, reason: el.reason.value || null };
      for (const [k, v] of Object.entries({ ...data, estimated_at: fromLocalInput(el.estimated_at.value) })) {
        const old = k.endsWith("_at") ? (f[k] ? new Date(f[k]).toISOString() : null) : f[k];
        const nv = k.endsWith("_at") && v ? new Date(v).toISOString() : v;
        if (old !== nv) patch[k] = v;
      }
      upsert(await api(`/flights/${f.id}`, { method: "PATCH", json: patch }));
    } else {
      upsert(await api("/flights", { method: "POST", json: { ...data, airline: el.airline.value, number: el.number.value, direction: el.direction.value } }));
    }
    $("#dlg-flight").close(); toast("Збережено");
  } catch (err) { $("#flight-error").textContent = err.message; }
}

function openStatus(f) {
  state.editing = f;
  $("#status-flight").textContent = `${f.flight_no} (${STATUS_UK[f.status]})`;
  const opts = (TRANSITIONS[f.direction][f.status] || []);
  $("#status-select").innerHTML = opts.map((s) => `<option value="${s}">${STATUS_UK[s]}</option>`).join("");
  $("#status-form").reset(); $("#status-error").textContent = "";
  $("#dlg-status").showModal();
}

async function saveStatus(e) {
  if (e.submitter?.value === "cancel") return;
  e.preventDefault();
  const el = e.target.elements, f = state.editing;
  try {
    upsert(await api(`/flights/${f.id}/status`, { method: "POST", json: { status: el.status.value, reason: el.reason.value || null, at: fromLocalInput(el.at.value), version: f.version } }));
    $("#dlg-status").close();
  } catch (err) { $("#status-error").textContent = err.message; }
}

// Значення часу в історії показуються в обраному поясі, а не сирим ISO-рядком (дефект D-15)
const fmtVal = (v) => (v == null ? "-" : /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(String(v)) ? `${toLocalInput(v).replace("T", " ")}` : v);

async function showHistory(f) {
  const h = await api(`/flights/${f.id}/history`);
  $("#history-flight").textContent = f.flight_no;
  $("#history-body").innerHTML = h.map((e) => `<tr><td>${new Date(e.occurred_at).toLocaleString("uk-UA")}</td><td>${e.event_type}</td><td>${esc(e.changed_by)}</td>
    <td style="white-space:normal">${e.changes.map((c) => `<div><b>${esc(c.field)}</b>: ${esc(fmtVal(c.old))} → ${esc(fmtVal(c.new))}</div>`).join("")}</td><td>${esc(e.reason)}</td></tr>`).join("");
  $("#dlg-history").showModal();
}

async function deleteFlight(f) {
  if (!confirm(`Видалити рейс ${f.flight_no}? Дозволено лише для помилково створених рейсів.`)) return;
  try { await api(`/flights/${f.id}`, { method: "DELETE" }); state.flights.delete(f.id); renderFlights(); toast("Рейс видалено"); }
  catch (err) { toast(err.message, true); }
}

function upsert(f) {
  if (f.sched_date === $("#f-date").value) state.flights.set(f.id, f);
  renderFlights(f.id);
}

// ---------- Ресурси ----------
async function loadResources() {
  const d = $("#r-date").value, type = $("#r-type").value;
  const [resources, allocs, conflicts] = await Promise.all([api(`/resources?type=${type}`), api(`/allocations?date=${d}`), api(`/allocations/conflicts?date=${d}`)]);
  $("#conflicts").innerHTML = conflicts.map((c) => `<li><b>${esc(c.flight_no)}</b> - ${esc(c.message)}</li>`).join("") || "<li class='muted'>Конфліктів немає</li>";
  const day0 = Date.parse(fromLocalInput(d + "T00:00")), pct = (iso) => Math.max(0, Math.min(100, (Date.parse(iso) - day0) / 864e5 * 100));
  const ticks = Array.from({ length: 13 }, (_, i) => `<span style="left:${i * 100 / 12}%">${String(i * 2).padStart(2, "0")}:00</span>`).join("");
  const lanes = resources.map((r) => {
    const bars = allocs.filter((a) => a.resource_id === r.id).map((a) => {
      const l = pct(a.start_at), w = Math.max(pct(a.end_at) - l, .6);
      return `<div class="bar ${a.manual ? "manual" : ""}" style="left:${l}%;width:${w}%" title="${esc(a.flight_no)} ${hm(a.start_at)}-${hm(a.end_at)}"
        data-flight="${a.flight_id}" data-kind="${a.kind}" data-no="${esc(a.flight_no)}">${esc(a.flight_no)}</div>`;
    }).join("");
    return `<div class="lane"><div class="name">${esc(r.code)}${r.contact ? " ⎍" : ""}${r.zone !== "ANY" ? ` <span class="muted">${r.zone === "SCHENGEN" ? "S" : "NS"}</span>` : ""}</div><div class="track">${bars}</div></div>`;
  }).join("");
  $("#gantt").innerHTML = `<div class="scale"><div></div><div class="ticks">${ticks}</div></div>${lanes}`;
}

function openAssign(bar) {
  state.assign = { flight: bar.dataset.flight, kind: bar.dataset.kind };
  $("#assign-info").textContent = `Рейс ${bar.dataset.no}, тип ресурсу ${bar.dataset.kind}. Для стійок реєстрації вкажіть два коди.`;
  $("#assign-form").reset(); $("#assign-error").textContent = "";
  $("#dlg-assign").showModal();
}

async function saveAssign(e) {
  if (e.submitter?.value === "cancel") return;
  e.preventDefault();
  const codes = e.target.elements.codes.value.split(",").map((s) => s.trim()).filter(Boolean);
  try {
    await api(`/allocations/${state.assign.flight}`, { method: "PUT", json: { kind: state.assign.kind, resource_codes: codes } });
    $("#dlg-assign").close(); toast("Ресурс перепризначено"); loadResources();
  } catch (err) { $("#assign-error").textContent = err.message; }
}

// ---------- A-CDM ----------
async function loadAcdm() {
  const rows = await api(`/acdm/flights?date=${$("#a-date").value}&direction=D`);
  state.acdm = new Map(rows.map((r) => [r.flight_id, r]));
  $("#acdm-body").innerHTML = rows.map((r) => {
    const m = r.milestones, late = r.tsat && r.tobt && r.tsat !== r.tobt;
    return `<tr data-id="${r.flight_id}"><td><b>${esc(r.flight_no)}</b></td><td>${hm(r.scheduled_at)}</td><td>${hm(r.estimated_at)}</td>
      <td>${hm(r.tobt)}${r.tobt_confirmed ? " ✓" : ""}</td><td${late ? ' style="color:var(--warn);font-weight:600"' : ""}>${hm(r.tsat)}</td>
      <td>${hm(m.ASBT?.value)}</td><td>${hm(m.ARDT?.value)}</td><td>${hm(m.AOBT?.value)}</td>
      <td><span class="badge st-${r.status}">${STATUS_UK[r.status] || r.status}</span></td>
      <td class="row-actions"><button class="secondary" data-act="tobt">TOBT</button><button class="secondary" data-act="dpi">DPI</button></td></tr>`;
  }).join("") || `<tr><td colspan="10" class="muted">Рейсів відправлення на цю дату немає</td></tr>`;
}

async function saveTobt(e) {
  if (e.submitter?.value === "cancel") return;
  e.preventDefault();
  try {
    await api(`/acdm/flights/${state.tobtFlight}/tobt`, { method: "PUT", json: { tobt: fromLocalInput(e.target.elements.tobt.value) } });
    $("#dlg-tobt").close(); toast("TOBT підтверджено, TSAT перераховано"); loadAcdm();
  } catch (err) { $("#tobt-error").textContent = err.message; }
}

// ---------- Наземне обслуговування ----------
const TASK_UK = { PLANNED: "Заплановано", IN_PROGRESS: "Виконується", DONE: "Виконано", CANCELLED: "Скасовано" };
async function loadTasks() {
  const p = new URLSearchParams({ date: $("#t-date").value });
  if ($("#t-team").value) p.set("team", $("#t-team").value);
  if ($("#t-status").value) p.set("status", $("#t-status").value);
  if ($("#t-overdue").checked) p.set("overdue", "true");
  const rows = await api(`/tasks?${p}`);
  state.tasks = new Map(rows.map((t) => [t.id, t]));
  const cls = { PLANNED: "st-SCHEDULED", IN_PROGRESS: "st-BOARDING", DONE: "st-ARRIVED", CANCELLED: "st-CANCELLED" };
  $("#tasks-body").innerHTML = rows.map((t) => `<tr data-id="${t.id}"${t.overdue ? ' style="background:#fff5f5"' : ""}>
    <td>${hm(t.planned_start)}-${hm(t.planned_end)}</td><td><b>${esc(t.flight_no)}</b></td><td>${esc(t.name)}</td><td>${esc(t.team)}</td>
    <td><span class="badge ${cls[t.status]}">${TASK_UK[t.status]}</span>${t.overdue ? ' <span class="badge st-CANCELLED">SLA</span>' : ""}</td>
    <td>${hm(t.actual_start)}</td><td>${hm(t.actual_end)}</td><td>${esc(t.assignee)}</td>
    <td class="row-actions">${t.status === "PLANNED" ? '<button class="secondary" data-act="start">Почати</button>' : ""}
      ${["PLANNED", "IN_PROGRESS"].includes(t.status) ? '<button class="primary" data-act="complete">Завершити</button>' : ""}</td></tr>`).join("")
    || `<tr><td colspan="9" class="muted">Завдань немає</td></tr>`;
}

async function taskAction(t, action) {
  try { await api(`/tasks/${t.id}/${action}`, { method: "POST", json: { version: t.version } }); loadTasks(); }
  catch (err) { toast(err.message, true); loadTasks(); }
}

// ---------- Розклад ----------
async function importSsim(e) {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    const res = await fetch(`${API}/schedules/imports`, { method: "POST", body: fd, headers: { Authorization: `Bearer ${state.token}` } });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail);
    $("#ssim-result").textContent = `Створено: ${body.created}, оновлено: ${body.updated}, без змін: ${body.skipped}\n` +
      body.errors.map((x) => `Рядок ${x.line}: ${x.message}`).join("\n");
  } catch (err) { toast(err.message, true); }
}

// ---------- Оновлення в реальному часі ----------
function connectWs() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/fids`);
  ws.onopen = () => $("#live").classList.add("on");
  ws.onclose = () => { $("#live").classList.remove("on"); setTimeout(connectWs, 3000); };
  ws.onmessage = async (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "fids.update" && msg.flight) {
      state.board.set(msg.flight.id, msg.flight);
      if (msg.flight.sched_date === $("#f-date").value) {
        const f = state.flights.get(msg.flight.id);
        if (!f || f.version < msg.flight.version) { try { state.flights.set(msg.flight.id, await api(`/flights/${msg.flight.id}`)); } catch {} }
        renderFlights(msg.flight.id);
      }
      if (!$("#tab-resources").hidden) loadResources();
    } else if (msg.type === "fids.remove") { state.flights.delete(msg.id); renderFlights(); }
    else if (msg.type === "resource.conflict") toast(`Конфлікт ресурсів: ${msg.message}`, true);
    else if (msg.type === "event") {
      clearTimeout(state.evh);
      state.evh = setTimeout(() => {
        if (!$("#tab-acdm").hidden) loadAcdm();
        if (!$("#tab-tasks").hidden) loadTasks();
      }, 400);
      if (msg.event === "TaskOverdue") toast(`Прострочено: ${msg.payload.flight_no} - ${msg.payload.name}`, true);
    }
  };
}

// ---------- Події інтерфейсу ----------
$("#login-form").addEventListener("submit", login);
$("#logout").addEventListener("click", logout);
$("#btn-new").addEventListener("click", () => openFlightForm());
$("#flight-form").addEventListener("submit", saveFlight);
$("#status-form").addEventListener("submit", saveStatus);
$("#assign-form").addEventListener("submit", saveAssign);
$("#ssim-form").addEventListener("submit", importSsim);
["#f-date", "#f-dir", "#f-status"].forEach((s) => $(s).addEventListener("change", loadFlights));
$("#f-q").addEventListener("input", () => { clearTimeout(state.qh); state.qh = setTimeout(loadFlights, 300); });
["#r-date", "#r-type"].forEach((s) => $(s).addEventListener("change", loadResources));
$("#btn-auto").addEventListener("click", async () => {
  try { const r = await api(`/allocations/auto?date=${$("#r-date").value}`, { method: "POST" }); toast(`Перераховано рейсів: ${r.flights}`); loadResources(); }
  catch (err) { toast(err.message, true); }
});
$("#f-status").innerHTML += Object.entries(STATUS_UK).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
$("#flights-body").addEventListener("click", (e) => {
  const btn = e.target.closest("button"); if (!btn) return;
  const f = state.flights.get(btn.closest("tr").dataset.id);
  ({ status: openStatus, edit: openFlightForm, history: showHistory, delete: deleteFlight })[btn.dataset.act](f);
});
$("#gantt").addEventListener("click", (e) => { const bar = e.target.closest(".bar[data-flight]"); if (bar) openAssign(bar); });
document.querySelectorAll("header nav button[data-tab]").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("header nav button[data-tab]").forEach((x) => x.classList.toggle("active", x === b));
  for (const t of ["flights", "resources", "acdm", "tasks", "schedule"]) $(`#tab-${t}`).hidden = t !== b.dataset.tab;
  ({ resources: loadResources, acdm: loadAcdm, tasks: loadTasks })[b.dataset.tab]?.();
}));

$("#a-date").addEventListener("change", loadAcdm);
$("#tz").value = state.tz;
$("#tz").addEventListener("change", () => {
  state.tz = $("#tz").value; sessionStorage.setItem("tz", state.tz);
  renderFlights(); if (!$("#tab-resources").hidden) loadResources(); if (!$("#tab-acdm").hidden) loadAcdm(); if (!$("#tab-tasks").hidden) loadTasks();
});
["#t-date", "#t-team", "#t-status", "#t-overdue"].forEach((s) => $(s).addEventListener("change", loadTasks));
$("#tobt-form").addEventListener("submit", saveTobt);
$("#acdm-body").addEventListener("click", async (e) => {
  const btn = e.target.closest("button"); if (!btn) return;
  const r = state.acdm.get(btn.closest("tr").dataset.id);
  if (btn.dataset.act === "tobt") {
    state.tobtFlight = r.flight_id; $("#tobt-flight").textContent = r.flight_no;
    $("#tobt-form").reset(); $("#tobt-error").textContent = ""; $("#tobt-form").elements.tobt.value = toLocalInput(r.tobt);
    $("#dlg-tobt").showModal();
  } else {
    const msgs = await api(`/acdm/flights/${r.flight_id}/dpi`);
    $("#history-flight").textContent = `${r.flight_no}: повідомлення DPI`;
    $("#history-body").innerHTML = msgs.map((m) => `<tr><td>${new Date(m.created_at).toLocaleString("uk-UA")}</td><td>${m.kind}</td><td colspan="3" style="white-space:normal;font-family:monospace">${esc(m.body)}</td></tr>`).join("");
    $("#dlg-history").showModal();
  }
});
$("#tasks-body").addEventListener("click", (e) => {
  const btn = e.target.closest("button"); if (!btn) return;
  taskAction(state.tasks.get(btn.closest("tr").dataset.id), btn.dataset.act);
});

if (state.token) start(); else $("#login-view").hidden = false;
