"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function fmtBytes(n) {
  if (n == null) return "?";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}
const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleString() : "never");
const fmtDuration = (s) => (s == null ? "" : s < 1 ? "less than 1 s" : s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`);

async function api(method, url, body) {
  const headers = { "Content-Type": "application/json" };
  const token = localStorage.getItem("apiToken");
  if (token) headers.Authorization = `Bearer ${token}`;
  const resp = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (resp.status === 401) {
    const entered = prompt("API token required:");
    if (entered) { localStorage.setItem("apiToken", entered); return api(method, url, body); }
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || resp.statusText);
  return data;
}

// Tabs and links: #<tab> opens a tab, #manual/<section> a section of the manual
const TAB_LOADERS = { cameras: loadCameras, nas: loadNas, history: loadHistory, status: loadNasHealth, manual: loadManual };

async function route() {
  const [requested, section] = location.hash.slice(1).split("/");
  const tab = requested && $(`#tab-${requested}`) ? requested : "status";
  document.querySelectorAll("button.tab, .panel").forEach((el) => el.classList.remove("active"));
  document.querySelector(`button.tab[data-tab="${tab}"]`).classList.add("active");
  $(`#tab-${tab}`).classList.add("active");
  await TAB_LOADERS[tab]?.();
  const target = section && document.getElementById(`manual-${section}`);
  if (target) target.scrollIntoView({ block: "start" });
  else window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);
document.querySelectorAll("button.tab").forEach((btn) => btn.addEventListener("click", () => {
  if (location.hash === `#${btn.dataset.tab}`) route();
  else location.hash = btn.dataset.tab;
}));

// Manual
let manualLoaded = false;
async function loadManual() {
  if (manualLoaded) return;
  const resp = await fetch("/static/manual.html");
  $("#manual-body").innerHTML = await resp.text();
  $("#manual-body").classList.remove("muted");
  document.querySelectorAll(".js-origin").forEach((el) => (el.textContent = location.origin));
  if (authRequired) {
    document.querySelectorAll(".js-token").forEach((el) => (el.textContent = ' -H "Authorization: Bearer <token>"'));
    document.querySelectorAll(".js-token-ha").forEach((el) => (el.textContent = '\n    headers:\n      Authorization: "Bearer <token>"'));
  }
  manualLoaded = true;
  // Wait for the pictures so that jumping to a section isn't undone by them growing the page afterwards
  const media = [...$("#manual-body").querySelectorAll("img, object")];
  await Promise.race([
    Promise.all(media.map((el) => new Promise((ok) => { el.addEventListener("load", ok, { once: true }); el.addEventListener("error", ok, { once: true }); if (el.complete) ok(); }))),
    new Promise((ok) => setTimeout(ok, 2000)),
  ]);
}

// Help pop-overs next to form fields
function togglePop(trigger) {
  const pop = document.getElementById(trigger.dataset.pop);
  if (pop.matches(":popover-open")) { pop.hidePopover(); return; }
  pop.showPopover();
  const r = trigger.getBoundingClientRect();
  pop.style.top = `${Math.min(r.bottom + 6, innerHeight - pop.offsetHeight - 8)}px`;
  pop.style.left = `${Math.max(8, Math.min(r.left - 12, innerWidth - pop.offsetWidth - 8))}px`;
}
document.addEventListener("click", (ev) => {
  const trigger = ev.target.closest(".info[data-pop]");
  if (trigger) { ev.preventDefault(); togglePop(trigger); }
});
document.addEventListener("keydown", (ev) => {
  const trigger = ev.target.closest?.(".info[data-pop]");
  if (trigger && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); togglePop(trigger); }
});

// Setup checklist (welcome dialog and Status tab)
let authRequired = false;
let setup = { nas: false, nasTarget: "", cameras: [], synced: false };

async function refreshSetup() {
  try {
    const [{ status }, { cameras }] = await Promise.all([api("GET", "/api/nas"), api("GET", "/api/cameras")]);
    setup = {
      nas: status.mounted && status.writable && !status.errors.length,
      nasTarget: status.target_dir,
      cameras,
      synced: cameras.some((c) => c.last_sync_at && !c.last_error),
    };
  } catch (e) { return; }
  const items = [
    { done: setup.nas, title: "Connect the NAS", href: "#nas",
      text: setup.nas ? `Files go to ${setup.nasTarget}` : "Enter your NAS share in the NAS tab, test it and save.",
      help: "#manual/nas" },
    { done: setup.cameras.length > 0, title: "Prepare and pair the camera", href: "#cameras",
      text: setup.cameras.length ? `Paired: ${setup.cameras.map((c) => c.name).join(", ")}`
        : "Turn on wireless on the camera, open its pairing screen, then click Pair camera.",
      help: "#manual/camera" },
    { done: setup.synced, title: "Copy your files", href: "#status",
      text: setup.synced ? "Your first sync has completed." : "Put the camera near the Pi and click Sync now.",
      help: "#manual/sync" },
  ];
  const html = items.map((it, i) =>
    `<li class="${it.done ? "done" : ""}"><span class="num">${it.done ? "\u2713" : i + 1}</span><div>` +
    `<a href="${it.href}" data-close><strong>${esc(it.title)}</strong></a><div class="muted">${esc(it.text)}</div>` +
    `<a class="small" href="${it.help}" data-close>How?</a></div></li>`).join("");
  $("#welcome-checklist").innerHTML = html;
  $("#status-checklist").innerHTML = html;
  $("#getting-started").hidden = items.every((it) => it.done);
  $("#welcome-start").textContent = items.every((it) => it.done) ? "Go to status" : "Start setup";
}

function nextSetupTab() {
  if (!setup.nas) return "nas";
  if (!setup.cameras.length) return "cameras";
  return "status";
}

const welcome = $("#welcome");
$("#open-welcome").addEventListener("click", () => { refreshSetup(); welcome.showModal(); });
welcome.addEventListener("click", (ev) => {
  if (ev.target.closest("a[data-close]")) welcome.close();
  else if (ev.target === welcome) welcome.close(); // click on the backdrop
});
welcome.addEventListener("close", () => {
  localStorage.setItem("welcomeSeen", "1");
  if (welcome.returnValue === "start") location.hash = nextSetupTab();
  welcome.returnValue = "";
});

// Status
const PHASES = {
  starting: "Starting...", preflight: "Checking NAS...", connecting: "Connecting to camera...",
  listing: "Reading media list...", transferring: "Transferring", finishing: "Finishing...", pairing: "Pairing...",
};

const STEP_ICONS = { done: "\u2713", running: "", failed: "\u2717", stopped: "\u25A0" };

function renderSteps(steps) {
  return (steps || []).map((st) =>
    `<li class="step ${st.state}"><span class="icon">${STEP_ICONS[st.state] ?? ""}</span>` +
    `<span class="label">${esc(st.label)}</span>` +
    `<span class="elapsed">${fmtDuration(st.elapsed)}</span>` +
    (st.detail ? `<div class="detail">${esc(st.detail)}</div>` : "") +
    (st.error ? `<div class="detail err">${esc(st.error)}</div>` : "") + "</li>").join("");
}

function renderStatus(s) {
  const job = s.job;
  $("#sync-now").disabled = s.busy;
  $("#pair").disabled = s.busy;
  $("#stop").hidden = !s.busy;
  $("#steps").hidden = !job;
  if (!job) {
    $("#phase").textContent = "Idle";
    $("#current").hidden = true;
  } else {
    $("#phase").textContent = PHASES[job.phase] || job.phase;
    $("#steps").innerHTML = renderSteps(job.steps);
    const cur = job.current;
    $("#current").hidden = !cur;
    if (cur) {
      const pct = cur.bytes_total ? (100 * cur.bytes_done) / cur.bytes_total : 0;
      $("#current-file").textContent = `${cur.camera_path}  ->  ${cur.nas_path}`;
      $("#progress-bar").style.width = `${pct.toFixed(1)}%`;
      $("#current-stats").textContent =
        `${fmtBytes(cur.bytes_done)} of ${fmtBytes(cur.bytes_total)} (${pct.toFixed(1)}%) at ${fmtBytes(cur.speed)}/s` +
        (cur.eta ? `, about ${fmtDuration(cur.eta)} left` : "");
      $("#queue").textContent = `File ${job.queue_index} of ${job.queue_total}`;
    }
    if (job.kind === "pair") $("#pair-status").textContent = job.message;
  }
  const last = s.last_job;
  if (last) {
    const cams = last.cameras.map((c) =>
      `<li>${esc(c.name)}: ${c.files_done} of ${c.files_new} new file(s), ${fmtBytes(c.bytes_done)}` +
      (c.error ? ` <span class="err">- ${esc(c.error)}</span>` : "") + "</li>").join("");
    $("#last-job").innerHTML =
      `<div>${esc(last.kind)} ${last.phase === "stopped" ? '<span class="muted">stopped</span>' : last.error ? '<span class="err">finished with errors</span>' : '<span class="ok">succeeded</span>'} ` +
      `at ${fmtTime(last.finished_at)}</div><div>${esc(last.message)}</div>` +
      (last.error ? `<div class="err">${esc(last.error)}</div>` : "") + (cams ? `<ul>${cams}</ul>` : "") +
      (last.steps?.length ? `<details><summary>Steps</summary><ol class="steps">${renderSteps(last.steps)}</ol></details>` : "");
    if (last.kind === "pair") {
      $("#pair-status").innerHTML = last.error ? `<span class="err">${esc(last.error)}</span>` : esc(last.message);
    }
  }
}

function connectEvents() {
  const source = new EventSource("/api/events");
  let lastFinished = null;
  source.onmessage = (ev) => {
    const s = JSON.parse(ev.data);
    renderStatus(s);
    const finished = s.last_job && s.last_job.finished_at;
    if (finished && finished !== lastFinished) {
      lastFinished = finished;
      loadNasHealth();
      loadCameras();
      refreshSetup();
    }
  };
  source.onerror = () => { source.close(); setTimeout(connectEvents, 3000); };
}

$("#sync-now").addEventListener("click", async () => {
  try { await api("POST", "/api/sync"); } catch (e) { alert(e.message); }
});
$("#stop").addEventListener("click", async () => {
  if (!confirm("Stop now? Files already transferred are kept; the file in progress is discarded and transferred again next time.")) return;
  try { await api("POST", "/api/sync/stop"); } catch (e) { alert(e.message); }
});

async function loadNasHealth() {
  try {
    const { status } = await api("GET", "/api/nas");
    const errors = status.errors.map((e) => `<div class="err">${esc(e)}</div>`).join("");
    $("#nas-health").innerHTML =
      `<div>${esc(status.type.toUpperCase())} target <span class="filename">${esc(status.target_dir)}</span></div>` +
      `<div>${status.mounted && status.writable ? '<span class="ok">Mounted and writable</span>' : '<span class="err">Not ready</span>'}` +
      (status.free_bytes != null ? `, ${fmtBytes(status.free_bytes)} free` : "") + "</div>" +
      (status.route ? `<div class="${status.route.ok ? "muted" : "err"}">${esc(status.route.message)}</div>` : "") + errors;
  } catch (e) { $("#nas-health").textContent = e.message; }
}

// Cameras
async function loadCameras() {
  const data = await api("GET", "/api/cameras");
  const opts = ['<option value="">All cameras</option>'];
  $("#camera-rows").innerHTML = data.cameras.map((c) => {
    opts.push(`<option value="${esc(c.serial)}">${esc(c.name)}</option>`);
    return `<tr>
      <td><input value="${esc(c.name)}" data-rename="${esc(c.serial)}"></td>
      <td>${esc(c.model_name)}${c.notes ? `<div class="muted">${esc(c.notes)}</div>` : ""}</td>
      <td class="mono">${esc(c.serial)}</td><td>${esc(c.firmware)}</td>
      <td>${fmtTime(c.last_sync_at)}${c.last_error ? `<div class="err">${esc(c.last_error)}</div>` : ""}</td>
      <td><input type="checkbox" ${c.enabled ? "checked" : ""} data-enable="${esc(c.serial)}"></td>
      <td><button data-sync="${esc(c.serial)}">Sync</button> <button class="danger" data-forget="${esc(c.serial)}">Forget</button></td>
    </tr>`;
  }).join("") || '<tr><td colspan="7" class="muted">No camera paired yet.</td></tr>';
  const sel = $("#history-camera"), current = sel.value;
  sel.innerHTML = opts.join("");
  sel.value = current;
}

$("#camera-rows").addEventListener("change", async (ev) => {
  const t = ev.target;
  try {
    if (t.dataset.rename) await api("PATCH", `/api/cameras/${t.dataset.rename}`, { name: t.value });
    if (t.dataset.enable) await api("PATCH", `/api/cameras/${t.dataset.enable}`, { enabled: t.checked });
  } catch (e) { alert(e.message); }
});
$("#camera-rows").addEventListener("click", async (ev) => {
  const t = ev.target;
  try {
    if (t.dataset.sync) await api("POST", `/api/sync?camera=${encodeURIComponent(t.dataset.sync)}`);
    if (t.dataset.forget && confirm("Forget this camera? Its transfer history is kept.")) {
      await api("DELETE", `/api/cameras/${t.dataset.forget}`);
      loadCameras();
    }
  } catch (e) { alert(e.message); }
});
$("#pair").addEventListener("click", async () => {
  try {
    await api("POST", "/api/cameras/pair");
    $("#pair-status").textContent = "Scanning for a camera in pairing mode...";
  } catch (e) { alert(e.message); }
});

// NAS
const form = $("#nas-form");
function updateNasVisibility() {
  const type = form.type.value;
  form.querySelectorAll(".only-remote").forEach((el) => (el.hidden = type === "path"));
  form.querySelectorAll(".only-smb").forEach((el) => (el.hidden = type !== "smb"));
  form.querySelectorAll(".only-nfs").forEach((el) => (el.hidden = type !== "nfs"));
  form.querySelectorAll(".only-path").forEach((el) => (el.hidden = type !== "path"));
}
form.type.addEventListener("change", updateNasVisibility);

async function loadNas() {
  const { config, layout } = await api("GET", "/api/nas");
  for (const [k, v] of Object.entries(config)) if (form[k]) form[k].value = v ?? "";
  form.layout.value = layout;
  form.password.value = "";
  updateNasVisibility();
}

function nasPayload() {
  const data = Object.fromEntries(new FormData(form).entries());
  if (!data.password) delete data.password;
  return data;
}

function showNasResult(text, ok) {
  const el = $("#nas-result");
  el.hidden = false;
  el.className = ok ? "ok" : "err";
  el.textContent = text;
}

$("#nas-test").addEventListener("click", async () => {
  try {
    const r = await api("POST", "/api/nas/test", nasPayload());
    showNasResult(r.messages.join("\n"), r.ok);
  } catch (e) { showNasResult(e.message, false); }
});
form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const r = await api("PUT", "/api/nas", nasPayload());
    const errs = r.status.errors;
    showNasResult(errs.length ? errs.join("\n") : `Saved. ${r.status.target_dir} is mounted and writable.`, !errs.length);
    loadNas();
    refreshSetup();
  } catch (e) { showNasResult(e.message, false); }
});
$("#nas-mount").addEventListener("click", async () => {
  try { await api("POST", "/api/nas/mount"); showNasResult("Mounted.", true); } catch (e) { showNasResult(e.message, false); }
});
$("#nas-unmount").addEventListener("click", async () => {
  try { await api("POST", "/api/nas/unmount"); showNasResult("Unmounted.", true); } catch (e) { showNasResult(e.message, false); }
});

// History
let searchTimer;
async function loadHistory() {
  const params = new URLSearchParams({ search: $("#search").value, camera: $("#history-camera").value });
  if (!params.get("camera")) params.delete("camera");
  const data = await api("GET", `/api/transferred?${params}`);
  $("#history-rows").innerHTML = data.items.map((f) => `<tr>
      <td class="mono">${esc(f.camera_path)}</td><td>${fmtBytes(f.size)}</td>
      <td class="mono">${esc(f.nas_path)}</td><td>${fmtTime(f.transferred_at)}</td>
      <td><button data-remove="${f.id}" title="Transfer this file again on the next sync">Re-transfer</button></td>
    </tr>`).join("") || '<tr><td colspan="5" class="muted">Nothing transferred yet.</td></tr>';
  $("#history-total").textContent = `${data.total} file(s)` + (data.total > data.items.length ? `, showing ${data.items.length}` : "");
}
$("#search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadHistory, 300); });
$("#history-camera").addEventListener("change", loadHistory);
$("#history-rows").addEventListener("click", async (ev) => {
  const id = ev.target.dataset.remove;
  if (!id) return;
  try { await api("DELETE", `/api/transferred/${id}`); loadHistory(); } catch (e) { alert(e.message); }
});
$("#reset-all").addEventListener("click", async () => {
  const camera = $("#history-camera").value;
  if (!confirm(camera ? "Reset the list for this camera? All its files will be transferred again." :
    "Reset the whole list? All files on all cameras will be transferred again.")) return;
  try {
    await api("DELETE", "/api/transferred" + (camera ? `?camera=${encodeURIComponent(camera)}` : ""));
    loadHistory();
  } catch (e) { alert(e.message); }
});

// Init
(async () => {
  const s = await api("GET", "/api/status");
  authRequired = s.auth_required;
  $("#footer").innerHTML = `gopro-nas-sync ${esc(s.version)} · <a href="#manual">Manual</a>`;
  renderStatus(s);
  connectEvents();
  loadCameras();
  await Promise.all([route(), refreshSetup()]);
  if (!localStorage.getItem("welcomeSeen")) welcome.showModal();
})();
