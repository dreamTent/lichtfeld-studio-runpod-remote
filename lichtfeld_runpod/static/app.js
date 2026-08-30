const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const state = {
  view: "dash",
  selected: null, // { kind: 'job'|'pod', id }
  data: { jobs: [], pods: [] },
  listFilter: "current",
  reloading: null,
  reloadFlash: null,
  discarding: null,
  aborting: null,
  datasetsDir: "",
};

function showView(name) {
  state.view = name;
  $$("nav [data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  ["dash", "new", "build", "settings"].forEach((v) => {
    const node = $(`#view-${v}`);
    if (node) node.hidden = v !== name;
  });
  if (name === "new") {
    fillDefaultJobName();
    loadJobOptions();
  }
  if (name === "build") {
    fillDefaultBuildName();
    loadBuildOptions();
  }
  if (name === "settings") loadSettings();
}

$$("nav [data-view]").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.disabled) return;
    showView(btn.dataset.view);
  });
});

$$("[data-list]").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.listFilter = btn.dataset.list;
    $$("[data-list]").forEach((b) => b.classList.toggle("active", b.dataset.list === state.listFilter));
    renderLists();
  });
});

function defaultJobName() {
  const letters = Array.from({ length: 6 }, () =>
    String.fromCharCode(97 + Math.floor(Math.random() * 26))
  ).join("");
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  return `${stamp}-${letters}`;
}

function fillDefaultJobName(force = false) {
  const input = $("#job-name");
  if (!input) return;
  if (force || !input.value.trim()) input.value = defaultJobName();
}

function fillDefaultBuildName(force = false) {
  const input = $("#build-name");
  if (!input) return;
  if (force || !input.value.trim()) input.value = defaultJobName();
}

const ORB_TITLE = {
  white: "foreign pod",
  green_blink: "created",
  blue_blink: "waiting",
  green: "running",
  teal: "finished",
  yellow: "connection lost",
  red_blink: "error",
  red: "disconnected",
};

function orb(color) {
  const span = document.createElement("span");
  span.className = `orb ${color || "white"}`;
  span.title = ORB_TITLE[color] || color || "white";
  return span;
}

function mergedRows() {
  const allJobs = state.data.jobs || [];
  const archived = state.listFilter === "archived";
  const jobs = allJobs.filter((j) => Boolean(j.archived) === archived);
  const pods = state.data.pods || [];
  const claimed = new Set(allJobs.map((j) => j.pod_id).filter(Boolean));
  const rows = jobs.map((job) => {
    const pod = pods.find((p) => p.job_id === job.id || p.id === job.pod_id) || null;
    return { kind: "job", id: job.id, job, pod, color: job.color, name: job.name };
  });
  for (const pod of pods) {
    if (pod.controlled || claimed.has(pod.id)) continue;
    rows.push({ kind: "pod", id: pod.id, job: null, pod, color: pod.color, name: pod.name });
  }
  return rows;
}

function formatDateTime(ts) {
  if (!ts) return "—";
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return "—";
  const pad = (n) => String(n).padStart(2, "0");
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return `${date} ${time}`;
}

function connectionErrorNote(n) {
  const c = Number(n) || 0;
  if (c <= 0) return "";
  return c === 1 ? " (1 connection error)" : ` (${c} connection errors)`;
}

function lastUpdateLabel(job, checking) {
  if (checking) return "last update: checking…";
  const when = formatDateTime(job?.last_ssh_ok);
  const base = when === "—" ? "last update: —" : `last update: ${when}`;
  return base + connectionErrorNote(job?.connection_errors) + countdownNote(job);
}

function lastUpdateValue(job, checking) {
  if (checking) return "checking…";
  const when = formatDateTime(job?.last_ssh_ok);
  return when + connectionErrorNote(job?.connection_errors) + countdownNote(job);
}

function countdownNote(job) {
  if (job?.next_check_at == null || ["complete", "error"].includes(job?.phase)) return "";
  const at = Number(job.next_check_at);
  if (!Number.isFinite(at)) return "";
  const kind = job.next_check_kind === "retry" ? "retry" : "poll";
  const left = Math.max(0, Math.ceil(at - Date.now() / 1000));
  if (left <= 0) return kind === "retry" ? " · retrying…" : " · polling…";
  return ` · ${left}s until next ${kind}`;
}

function tickCountdowns() {
  $$("[data-countdown]").forEach((el) => {
    const job = (state.data.jobs || []).find((j) => j.id === el.dataset.countdown);
    if (!job) return;
    const checking = state.reloading === job.id;
    if (el.dataset.countdownWhere === "label") {
      const row = mergedRows().find((r) => r.job?.id === job.id);
      if (row) el.textContent = rowSubtitle(row);
      return;
    }
    el.textContent = lastUpdateValue(job, checking);
  });
  const podList = $("[data-pod-list-countdown]");
  if (podList) podList.textContent = podListCountdownText();
}

function podListCountdownText() {
  const raw = state.data?.next_pod_list_at;
  if (raw == null) return "listing pods…";
  const at = Number(raw);
  if (!Number.isFinite(at)) return "listing pods…";
  const left = Math.max(0, Math.ceil(at - Date.now() / 1000));
  if (left <= 0) return "listing pods…";
  return `${left}s until next pod list`;
}

function rowSubtitle(row) {
  const job = row.job;
  const pod = row.pod;
  if (job) {
    const checking = state.reloading === job.id;
    const bits = [job.kind === "build" ? "build" : null, job.message || job.phase, lastUpdateLabel(job, checking)];
    if (job.gpu) bits.push(job.gpu);
    return bits.filter(Boolean).join(" · ");
  }
  return [pod.status, pod.gpu, "foreign"].filter(Boolean).join(" · ");
}

function renderLists() {
  const list = $("#item-list");
  list.innerHTML = "";
  const rows = mergedRows();
  if (!rows.length) {
    list.innerHTML = `<li class="empty">${state.listFilter === "archived" ? "No archived jobs." : "No jobs yet."}</li>`;
  }
  for (const row of rows) {
    const li = document.createElement("li");
    if (state.selected?.kind === row.kind && state.selected.id === row.id) li.classList.add("selected");
    if (row.job && state.reloading === row.job.id) li.classList.add("reloading");
    if (row.job && state.reloadFlash === row.job.id) li.classList.add("reload-flash");
    li.append(orb(row.color));
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.innerHTML = `<strong>${esc(row.name)}</strong><span${
      row.job ? ` data-countdown="${esc(row.job.id)}" data-countdown-where="label"` : ""
    }>${esc(rowSubtitle(row))}</span>`;
    li.append(meta);
    li.addEventListener("click", () => select({ kind: row.kind, id: row.id }));
    list.append(li);
  }
  if (state.selected && !rows.some((r) => r.kind === state.selected.kind && r.id === state.selected.id)) {
    state.selected = null;
  }
  tickCountdowns();
  renderDetail();
}

function select(sel) {
  state.selected = sel;
  showView("dash");
  renderLists();
  renderDetail();
}

function livePodId(pod) {
  if (!pod?.id) return null;
  if (String(pod.status || "").toUpperCase() === "GONE") return null;
  return pod.id;
}

function renderDetail() {
  const box = $("#detail");
  if (!state.selected) {
    box.innerHTML = `<p class="empty">Select a job.</p>`;
    return;
  }
  const row = mergedRows().find((r) => r.kind === state.selected.kind && r.id === state.selected.id);
  if (!row) {
    state.selected = null;
    box.innerHTML = `<p class="empty">Select a job.</p>`;
    return;
  }
  const job = row.job;
  const pod = row.pod;
  const title = job?.name || pod?.name || "—";
  const kv = [];
  if (job) {
    kv.push(["Status", job.message || job.phase || "—"]);
    const checking = state.reloading === job.id;
    kv.push(["Last update", lastUpdateValue(job, checking), checking ? "checking" : state.reloadFlash === job.id ? "flash" : "", job.id]);
    kv.push(["Stage", job.stage || "—"]);
    kv.push(["GPU", `${job.gpu || "—"} / ${job.cloud || "—"}`]);
    kv.push(["Pod", job.pod_id || pod?.id || "—"]);
    kv.push(["SSH", pod?.ssh || "—"]);
    if (job.kind === "build") {
      kv.push(["Git", job.git_ref || "—"]);
      kv.push(["CUDA arch", job.cuda_arch ? `sm_${job.cuda_arch}` : "—"]);
      kv.push(["Repo", job.repo_url || "—"]);
      kv.push(["Archive", job.build_archive || "—"]);
    } else {
      kv.push(["Build", job.build_archive || "—"]);
      kv.push(["Dataset", job.dataset_archive || job.dataset_local || "—"]);
    }
    kv.push(["Results", job.result_dir || "—"]);
    kv.push(["Download", job.auto_download ? "local + FTP" : "FTP only"]);
    if (job.local_results) kv.push(["Local folder", job.local_results]);
  } else {
    kv.push(["Pod", pod.id]);
    kv.push(["Status", `${pod.status || ""} · ${pod.color}`]);
    kv.push(["GPU", pod.gpu || "—"]);
    kv.push(["SSH", pod.ssh || "—"]);
    kv.push(["Control", "foreign (not started by this app)"]);
  }
  const log = job?.log_tail || job?.message || "";
  const podId = livePodId(pod);
  const discarding = podId && state.discarding === podId;
  const reloadBtn =
    job && job.phase !== "complete"
      ? `<button type="button" data-act="reload"${state.reloading === job.id ? " disabled" : ""}>${
          state.reloading === job.id ? "Reloading…" : "Reload"
        }</button>`
      : "";
  const discardBtn = podId
    ? `<button type="button" data-act="discard" class="danger"${discarding ? " disabled" : ""}>${
        discarding ? "Discarding…" : "Discard pod"
      }</button>`
    : "";
  const aborting = job && state.aborting === job.id;
  const abortBtn =
    job && job.phase === "uploading_dataset"
      ? `<button type="button" data-act="abort-upload" class="danger"${aborting ? " disabled" : ""}>${
          aborting ? "Aborting…" : "Abort upload"
        }</button>`
      : "";
  const openResultsBtn =
    job && job.local_results_ready
      ? `<button type="button" data-act="open-results">Open result folder</button>`
      : "";
  const archiveBtn = job
    ? job.archived
      ? `<button type="button" data-act="unarchive">Unarchive</button>`
      : `<button type="button" data-act="archive">Archive</button>`
    : "";
  const deleteBtn = job ? `<button type="button" data-act="delete" class="danger">Remove from list</button>` : "";
  const note = job
    ? job.local_results_ready
      ? "Open result folder shows the downloaded output on this machine. Archive or remove hides this listing only. FTP results and local downloads stay."
      : podId
      ? "Reload probes SSH and checks FTP for finished results. Discard pod terminates the GPU on RunPod. Archive or remove hides this listing only. FTP results and local downloads stay."
      : job.phase === "uploading_dataset"
        ? "Abort upload stops the FTP transfer and cancels this job. Archive or remove hides this listing only."
        : "Reload probes SSH and checks FTP for finished results. Archive or remove hides this listing only. FTP results and local downloads stay."
    : "This pod was not started by this app. Discard pod terminates it on RunPod.";
  const actions =
    job || podId
      ? `<div class="detail-actions">
        <p class="note">${note}</p>
        ${openResultsBtn}
        ${reloadBtn}
        ${abortBtn}
        ${discardBtn}
        ${archiveBtn}
        ${deleteBtn}
      </div>`
      : "";
  box.innerHTML = `
    <h3>${esc(title)}</h3>
    <div class="kv">${kv
      .map(([k, v, cls, id]) => `<span>${esc(k)}</span><b${cls ? ` class="${cls}"` : ""}${id ? ` data-countdown="${esc(id)}"` : ""}>${esc(v)}</b>`)
      .join("")}</div>
    ${actions}
    <pre class="log">${esc(log)}</pre>
  `;
  box.querySelector("[data-act=archive]")?.addEventListener("click", () => archiveJob(job.id, true));
  box.querySelector("[data-act=unarchive]")?.addEventListener("click", () => archiveJob(job.id, false));
  box.querySelector("[data-act=reload]")?.addEventListener("click", () => reloadJob(job.id));
  box.querySelector("[data-act=delete]")?.addEventListener("click", () => deleteJobListing(job.id));
  box.querySelector("[data-act=discard]")?.addEventListener("click", () => discardPod(podId, Boolean(job)));
  box.querySelector("[data-act=abort-upload]")?.addEventListener("click", () => abortUpload(job.id));
  box.querySelector("[data-act=open-results]")?.addEventListener("click", () => openResultFolder(job.id));
}

async function reloadJob(jobId) {
  state.reloading = jobId;
  renderLists();
  try {
    await api(`/api/jobs/${jobId}/reload`, { method: "POST" });
    applySnapshot(await api("/api/state"));
    state.reloadFlash = jobId;
    setTimeout(() => {
      if (state.reloadFlash === jobId) {
        state.reloadFlash = null;
        renderLists();
      }
    }, 1400);
  } catch (err) {
    alert(err.message);
  } finally {
    state.reloading = null;
    renderLists();
  }
}

async function openResultFolder(jobId) {
  try {
    await api(`/api/jobs/${jobId}/open-results`, { method: "POST" });
  } catch (err) {
    alert(err.message);
  }
}

async function archiveJob(jobId, archived) {
  try {
    await api(`/api/jobs/${jobId}/${archived ? "archive" : "unarchive"}`, { method: "POST" });
    if (state.selected?.kind === "job" && state.selected.id === jobId) state.selected = null;
    applySnapshot(await api("/api/state"));
  } catch (err) {
    alert(err.message);
  }
}

async function deleteJobListing(jobId) {
  const ok = window.confirm(
    "Remove this job from the list? FTP results and any local downloads are not deleted. A running GPU pod is left as-is."
  );
  if (!ok) return;
  try {
    await api(`/api/jobs/${jobId}`, { method: "DELETE" });
    if (state.selected?.kind === "job" && state.selected.id === jobId) state.selected = null;
    applySnapshot(await api("/api/state"));
  } catch (err) {
    alert(err.message);
  }
}

async function abortUpload(jobId) {
  const ok = window.confirm("Abort this FTP upload and cancel the job?");
  if (!ok) return;
  state.aborting = jobId;
  renderLists();
  try {
    await api(`/api/jobs/${jobId}/abort-upload`, { method: "POST" });
    applySnapshot(await api("/api/state"));
  } catch (err) {
    alert(err.message);
  } finally {
    state.aborting = null;
    renderLists();
  }
}

async function discardPod(podId, attachedToJob) {
  const ok = window.confirm(
    attachedToJob
      ? "Terminate this GPU pod on RunPod? The job will stop. FTP results and the job listing stay."
      : "Terminate this GPU pod on RunPod? This app did not start it."
  );
  if (!ok) return;
  state.discarding = podId;
  renderLists();
  try {
    await api(`/api/pods/${encodeURIComponent(podId)}/discard`, { method: "POST" });
    applySnapshot(await api("/api/state"));
  } catch (err) {
    alert(err.message);
  } finally {
    state.discarding = null;
    renderLists();
  }
}

function esc(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.detail || data.message || res.statusText;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

async function loadSettings() {
  const s = await api("/api/settings");
  const form = $("#settings-form");
  form.runpod_api_key.value = s.runpod_api_key || "";
  form.sftp_host.value = s.sftp_host || "";
  form.sftp_user.value = s.sftp_user || "";
  form.sftp_password.value = s.sftp_password || "";
  form.storage_protocol.value = s.storage_protocol || "ftp";
  $("#config-hint").hidden = s.configured;
}

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  $("#settings-error").hidden = true;
  $("#settings-ok").hidden = true;
  const body = {
    runpod_api_key: form.runpod_api_key.value,
    sftp_host: form.sftp_host.value,
    sftp_user: form.sftp_user.value,
    sftp_password: form.sftp_password.value,
    storage_protocol: form.storage_protocol.value,
  };
  try {
    await api("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    $("#settings-ok").hidden = false;
    await loadSettings();
  } catch (err) {
    $("#settings-error").textContent = err.message;
    $("#settings-error").hidden = false;
  }
});

async function loadJobOptions() {
  try {
    const [gpus, builds, datasets, defaults] = await Promise.all([
      api("/api/gpus"),
      api("/api/ftp/builds"),
      api("/api/ftp/datasets"),
      api("/api/defaults"),
    ]);
    state.datasetsDir = defaults.datasets_dir || "";
    const imageInput = $("#job-image");
    if (imageInput && !imageInput.value.trim()) imageInput.value = defaults.image || "";
    fillSelect($("#gpu-select"), gpus.gpus.map((g) => ({
      value: g.id,
      label: `${g.name}${g.memory_gb ? ` (${g.memory_gb} GB)` : ""}${g.availability ? ` · ${g.availability}` : ""}`,
    })), $("#gpu-select").value || "NVIDIA L40S");
    fillSelect($("#build-select"), builds.files.map((f) => ({ value: f, label: f })));
    fillSelect($("#dataset-select"), datasets.files.map((f) => ({ value: f, label: f })));
    browseFs(state.datasetsDir || "");
  } catch (err) {
    $("#job-error").textContent = err.message;
    $("#job-error").hidden = false;
    browseFs(state.datasetsDir || "");
  }
}

function fillSelect(sel, items, preferred) {
  const current = preferred || sel.value;
  sel.innerHTML = "";
  if (!items.length) {
    sel.innerHTML = `<option value="">(none found)</option>`;
    return;
  }
  for (const it of items) {
    const o = document.createElement("option");
    o.value = it.value;
    o.textContent = it.label;
    sel.append(o);
  }
  if (current && [...sel.options].some((o) => o.value === current)) sel.value = current;
}

$$("[name=dataset_source]").forEach((r) => {
  r.addEventListener("change", () => {
    const local = $("[name=dataset_source]:checked").value === "local";
    $("#local-dataset-wrap").hidden = !local;
    $("#ftp-dataset-wrap").hidden = local;
  });
});

const ARCHIVE_RE = /\.(tar\.gz|tgz|tar|zip)$/i;

function isArchivePath(p) {
  return ARCHIVE_RE.test(String(p || ""));
}

function syncUploadAsIs() {
  const wrap = $("#upload-as-is-wrap");
  const box = $("#upload-as-is");
  if (!wrap || !box) return;
  const show = isArchivePath($("#dataset-local")?.value.trim());
  wrap.hidden = !show;
  if (show) box.checked = true;
}

async function pickPath(kind, purpose) {
  try {
    const data = await api("/api/fs/pick", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind,
        purpose: purpose || "",
        start: kind === "dir"
          ? ($("#dataset-local")?.value.trim() || state.datasetsDir)
          : (state.datasetsDir || ""),
      }),
    });
    if (data.cancelled) return null;
    return data.path || null;
  } catch (err) {
    $("#job-error").textContent = `${err.message} Use the folder list below.`;
    $("#job-error").hidden = false;
    return null;
  }
}

$("#pick-folder")?.addEventListener("click", async () => {
  const path = await pickPath("dir");
  if (!path) return;
  $("#dataset-local").value = path;
  syncUploadAsIs();
  browseFs(path);
});

$("#pick-archive")?.addEventListener("click", async () => {
  const path = await pickPath("file", "archive");
  if (!path) return;
  $("#dataset-local").value = path;
  syncUploadAsIs();
});

$("#pick-config")?.addEventListener("click", async () => {
  const path = await pickPath("file", "json");
  if (!path) return;
  $("#config-local").value = path;
});

$("#dataset-local")?.addEventListener("input", syncUploadAsIs);

$("#override-lichtfeld")?.addEventListener("change", () => {
  const on = $("#override-lichtfeld").checked;
  $("#override-fields").hidden = !on;
});

async function browseFs(path) {
  const box = $("#fs-browser");
  if (!box) return;
  try {
    const data = await api(`/api/fs?path=${encodeURIComponent(path)}`);
    box.innerHTML = "";
    const use = document.createElement("button");
    use.type = "button";
    use.textContent = "Use this folder";
    use.addEventListener("click", () => {
      $("#dataset-local").value = data.path;
      syncUploadAsIs();
      if (data.is_scene) applySceneHint(data);
    });
    box.append(use);
    if (data.parent && data.parent !== data.path) {
      const up = document.createElement("button");
      up.type = "button";
      up.textContent = `↑ ${data.parent}`;
      up.addEventListener("click", () => browseFs(data.parent));
      box.append(up);
    }
    for (const ent of data.entries) {
      if (!ent.is_dir && !ent.is_archive && !ent.is_json) continue;
      const b = document.createElement("button");
      b.type = "button";
      if (ent.is_dir) {
        b.textContent = `▸ ${ent.name}`;
        b.addEventListener("click", () => {
          $("#dataset-local").value = ent.path;
          syncUploadAsIs();
          browseFs(ent.path);
        });
      } else if (ent.is_json) {
        b.className = "file";
        b.textContent = `JSON ${ent.name}`;
        b.addEventListener("click", () => {
          $("#config-local").value = ent.path;
        });
      } else {
        b.className = "file";
        b.textContent = `▣ ${ent.name}`;
        b.addEventListener("click", () => {
          $("#dataset-local").value = ent.path;
          syncUploadAsIs();
        });
      }
      box.append(b);
    }
    if (data.is_scene) applySceneHint(data);
  } catch (err) {
    box.textContent = err.message;
  }
}

function applySceneHint(data) {
  $("#dataset-local").value = data.scene_root || data.path;
  if (data.configs?.length) {
    $("#config-rel").value = data.configs[0];
    $("#config-note").textContent = `Detected config: ${data.configs.join(", ")}`;
  } else {
    $("#config-note").textContent = "Scene found, but no JSON config. Pick one if you have it.";
  }
}

async function submitJob() {
  const form = $("#job-form");
  $("#job-error").hidden = true;
  const source = form.dataset_source.value;
  const override = form.override_lichtfeld.checked;
  const body = {
    name: form.name.value,
    gpu: form.gpu.value,
    cloud: form.cloud.value,
    image: form.image.value,
    build_archive: form.build_archive.value,
    dataset_source: source,
    dataset_archive: source === "ftp" ? form.dataset_archive.value : "",
    dataset_local: source === "local" ? form.dataset_local.value : "",
    upload_as_is: source === "local" && form.upload_as_is.checked,
    result_dir: form.result_dir.value,
    config: form.config.value,
    config_local: form.config_local.value,
    auto_download: form.auto_download.checked,
    terminate_when_done: form.terminate_when_done.checked,
    max_cap: override && form.max_cap.value ? Number(form.max_cap.value) : null,
    enable_sparsity: override ? form.enable_sparsity.checked : null,
    gut: override ? form.gut.checked : null,
  };
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    fillDefaultJobName(true);
    select({ kind: "job", id: job.id });
  } catch (err) {
    $("#job-error").textContent = err.message;
    $("#job-error").hidden = false;
  }
}

$("#job-form").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.target.tagName === "TEXTAREA") return;
  if (e.target.id === "job-start") return;
  e.preventDefault();
});

$("#job-form").addEventListener("submit", (e) => {
  e.preventDefault();
});

$("#job-start")?.addEventListener("click", () => submitJob());

const buildAuto = { cuda: true, result: true, archive: true };

async function refreshBuildPaths() {
  const gpu = $("#build-gpu")?.value || "NVIDIA L40S";
  const gitRef = $("#build-git-ref")?.value.trim() || "";
  const params = new URLSearchParams({ gpu, git_ref: gitRef });
  if (!buildAuto.cuda) {
    const arch = $("#build-cuda-arch")?.value.trim();
    if (arch) params.set("cuda_arch", arch);
  }
  try {
    const data = await api(`/api/build-defaults?${params}`);
    if (buildAuto.cuda && data.cuda_arch) $("#build-cuda-arch").value = data.cuda_arch;
    if (buildAuto.result && data.result_dir) $("#build-result-dir").value = data.result_dir;
    if (buildAuto.archive && data.archive_name) $("#build-archive-name").value = data.archive_name;
    const repo = $("#build-repo");
    if (repo && !repo.value.trim() && data.repo_url) repo.value = data.repo_url;
    const image = $("#build-image");
    if (image && !image.value.trim() && data.image) image.value = data.image;
  } catch (err) {
    const box = $("#build-error");
    if (box) {
      box.textContent = err.message;
      box.hidden = false;
    }
  }
}

async function loadBuildOptions() {
  $("#build-error").hidden = true;
  try {
    const [gpus, defaults] = await Promise.all([api("/api/gpus"), api("/api/defaults")]);
    const imageInput = $("#build-image");
    if (imageInput && !imageInput.value.trim()) imageInput.value = defaults.image || "";
    const repo = $("#build-repo");
    if (repo && !repo.value.trim() && defaults.repo_url) repo.value = defaults.repo_url;
    const gitRef = $("#build-git-ref");
    if (gitRef && !gitRef.value.trim() && defaults.git_ref) gitRef.value = defaults.git_ref;
    fillSelect(
      $("#build-gpu"),
      gpus.gpus.map((g) => ({
        value: g.id,
        label: `${g.name}${g.memory_gb ? ` (${g.memory_gb} GB)` : ""}${g.availability ? ` · ${g.availability}` : ""}`,
      })),
      $("#build-gpu").value || "NVIDIA L40S"
    );
    await refreshBuildPaths();
  } catch (err) {
    $("#build-error").textContent = err.message;
    $("#build-error").hidden = false;
  }
}

async function submitBuild() {
  const form = $("#build-form");
  $("#build-error").hidden = true;
  const body = {
    kind: "build",
    name: form.name.value,
    gpu: form.gpu.value,
    cloud: form.cloud.value,
    image: form.image.value,
    git_ref: form.git_ref.value,
    cuda_arch: form.cuda_arch.value,
    repo_url: form.repo_url.value,
    result_dir: form.result_dir.value,
    archive_name: form.archive_name.value,
    auto_download: form.auto_download.checked,
    terminate_when_done: form.terminate_when_done.checked,
  };
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    fillDefaultBuildName(true);
    buildAuto.cuda = true;
    buildAuto.result = true;
    buildAuto.archive = true;
    select({ kind: "job", id: job.id });
  } catch (err) {
    $("#build-error").textContent = err.message;
    $("#build-error").hidden = false;
  }
}

$("#build-gpu")?.addEventListener("change", () => {
  buildAuto.cuda = true;
  refreshBuildPaths();
});
$("#build-git-ref")?.addEventListener("input", () => refreshBuildPaths());
$("#build-cuda-arch")?.addEventListener("input", () => {
  buildAuto.cuda = false;
  refreshBuildPaths();
});
$("#build-result-dir")?.addEventListener("input", () => {
  buildAuto.result = false;
});
$("#build-archive-name")?.addEventListener("input", () => {
  buildAuto.archive = false;
});

$("#build-form")?.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.target.tagName === "TEXTAREA") return;
  if (e.target.id === "build-start") return;
  e.preventDefault();
});
$("#build-form")?.addEventListener("submit", (e) => e.preventDefault());
$("#build-start")?.addEventListener("click", () => submitBuild());

function applySnapshot(data) {
  state.data = data;
  renderLists();
}

async function boot() {
  fillDefaultJobName();
  try {
    applySnapshot(await api("/api/state"));
    const s = await api("/api/settings");
    $("#config-hint").hidden = s.configured;
  } catch (err) {
    $("#config-hint").hidden = false;
    $("#config-hint").textContent = err.message;
  }
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => {
    try {
      applySnapshot(JSON.parse(ev.data));
    } catch {
      /* ignore */
    }
  };
  setInterval(tickCountdowns, 1000);
}

boot();
