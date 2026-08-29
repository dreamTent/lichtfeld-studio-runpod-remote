const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const state = {
  view: "dash",
  selected: null, // { kind: 'job'|'pod', id }
  data: { jobs: [], pods: [] },
};

function showView(name) {
  state.view = name;
  $$("nav [data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  ["dash", "new", "settings"].forEach((v) => {
    const node = $(`#view-${v}`);
    if (node) node.hidden = v !== name;
  });
  if (name === "new") {
    fillDefaultJobName();
    loadJobOptions();
  }
  if (name === "settings") loadSettings();
}

$$("nav [data-view]").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.disabled) return;
    showView(btn.dataset.view);
  });
});

function defaultJobName() {
  const letters = Array.from({ length: 6 }, () =>
    String.fromCharCode(97 + Math.floor(Math.random() * 26))
  ).join("");
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  return `${letters}-${stamp}`;
}

function fillDefaultJobName(force = false) {
  const input = $("#job-name");
  if (!input) return;
  if (force || !input.value.trim()) input.value = defaultJobName();
}

function orb(color) {
  const span = document.createElement("span");
  span.className = `orb ${color || "white"}`;
  span.title = color || "white";
  return span;
}

function mergedRows() {
  const jobs = state.data.jobs || [];
  const pods = state.data.pods || [];
  const claimed = new Set(jobs.map((j) => j.pod_id).filter(Boolean));
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

function rowSubtitle(row) {
  const job = row.job;
  const pod = row.pod;
  if (job) {
    const bits = [job.message || job.phase];
    if (job.gpu) bits.push(job.gpu);
    return bits.join(" · ");
  }
  return [pod.status, pod.gpu, "foreign"].filter(Boolean).join(" · ");
}

function renderLists() {
  const list = $("#item-list");
  list.innerHTML = "";
  const rows = mergedRows();
  if (!rows.length) {
    list.innerHTML = `<li class="empty">No jobs yet.</li>`;
  }
  for (const row of rows) {
    const li = document.createElement("li");
    if (state.selected?.kind === row.kind && state.selected.id === row.id) li.classList.add("selected");
    li.append(orb(row.color));
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.innerHTML = `<strong>${esc(row.name)}</strong><span>${esc(rowSubtitle(row))}</span>`;
    li.append(meta);
    li.addEventListener("click", () => select({ kind: row.kind, id: row.id }));
    list.append(li);
  }
  if (state.selected) renderDetail();
}

function select(sel) {
  state.selected = sel;
  showView("dash");
  renderLists();
  renderDetail();
}

function renderDetail() {
  const box = $("#detail");
  if (!state.selected) {
    box.innerHTML = `<p class="empty">Select a job.</p>`;
    return;
  }
  const row = mergedRows().find((r) => r.kind === state.selected.kind && r.id === state.selected.id);
  if (!row) {
    box.innerHTML = `<p class="empty">Gone.</p>`;
    return;
  }
  const job = row.job;
  const pod = row.pod;
  const title = job?.name || pod?.name || "—";
  const kv = [];
  if (job) {
    kv.push(["Status", job.message || job.phase || "—"]);
    kv.push(["Stage", job.stage || "—"]);
    kv.push(["GPU", `${job.gpu || "—"} / ${job.cloud || "—"}`]);
    kv.push(["Pod", job.pod_id || pod?.id || "—"]);
    kv.push(["SSH", pod?.ssh || "—"]);
    kv.push(["Build", job.build_archive || "—"]);
    kv.push(["Dataset", job.dataset_archive || job.dataset_local || "—"]);
    kv.push(["Results", job.result_dir || "—"]);
    kv.push(["Download", job.auto_download ? "local + FTP" : "FTP only"]);
  } else {
    kv.push(["Pod", pod.id]);
    kv.push(["Status", `${pod.status || ""} · ${pod.color}`]);
    kv.push(["GPU", pod.gpu || "—"]);
    kv.push(["SSH", pod.ssh || "—"]);
    kv.push(["Control", "foreign (not started by this app)"]);
  }
  const log = job?.log_tail || job?.message || "";
  box.innerHTML = `
    <h3>${esc(title)}</h3>
    <div class="kv">${kv.map(([k, v]) => `<span>${esc(k)}</span><b>${esc(v)}</b>`).join("")}</div>
    <pre class="log">${esc(log)}</pre>
  `;
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
    const [gpus, builds, datasets] = await Promise.all([
      api("/api/gpus"),
      api("/api/ftp/builds"),
      api("/api/ftp/datasets"),
    ]);
    fillSelect($("#gpu-select"), gpus.gpus.map((g) => ({
      value: g.id,
      label: `${g.name}${g.memory_gb ? ` (${g.memory_gb} GB)` : ""}${g.availability ? ` · ${g.availability}` : ""}`,
    })), $("#gpu-select").value || "NVIDIA L40S");
    fillSelect($("#build-select"), builds.files.map((f) => ({ value: f, label: f })));
    fillSelect($("#dataset-select"), datasets.files.map((f) => ({ value: f, label: f })));
  } catch (err) {
    $("#job-error").textContent = err.message;
    $("#job-error").hidden = false;
  }
  browseFs("");
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

async function browseFs(path) {
  const box = $("#fs-browser");
  try {
    const data = await api(`/api/fs?path=${encodeURIComponent(path)}`);
    box.innerHTML = "";
    const up = document.createElement("button");
    up.type = "button";
    up.textContent = `↑ ${data.parent}`;
    up.addEventListener("click", () => browseFs(data.parent));
    box.append(up);
    for (const ent of data.entries) {
      if (!ent.is_dir) continue;
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = `▸ ${ent.name}`;
      b.addEventListener("click", () => {
        $("#dataset-local").value = ent.path;
        browseFs(ent.path);
      });
      box.append(b);
    }
    if (data.is_scene) {
      $("#dataset-local").value = data.scene_root || data.path;
      if (data.configs?.length) {
        $("#config-rel").value = data.configs[0];
        $("#config-note").textContent = `Detected config: ${data.configs.join(", ")}`;
      } else {
        $("#config-note").textContent = "Scene found, but no JSON config. Pick one if you have it.";
      }
    }
  } catch (err) {
    box.textContent = err.message;
  }
}

$("#job-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  $("#job-error").hidden = true;
  const source = form.dataset_source.value;
  const body = {
    name: form.name.value,
    gpu: form.gpu.value,
    cloud: form.cloud.value,
    build_archive: form.build_archive.value,
    dataset_source: source,
    dataset_archive: source === "ftp" ? form.dataset_archive.value : "",
    dataset_local: source === "local" ? form.dataset_local.value : "",
    result_dir: form.result_dir.value,
    config: form.config.value,
    auto_download: form.auto_download.checked,
    terminate_when_done: form.terminate_when_done.checked,
    max_cap: form.max_cap.value ? Number(form.max_cap.value) : null,
    enable_sparsity: form.enable_sparsity.checked,
    gut: form.gut.checked,
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
});

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
}

boot();
