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

// Tabs
document.querySelectorAll("button.tab").forEach((btn) => btn.addEventListener("click", () => {
  document.querySelectorAll("button.tab, .panel").forEach((el) => el.classList.remove("active"));
  btn.classList.add("active");
  $(`#tab-${btn.dataset.tab}`).classList.add("active");
  ({ cameras: loadCameras, nas: loadNas, history: loadHistory, status: loadNasHealth })[btn.dataset.tab]?.();
}));

// Status
const PHASES = {
  starting: "Starting...", preflight: "Checking NAS...", connecting: "Connecting to camera...",
  listing: "Reading media list...", transferring: "Transferring", finishing: "Finishing...", pairing: "Pairing...",
};

function renderStatus(s) {
  const job = s.job;
  $("#sync-now").disabled = s.busy;
  $("#pair").disabled = s.busy;
  $("#stop").hidden = !s.busy;
  if (!job) {
    $("#phase").textContent = "Idle";
    $("#current").hidden = true;
  } else {
    $("#phase").textContent = `${PHASES[job.phase] || job.phase} ${job.message ? "- " + job.message : ""}`;
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
      (last.error ? `<div class="err">${esc(last.error)}</div>` : "") + (cams ? `<ul>${cams}</ul>` : "");
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
  $("#pair-hint").textContent = data.pairing_instructions;
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
  $("#footer").textContent = `gopro-nas-sync ${s.version}`;
  renderStatus(s);
  connectEvents();
  loadNasHealth();
  loadCameras();
})();
