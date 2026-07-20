// Blueprint Marketplace — client logic (vanilla JS, no build step).

const app = document.getElementById("app");
const state = {
  view: "catalog",
  settings: null,
  contexts: [],
  catalog: [],
  bp: null,        // selected blueprint
  step: 0,         // guide step index
  namespace: "",
  done: {},        // step index -> true
};

// --- helpers -------------------------------------------------------------- //
const esc = (s) => (s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}
async function putJSON(url, body) {
  const r = await fetch(url, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}

// streamPost reads an SSE stream from a POST response.
async function streamPost(url, body, { onLog, onDone, onError }) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  if (!r.ok || !r.body) { onError && onError(`${r.status} ${r.statusText}`); return; }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const chunks = buf.split("\n\n");
    buf = chunks.pop();
    for (const chunk of chunks) {
      let event = "message";
      const data = [];
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
      }
      const payload = data.join("\n");
      if (event === "log") onLog && onLog(payload);
      else if (event === "done") onDone && onDone(payload);
      else if (event === "error") onError && onError(payload);
    }
  }
}

// Tiny markdown-ish renderer for guide bodies (safe: escapes first).
function md(text) {
  let h = esc(text);
  h = h.replace(/`([^`]+)`/g, "<code>$1</code>");
  h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  const lines = h.split("\n").map((l) => {
    if (l.trim().startsWith("&gt; ")) return `<blockquote class="muted">${l.trim().slice(5)}</blockquote>`;
    return l;
  });
  return lines.join("<br>");
}

// --- data loading --------------------------------------------------------- //
async function loadContexts() {
  try { state.contexts = await getJSON("/api/contexts"); } catch { state.contexts = []; }
}
async function loadSettings() { state.settings = await getJSON("/api/settings"); }
async function loadCatalog() { try { state.catalog = await getJSON("/api/catalog"); } catch { state.catalog = []; } }

function activeContext() {
  const cfg = state.settings?.targetContext;
  if (cfg) return state.contexts.find((c) => c.name === cfg) || { name: cfg, ready: false };
  return state.contexts.find((c) => c.current) || null;
}

function renderClusterChip() {
  const dot = document.getElementById("cluster-dot");
  const name = document.getElementById("cluster-name");
  const c = activeContext();
  if (!c) { dot.className = "dot"; name.textContent = "no cluster"; return; }
  name.textContent = c.name;
  dot.className = "dot " + (c.ready ? "ok" : "bad");
  document.getElementById("cluster-chip").title = c.ready ? "SUSE AI Factory detected" : "SUSE AI Factory CRDs not found";
}

// --- views ---------------------------------------------------------------- //
function setView(v) {
  state.view = v;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  render();
}

function render() {
  renderClusterChip();
  if (state.view === "settings") return renderSettings();
  if (state.view === "running") return renderRunning();
  if (state.view === "detail") return renderDetail();
  if (state.view === "guide") return renderGuide();
  return renderCatalog();
}

// --- running frontends overview ------------------------------------------ //
function bpName(id) {
  const bp = state.catalog.find((b) => b.id === id);
  return bp ? bp.displayName : id;
}

async function renderRunning() {
  app.innerHTML = `<h2>Running frontends</h2>
    <p class="muted">Local demo UIs started from the guided demos (uvicorn + kubectl port-forwards).</p>
    <div id="running-list"><span class="spinner"></span>Loading…</div>`;
  await refreshRunningList();
}

async function refreshRunningList() {
  const host = document.getElementById("running-list");
  if (!host) return;
  let procs = [];
  try { procs = await getJSON("/api/processes"); }
  catch (e) { host.innerHTML = `<div class="error">${esc(String(e))}</div>`; return; }
  if (!procs.length) { host.innerHTML = `<p class="muted">No frontends are running.</p>`; return; }
  host.innerHTML = `
    <div class="row" style="justify-content:flex-end;margin-bottom:10px"><button class="danger" id="stop-all" style="max-width:120px">Stop all</button></div>
    <div class="card">${procs.map((p) => `
      <div class="check">
        <span class="badge ok">running</span>
        <span><strong>${esc(bpName(p.blueprint))}</strong></span>
        <span class="muted">${esc(p.namespace || "")}</span>
        ${p.url ? `<a href="${esc(p.url)}" target="_blank" style="margin-left:auto">${esc(p.url)}</a>` : `<span style="margin-left:auto"></span>`}
        <button class="danger" data-stop="${esc(p.blueprint)}" style="max-width:90px">Stop</button>
      </div>`).join("")}</div>`;
  host.querySelectorAll("button[data-stop]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true; b.textContent = "…";
    await fetch(`/api/blueprints/${b.dataset.stop}/frontend/stop`, { method: "POST" });
    await refreshRunningList(); updateRunningIndicator();
  }));
  document.getElementById("stop-all")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    for (const p of procs) await fetch(`/api/blueprints/${p.blueprint}/frontend/stop`, { method: "POST" });
    await refreshRunningList(); updateRunningIndicator();
  });
}

// Poll the process list to keep the header indicator + tab count fresh.
async function updateRunningIndicator() {
  let procs = [];
  try { procs = await getJSON("/api/processes"); } catch { return; }
  const n = procs.length;
  const badge = document.getElementById("running-badge");
  if (badge) {
    badge.hidden = n === 0;
    badge.textContent = n ? `▶ ${n} running` : "";
  }
  const tab = document.querySelector('#tabs button[data-view="running"]');
  if (tab) tab.textContent = n ? `Running (${n})` : "Running";
  if (state.view === "running") refreshRunningList();
}

function renderCatalog() {
  if (!state.catalog.length) {
    app.innerHTML = `<h2>Catalog</h2><p class="muted">No blueprints loaded. Check the git repo in <a href="#" id="to-settings">Settings</a>, or start the binary with <code>--dir ../blueprints</code>.</p>`;
    document.getElementById("to-settings")?.addEventListener("click", (e) => { e.preventDefault(); setView("settings"); });
    return;
  }
  app.innerHTML = `<h2>Blueprints</h2><p class="muted">Select a blueprint to import it and run a guided demo.</p>
    <div class="grid">${state.catalog.map(cardHTML).join("")}</div>`;
  state.catalog.forEach((bp) => {
    document.getElementById(`bp-${bp.id}`)?.addEventListener("click", () => openBlueprint(bp.id));
  });
}

function cardHTML(bp) {
  const tags = (bp.tags || []).map((t) => `<span class="badge neutral">${esc(t)}</span>`).join("");
  return `<div class="card clickable" id="bp-${esc(bp.id)}">
    <h3>${esc(bp.displayName)}</h3>
    <p>${esc(bp.description)}</p>
    <div class="tags"><span class="badge">${esc(bp.category || "blueprint")}</span>${tags}</div>
  </div>`;
}

async function openBlueprint(id) {
  state.bp = state.catalog.find((b) => b.id === id);
  state.step = 0; state.done = {}; state.namespace = "";
  setView("detail");
}

async function renderDetail() {
  const bp = state.bp;
  app.innerHTML = `
    <button class="link back" id="back">← Catalog</button>
    <h2>${esc(bp.displayName)}</h2>
    <p class="muted">${esc(bp.description)}</p>
    <div class="card section">
      <h3>Prerequisites <span class="muted" id="prereq-ctx"></span></h3>
      <div id="prereqs"><span class="spinner"></span>Checking…</div>
    </div>
    <div class="section row">
      <button class="primary" id="start-guide">Start guided demo →</button>
    </div>`;
  document.getElementById("back").addEventListener("click", () => setView("catalog"));
  document.getElementById("start-guide").addEventListener("click", () => { state.step = 0; setView("guide"); });
  try {
    const res = await getJSON(`/api/blueprints/${bp.id}/prereqs`);
    document.getElementById("prereq-ctx").textContent = res.context ? `on ${res.context}` : "";
    document.getElementById("prereqs").innerHTML = res.results.map((r) =>
      `<div class="check"><span class="status ${r.ok ? "ok" : "bad"}">${r.ok ? "✓" : "✗"}</span>
        <span>${esc(r.label)}</span><span class="muted" style="margin-left:auto">${esc(r.message)}</span></div>`).join("");
  } catch (e) {
    document.getElementById("prereqs").innerHTML = `<div class="error">${esc(String(e))}</div>`;
  }
}

function renderGuide() {
  const bp = state.bp;
  const steps = bp.guide || [];
  const step = steps[state.step];
  const stepper = steps.map((s, i) =>
    `<span class="step ${i === state.step ? "active" : ""} ${state.done[i] ? "done" : ""}">${i + 1}. ${esc(s.title)}</span>`).join("");

  app.innerHTML = `
    <button class="link back" id="back">← ${esc(bp.displayName)}</button>
    <div class="stepper">${stepper}</div>
    <div class="card">
      <h2>${esc(step.title)}</h2>
      <div class="step-body">${md(step.body)}</div>
      <div id="action"></div>
      <pre class="log" id="log" hidden></pre>
    </div>
    <div id="processes" class="section"></div>
    <div class="step-nav">
      <div class="row">
        <button id="prev" ${state.step === 0 ? "disabled" : ""}>← Back</button>
        <button id="cancel">Cancel</button>
      </div>
      <button class="primary" id="next" ${state.step >= steps.length - 1 ? "disabled" : ""}>Next →</button>
    </div>`;
  document.getElementById("back").addEventListener("click", () => setView("detail"));
  document.getElementById("cancel").addEventListener("click", () => setView("catalog"));
  document.getElementById("prev").addEventListener("click", () => { if (state.step > 0) { state.step--; render(); } });
  document.getElementById("next").addEventListener("click", () => { if (state.step < steps.length - 1) { state.step++; render(); } });
  renderAction(step);
  refreshProcesses();
}

function logLine(s) {
  const el = document.getElementById("log");
  el.hidden = false;
  el.textContent += s + "\n";
  el.scrollTop = el.scrollHeight;
}

function renderAction(step) {
  const host = document.getElementById("action");
  const a = step.action;
  if (!a) { host.innerHTML = ""; return; }
  const bp = state.bp;

  if (a.type === "import") {
    host.innerHTML = `<div class="section"><button class="primary" id="do">Import blueprint (kubectl apply)</button><div class="error" id="err"></div></div>`;
    document.getElementById("do").addEventListener("click", (e) => {
      e.target.disabled = true; e.target.innerHTML = `<span class="spinner"></span>Importing…`;
      streamPost(`/api/blueprints/${bp.id}/import`, {}, {
        onLog: logLine,
        onDone: (m) => { logLine("✓ " + m); e.target.disabled = false; e.target.textContent = "Re-import"; markDone(); },
        onError: (m) => { document.getElementById("err").textContent = m; e.target.disabled = false; e.target.textContent = "Retry import"; },
      });
    });

  } else if (a.type === "namespace-input") {
    host.innerHTML = `<div class="section"><label>AIWorkload namespace</label>
      <div class="row"><input id="ns" placeholder="e.g. airflow-genai-rag-minimal-system" value="${esc(state.namespace)}" />
      <button id="save-ns" style="max-width:120px">Save</button></div>
      <div class="muted" id="ns-msg"></div></div>`;
    document.getElementById("save-ns").addEventListener("click", () => {
      state.namespace = document.getElementById("ns").value.trim();
      document.getElementById("ns-msg").textContent = state.namespace ? `Using namespace "${state.namespace}"` : "Enter a namespace";
      if (state.namespace) markDone();
    });

  } else if (a.type === "start-frontend") {
    host.innerHTML = `<div class="section"><button class="primary" id="do">Start local frontend</button>
      <button class="danger" id="stop" style="margin-left:8px">Stop</button>
      <span id="link" style="margin-left:10px"></span><div class="error" id="err"></div></div>`;
    document.getElementById("do").addEventListener("click", (e) => {
      if (!state.namespace) { document.getElementById("err").textContent = "Set the AIWorkload namespace in the previous step first."; return; }
      e.target.disabled = true; e.target.innerHTML = `<span class="spinner"></span>Starting…`;
      streamPost(`/api/blueprints/${bp.id}/frontend/start`, { namespace: state.namespace }, {
        onLog: logLine,
        onDone: (url) => {
          logLine("✓ ready: " + url);
          document.getElementById("link").innerHTML = `<a href="${esc(url)}" target="_blank">Open ${esc(url)}</a>`;
          e.target.disabled = false; e.target.textContent = "Restart"; markDone(); refreshProcesses();
        },
        onError: (m) => { document.getElementById("err").textContent = m; e.target.disabled = false; e.target.textContent = "Retry"; },
      });
    });
    document.getElementById("stop").addEventListener("click", async () => {
      await fetch(`/api/blueprints/${bp.id}/frontend/stop`, { method: "POST" });
      logLine("stopped frontend"); refreshProcesses();
    });

  } else if (a.type === "open-url") {
    host.innerHTML = `<div class="section"><a href="${esc(a.url)}" target="_blank"><button class="primary">Open ${esc(a.url)}</button></a></div>`;
  }
}

function markDone() { state.done[state.step] = true; document.querySelectorAll(".stepper .step")[state.step]?.classList.add("done"); }

async function refreshProcesses() {
  const host = document.getElementById("processes");
  if (!host) return;
  let procs = [];
  try { procs = await getJSON("/api/processes"); } catch { return; }
  // Only show the process(es) for the blueprint currently being viewed.
  if (state.bp) procs = procs.filter((p) => p.blueprint === state.bp.id);
  if (!procs.length) { host.innerHTML = ""; return; }
  host.innerHTML = `<div class="card"><h3>Running frontend</h3>${procs.map((p) =>
    `<div class="check"><span class="badge ok">running</span><span>${esc(p.blueprint)}</span>
      <span class="muted">${esc(p.namespace || "")}</span>
      ${p.url ? `<a href="${esc(p.url)}" target="_blank" style="margin-left:auto">${esc(p.url)}</a>` : `<span style="margin-left:auto"></span>`}
      <button class="danger" data-stop="${esc(p.blueprint)}" style="max-width:90px">Stop</button></div>`).join("")}</div>`;
  host.querySelectorAll("button[data-stop]").forEach((b) =>
    b.addEventListener("click", async () => { await fetch(`/api/blueprints/${b.dataset.stop}/frontend/stop`, { method: "POST" }); refreshProcesses(); }));
}

function renderSettings() {
  const s = state.settings || {};
  const opts = state.contexts.map((c) =>
    `<option value="${esc(c.name)}" ${c.name === s.targetContext ? "selected" : ""}>${esc(c.name)}${c.ready ? " ✓ AI Factory" : ""}</option>`).join("");
  app.innerHTML = `
    <h2>Settings</h2>
    <div class="card" style="max-width:640px">
      <label>Blueprints git repo (URL)</label>
      <input id="repo" value="${esc(s.blueprintsRepo || "")}" ${s.gitManaged ? "" : "disabled"} />
      <label>Git ref / branch</label>
      <input id="ref" value="${esc(s.blueprintsRef || "")}" ${s.gitManaged ? "" : "disabled"} />
      ${s.gitManaged ? "" : `<div class="muted">Running with <code>--dir</code>; git settings are disabled.</div>`}
      <label>Target cluster (kube context)</label>
      <select id="ctx"><option value="">— host current-context —</option>${opts}</select>
      <div class="section row"><button class="primary" id="save">Save</button><span id="msg" class="muted"></span></div>
      <div class="error" id="err"></div>
    </div>`;
  document.getElementById("save").addEventListener("click", async () => {
    const btn = document.getElementById("save");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>Saving…`;
    try {
      state.settings = await putJSON("/api/settings", {
        blueprintsRepo: document.getElementById("repo").value.trim(),
        blueprintsRef: document.getElementById("ref").value.trim(),
        targetContext: document.getElementById("ctx").value,
      });
      await loadCatalog();
      document.getElementById("msg").textContent = "Saved.";
      renderClusterChip();
    } catch (e) {
      document.getElementById("err").textContent = String(e);
    } finally { btn.disabled = false; btn.textContent = "Save"; }
  });
}

// --- boot ----------------------------------------------------------------- //
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
document.getElementById("running-badge").addEventListener("click", () => setView("running"));

const themeBtn = document.getElementById("theme-toggle");
function applyTheme(t) { document.documentElement.dataset.theme = t; themeBtn.textContent = t === "dark" ? "☾" : "☀"; localStorage.setItem("bpm-theme", t); }
themeBtn.addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
applyTheme(localStorage.getItem("bpm-theme") || "dark");

(async function init() {
  await Promise.all([loadSettings(), loadContexts()]);
  await loadCatalog();
  render();
  updateRunningIndicator();
  setInterval(updateRunningIndicator, 4000);
})();
