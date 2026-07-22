// Blueprint Marketplace — client logic (vanilla JS, no build step).

const app = document.getElementById("app");
const state = {
  view: "catalog",
  settings: null,
  contexts: [],
  catalog: [],
  catalogQuery: "", // catalog search box text
  catalogTopic: "", // catalog topic filter ("" = all)
  bp: null,        // selected blueprint
  step: 0,         // guide step index
  namespace: "",
  done: {},        // step index -> true
  bpContext: "",   // target kube context for the open blueprint's guided demo
  bpModel: "",     // chosen model-size id for the open blueprint (if it has modelSizes)
  selectMode: false,       // catalog multi-select mode
  selected: new Set(),     // ids ticked for bulk import
  rancherClusters: [],     // downstream clusters from the last Rancher connect
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
async function postJSON(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
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
// Supports # / ## / ### headings, **bold**, `code`, > blockquotes, ordered
// lists (1. / 2.), unordered lists (- / *), and ``` fenced code blocks — the
// latter render as a boxed, copy-to-clipboard snippet (see the .copy-btn handler
// at boot). Keeps guide steps readable and prompts easy to grab.
function md(text) {
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  const lines = String(text).split("\n");
  const out = [];
  let list = null; // "ol" | "ul" | null
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  let fence = false, fenceBuf = [];
  const flushFence = () => {
    out.push(
      '<div class="codeblock"><button type="button" class="copy-btn" aria-label="Copy to clipboard">Copy</button>' +
      `<pre><code>${esc(fenceBuf.join("\n"))}</code></pre></div>`
    );
    fenceBuf = []; fence = false;
  };
  for (const raw of lines) {
    const l = raw.trim();
    // ``` fenced code block -> a boxed, copyable snippet.
    if (fence) {
      if (l.startsWith("```")) flushFence();
      else fenceBuf.push(raw);
      continue;
    }
    if (l.startsWith("```")) { closeList(); fence = true; fenceBuf = []; continue; }
    const h = l.match(/^(#{1,3})\s+(.*)$/);
    const ol = l.match(/^\d+\.\s+(.*)$/);
    const ul = l.match(/^[-*]\s+(.*)$/);
    if (h) {
      closeList();
      out.push(`<h4 class="guide-h">${inline(h[2])}</h4>`);
    } else if (ol) {
      if (list !== "ol") { closeList(); out.push('<ol class="guide-list">'); list = "ol"; }
      out.push(`<li>${inline(ol[1])}</li>`);
    } else if (ul) {
      if (list !== "ul") { closeList(); out.push('<ul class="guide-list">'); list = "ul"; }
      out.push(`<li>${inline(ul[1])}</li>`);
    } else if (l.startsWith("> ")) {
      closeList();
      out.push(`<blockquote class="muted">${inline(l.slice(2))}</blockquote>`);
    } else if (l === "") {
      closeList();
      out.push("<br>");
    } else {
      closeList();
      out.push(`<div>${inline(l)}</div>`);
    }
  }
  closeList();
  if (fence) flushFence(); // tolerate an unterminated fence
  return out.join("");
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
  if (state.view === "bulk") return renderBulk();
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

async function stopProc(p) {
  if (p.kind === "component-ui") {
    await fetch(`/api/blueprints/${p.blueprint}/component-ui/stop`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: p.name }),
    });
  } else {
    await fetch(`/api/blueprints/${p.blueprint}/frontend/stop`, { method: "POST" });
  }
}

function procRowHTML(p, i) {
  return `<div class="check">
    <span class="badge ok">${esc(p.kind === "component-ui" ? "port-forward" : "running")}</span>
    <span><strong>${esc(bpName(p.blueprint))}</strong> <span class="muted">· ${esc(p.name)}</span></span>
    <span class="muted">${esc(p.namespace || "")}</span>
    ${p.url ? `<a href="${esc(p.url)}" target="_blank" style="margin-left:auto">${esc(p.url)}</a>` : `<span style="margin-left:auto"></span>`}
    <button class="danger" data-stop="${i}" style="max-width:90px">Stop</button>
  </div>`;
}

async function refreshRunningList() {
  const host = document.getElementById("running-list");
  if (!host) return;
  let procs = [];
  try { procs = await getJSON("/api/processes"); }
  catch (e) { host.innerHTML = `<div class="error">${esc(String(e))}</div>`; return; }
  if (!procs.length) { host.innerHTML = `<p class="muted">Nothing is running.</p>`; return; }
  host.innerHTML = `
    <div class="row" style="justify-content:flex-end;margin-bottom:10px"><button class="danger" id="stop-all" style="max-width:120px">Stop all</button></div>
    <div class="card">${procs.map((p, i) => procRowHTML(p, i)).join("")}</div>`;
  host.querySelectorAll("button[data-stop]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true; b.textContent = "…";
    await stopProc(procs[+b.dataset.stop]);
    await refreshRunningList(); updateRunningIndicator();
  }));
  document.getElementById("stop-all")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    for (const p of procs) await stopProc(p);
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

// topicLabel prettifies a category slug for a section header (e.g.
// "anomaly-detection" -> "Anomaly detection").
function topicLabel(slug) {
  const s = String(slug || "other").replace(/[-_]+/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "Other";
}

function renderCatalog() {
  if (!state.catalog.length) {
    app.innerHTML = `<h2>Catalog</h2><p class="muted">No blueprints loaded. Check the git repo in <a href="#" id="to-settings">Settings</a>, or start the binary with <code>--dir ../blueprints</code>.</p>`;
    document.getElementById("to-settings")?.addEventListener("click", (e) => { e.preventDefault(); setView("settings"); });
    return;
  }
  // Distinct topics (categories) for the dropdown filter.
  const topics = [...new Set(state.catalog.map((bp) => bp.category || "other"))].sort();
  const topicOptions = ['<option value="">All topics</option>']
    .concat(topics.map((t) =>
      `<option value="${esc(t)}" ${t === state.catalogTopic ? "selected" : ""}>${esc(topicLabel(t))}</option>`))
    .join("");

  const selN = state.selected.size;
  app.innerHTML = `<h2>Blueprints</h2>
    <p class="muted">Select a blueprint to import it and run a guided demo.</p>
    <div class="catalog-filters">
      <input id="catalog-search" type="search" autocomplete="off"
        placeholder="Search by name, tag, or topic…" value="${esc(state.catalogQuery || "")}" />
      <select id="catalog-topic">${topicOptions}</select>
      <button id="select-toggle" class="${state.selectMode ? "primary" : ""}">${state.selectMode ? "Done" : "Select"}</button>
    </div>
    ${state.selectMode ? `<div class="select-bar">
      <span><strong id="sel-count">${selN}</strong> selected</span>
      <span class="row" style="margin-left:auto;gap:8px">
        <button id="sel-clear" ${selN ? "" : "disabled"}>Clear</button>
        <button id="sel-import" class="primary" ${selN ? "" : "disabled"}>Import selected</button>
      </span>
    </div>` : ""}
    <div id="catalog-results"></div>`;

  const renderResults = () => {
    const q = (state.catalogQuery || "").toLowerCase().trim();
    const topic = state.catalogTopic || "";
    const matches = state.catalog.filter((bp) => {
      if (topic && (bp.category || "other") !== topic) return false;
      if (!q) return true;
      const hay = [bp.displayName, bp.description, bp.category, ...(bp.tags || [])]
        .filter(Boolean).join(" ").toLowerCase();
      return hay.includes(q);
    });
    const results = document.getElementById("catalog-results");
    if (!matches.length) {
      results.innerHTML = `<p class="muted">No blueprints match your filters.</p>`;
      return;
    }
    // All matching cards together in one grid (no per-topic sections).
    results.innerHTML = `<div class="grid">${matches.map(cardHTML).join("")}</div>`;
    matches.forEach((bp) => {
      const el = document.getElementById(`bp-${bp.id}`);
      if (!el) return;
      if (!state.selectMode) {
        el.addEventListener("click", () => openBlueprint(bp.id));
        return;
      }
      if (needsSetup(bp)) return; // blocked — needs per-blueprint setup, not selectable
      el.addEventListener("click", () => {
        if (state.selected.has(bp.id)) state.selected.delete(bp.id);
        else state.selected.add(bp.id);
        el.classList.toggle("selected", state.selected.has(bp.id));
        const cb = el.querySelector(".card-check");
        if (cb) cb.checked = state.selected.has(bp.id);
        updateSelBar();
      });
    });
  };

  const searchEl = document.getElementById("catalog-search");
  const topicEl = document.getElementById("catalog-topic");
  searchEl.addEventListener("input", () => { state.catalogQuery = searchEl.value; renderResults(); });
  topicEl.addEventListener("change", () => { state.catalogTopic = topicEl.value; renderResults(); });

  document.getElementById("select-toggle").addEventListener("click", () => {
    state.selectMode = !state.selectMode;
    if (!state.selectMode) state.selected.clear();
    renderCatalog();
  });
  if (state.selectMode) {
    document.getElementById("sel-clear").addEventListener("click", () => { state.selected.clear(); renderCatalog(); });
    document.getElementById("sel-import").addEventListener("click", () => { if (state.selected.size) setView("bulk"); });
  }
  renderResults();
}

// updateSelBar refreshes the sticky action bar count + button state without a
// full re-render (keeps search-box focus while ticking cards).
function updateSelBar() {
  const n = state.selected.size;
  const c = document.getElementById("sel-count"); if (c) c.textContent = n;
  const clr = document.getElementById("sel-clear"); if (clr) clr.disabled = !n;
  const imp = document.getElementById("sel-import"); if (imp) imp.disabled = !n;
}

// needsSetup reports whether a blueprint requires wizard input (e.g. an HF token)
// before it can be imported — such blueprints are excluded from bulk import.
function needsSetup(bp) {
  return (bp.importWizard?.inputs || []).some((i) => i.required);
}

function cardHTML(bp) {
  const tags = (bp.tags || []).map((t) => `<span class="badge neutral">${esc(t)}</span>`).join("");
  const sel = state.selectMode;
  const blocked = sel && needsSetup(bp);
  const checked = state.selected.has(bp.id);
  const cls = `card ${sel ? "selectable" : "clickable"}${checked ? " selected" : ""}${blocked ? " disabled" : ""}`;
  const check = sel
    ? `<input type="checkbox" class="card-check" ${checked ? "checked" : ""} ${blocked ? "disabled" : ""} aria-label="Select ${esc(bp.displayName)}">`
    : "";
  const setupBadge = blocked ? `<span class="badge warn">needs setup — import individually</span>` : "";
  return `<div class="${cls}" id="bp-${esc(bp.id)}">
    ${check}
    <h3>${esc(bp.displayName)}</h3>
    <p>${esc(bp.description)}</p>
    <div class="tags"><span class="badge">${esc(bp.category || "blueprint")}</span>${tags}${setupBadge}</div>
  </div>`;
}

async function openBlueprint(id) {
  state.bp = state.catalog.find((b) => b.id === id);
  state.step = 0; state.done = {}; state.namespace = "";
  state.bpContext = defaultBulkContext(); // default to the settings/current cluster
  state.bpModel = state.bp?.modelSizes?.default || ""; // default model size, if any
  setView("detail");
}

// modelSelectHTML renders a "Model" dropdown bound to state.bpModel, shown only when
// the blueprint defines modelSizes. The choice flows into import (CR) + frontend env.
function modelSelectHTML(id) {
  const ms = state.bp?.modelSizes;
  if (!ms || !(ms.options || []).length) return "";
  const cur = state.bpModel || ms.default || ms.options[0].id;
  const opts = ms.options.map((o) =>
    `<option value="${esc(o.id)}" ${o.id === cur ? "selected" : ""}>${esc(o.label || o.id)}</option>`).join("");
  return `<label class="cluster-select">Model
    <select id="${esc(id)}">${opts}</select></label>`;
}
function wireModelSelect(id, onChange) {
  const el = document.getElementById(id);
  if (!el) return;
  if (!state.bpModel && el.value) state.bpModel = el.value; // adopt the default
  el.addEventListener("change", () => { state.bpModel = el.value; onChange && onChange(); });
}

// clusterSelectHTML renders a "Target cluster" dropdown bound to state.bpContext,
// used in the guided demo so a blueprint installed on a downstream cluster is
// checked/port-forwarded there. wireClusterSelect binds change → onChange.
function clusterSelectHTML(id) {
  const cur = state.bpContext || defaultBulkContext();
  const opts = state.contexts.map((c) =>
    `<option value="${esc(c.name)}" ${c.name === cur ? "selected" : ""}>${esc(c.name)}${c.ready ? "" : " — AI Factory not detected"}</option>`).join("");
  return `<label class="cluster-select">Target cluster
    <select id="${esc(id)}" ${state.contexts.length ? "" : "disabled"}>${opts || '<option value="">no clusters found</option>'}</select></label>`;
}
function wireClusterSelect(id, onChange) {
  const el = document.getElementById(id);
  if (!el) return;
  if (!state.bpContext && el.value) state.bpContext = el.value; // adopt the default
  el.addEventListener("change", () => { state.bpContext = el.value; onChange && onChange(); });
}

// --- bulk import ---------------------------------------------------------- //
// The default cluster for a bulk import: the settings target, else the current
// kube context. The user can override it in the dropdown (Rancher downstream
// clusters — the batch must land on the right cluster or component access on the
// blueprint never turns green).
function defaultBulkContext() {
  return state.settings?.targetContext || state.contexts.find((c) => c.current)?.name || "";
}

function bulkRowHTML(bp) {
  return `<div class="card bulk-row" data-row="${esc(bp.id)}">
    <div class="check">
      <span class="status" data-st>•</span>
      <span><strong>${esc(bp.displayName)}</strong></span>
      <span class="muted" data-msg style="margin-left:auto">queued</span>
    </div>
    <pre class="log" data-log hidden></pre>
  </div>`;
}

function renderBulk() {
  const bps = [...state.selected].map((id) => state.catalog.find((b) => b.id === id)).filter(Boolean);
  if (!bps.length) { setView("catalog"); return; }
  const cur = defaultBulkContext();
  const ctxOptions = state.contexts.map((c) =>
    `<option value="${esc(c.name)}" ${c.name === cur ? "selected" : ""}>${esc(c.name)}${c.ready ? "" : " — AI Factory not detected"}</option>`).join("");

  app.innerHTML = `
    <button class="link back" id="back">← Catalog</button>
    <h2>Import ${bps.length} blueprint${bps.length === 1 ? "" : "s"}</h2>
    <div class="card section">
      <div class="row" style="gap:12px;align-items:flex-end;flex-wrap:wrap">
        <label style="flex:1;min-width:240px">Target cluster
          <select id="bulk-ctx" ${state.contexts.length ? "" : "disabled"}>${ctxOptions || '<option value="">no clusters found</option>'}</select>
        </label>
        <button class="primary" id="bulk-start" ${bps.length && cur ? "" : "disabled"}>Start import</button>
      </div>
      <p class="muted" style="margin-top:8px">Each blueprint is applied (<code>kubectl apply</code>) into the selected cluster, one after another.</p>
    </div>
    <div id="bulk-rows">${bps.map(bulkRowHTML).join("")}</div>
    <div id="bulk-summary" class="section"></div>`;

  document.getElementById("back").addEventListener("click", () => setView("catalog"));
  document.getElementById("bulk-start").addEventListener("click", () => runBulkImport(bps));
}

async function runBulkImport(bps) {
  const ctxSel = document.getElementById("bulk-ctx");
  const ctx = ctxSel.value;
  if (!ctx) return;
  const startBtn = document.getElementById("bulk-start");
  const back = document.getElementById("back");
  startBtn.disabled = true; startBtn.innerHTML = '<span class="spinner"></span>importing…';
  ctxSel.disabled = true; back.style.pointerEvents = "none";

  let ok = 0, fail = 0;
  for (const bp of bps) {
    const row = document.querySelector(`[data-row="${CSS.escape(bp.id)}"]`);
    const st = row.querySelector("[data-st]");
    const msg = row.querySelector("[data-msg]");
    const log = row.querySelector("[data-log]");
    st.className = "status"; st.textContent = "…"; msg.textContent = "importing…";
    log.hidden = false; log.textContent = "";
    await new Promise((resolve) => {
      let settled = false;
      const done = () => { if (!settled) { settled = true; resolve(); } };
      streamPost(`/api/blueprints/${bp.id}/import`, { context: ctx }, {
        onLog: (l) => { log.textContent += l + "\n"; log.scrollTop = log.scrollHeight; },
        onDone: (m) => { st.className = "status ok"; st.textContent = "✓"; msg.textContent = m || "imported"; ok++; done(); },
        onError: (m) => { st.className = "status bad"; st.textContent = "✗"; msg.textContent = m || "failed"; fail++; done(); },
      }).then(() => {
        if (!settled) { st.className = "status bad"; st.textContent = "✗"; msg.textContent = "no response"; fail++; done(); }
      });
    });
  }

  document.getElementById("bulk-summary").innerHTML =
    `<div class="card"><strong>${ok} imported${fail ? `, ${fail} failed` : ""}.</strong>
     <div class="muted">Target cluster: <code>${esc(ctx)}</code></div></div>`;
  startBtn.hidden = true; back.style.pointerEvents = "";
  state.selected.clear(); state.selectMode = false; // selection consumed
}

async function renderDetail() {
  const bp = state.bp;
  app.innerHTML = `
    <button class="link back" id="back">← Catalog</button>
    <h2>${esc(bp.displayName)}</h2>
    <p class="muted">${esc(bp.description)}</p>
    <div class="card section"><div class="row" style="gap:16px;flex-wrap:wrap;align-items:flex-end">${clusterSelectHTML("detail-ctx")}${modelSelectHTML("detail-model")}</div>
      <p class="muted" style="margin-top:6px">Prereqs, import and component access all run against this cluster.${bp.modelSizes ? " The model applies to both import and the local frontend." : ""}</p>
    </div>
    <div class="card section">
      <h3>Prerequisites <span class="muted" id="prereq-ctx"></span></h3>
      <div id="prereqs"><span class="spinner"></span>Checking…</div>
    </div>
    <div class="section row">
      <button class="primary" id="start-guide">Start guided demo →</button>
    </div>`;
  document.getElementById("back").addEventListener("click", () => setView("catalog"));
  document.getElementById("start-guide").addEventListener("click", () => { state.step = 0; setView("guide"); });
  wireClusterSelect("detail-ctx", () => renderDetail()); // re-check prereqs on the new cluster
  wireModelSelect("detail-model"); // choice used at import + frontend start
  try {
    const res = await getJSON(`/api/blueprints/${bp.id}/prereqs?context=${encodeURIComponent(state.bpContext || "")}`);
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
    <div class="guide-cluster"><div class="row" style="gap:16px;flex-wrap:wrap;align-items:flex-end">${clusterSelectHTML("guide-ctx")}${modelSelectHTML("guide-model")}</div></div>
    <div class="card">
      <h2>${esc(step.title)}</h2>
      <div class="step-body">${md(step.body)}</div>
      <div id="action"></div>
      <pre class="log" id="log" hidden></pre>
    </div>
    <div id="component-uis" class="section"></div>
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
  renderComponentUIs();
  refreshProcesses();
  // Switching cluster re-checks component access (and future actions use it too).
  wireClusterSelect("guide-ctx", () => renderComponentUIs());
  wireModelSelect("guide-model"); // used by the import + start-frontend actions
}

// Component UIs (e.g. Airflow) — a port-forward button per declared component,
// enabled only once the component's service is Ready (polled with a spinner).
function renderComponentUIs() {
  const host = document.getElementById("component-uis");
  const bp = state.bp;
  const uis = (bp && bp.componentUIs) || [];
  if (!uis.length) { if (host) host.innerHTML = ""; return; }
  if (!state.namespace) {
    host.innerHTML = `<div class="card"><h3>Component access</h3>
      <div class="muted">Set the AIWorkload namespace above to enable component UI access.</div></div>`;
    return;
  }
  host.innerHTML = `<div class="card"><h3>Component access</h3>${uis.map((u) => `
    <div class="check" data-cui="${esc(u.name)}">
      <span class="dot" data-dot></span>
      <span><strong>${esc(u.label || u.name)}</strong></span>
      <span class="muted" data-status style="margin-left:auto">checking…</span>
      <button data-open disabled style="max-width:120px">Open</button>
    </div>`).join("")}</div>`;
  uis.forEach((u) => wireComponentUI(u));
}

async function wireComponentUI(u) {
  const row = document.querySelector(`#component-uis [data-cui="${CSS.escape(u.name)}"]`);
  if (!row) return;
  const dot = row.querySelector("[data-dot]");
  const status = row.querySelector("[data-status]");
  const btn = row.querySelector("[data-open]");

  async function poll() {
    if (!document.body.contains(row)) return; // view changed; stop polling
    try {
      const r = await postJSON(`/api/blueprints/${state.bp.id}/service-status`, {
        namespace: state.namespace, service: u.service, context: state.bpContext,
      });
      if (r.ready) {
        dot.className = "dot ok"; status.textContent = "ready"; btn.disabled = false;
      } else {
        dot.className = "dot bad";
        status.innerHTML = '<span class="spinner"></span>waiting…';
        btn.disabled = true;
        setTimeout(poll, 5000);
      }
    } catch {
      dot.className = "dot bad"; status.textContent = "unavailable"; btn.disabled = true;
      setTimeout(poll, 8000);
    }
  }
  btn.addEventListener("click", async () => {
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
    try {
      const r = await postJSON(`/api/blueprints/${state.bp.id}/component-ui/start`, {
        namespace: state.namespace, name: u.name, context: state.bpContext,
      });
      window.open(r.url, "_blank");
      btn.textContent = "Open"; btn.disabled = false;
      refreshProcesses();
    } catch (e) { status.textContent = String(e); btn.textContent = "Open"; btn.disabled = false; }
  });
  poll();
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
    // Optional pre-import wizard: a checklist (options) and/or text inputs (e.g. an
    // HF token) injected into the blueprint CR before apply.
    const wiz = bp.importWizard;
    const opts = (wiz && wiz.options) || [];
    const inputs = (wiz && wiz.inputs) || [];
    let wizardHTML = "";
    if (wiz && (opts.length || inputs.length)) {
      wizardHTML = `<div class="card wizard">
        <h3>${esc(wiz.title || "Options")}</h3>
        ${wiz.body ? `<div class="step-body">${md(wiz.body)}</div>` : ""}
        ${inputs.map((i) => `
          <label class="wizard-input">
            <span><strong>${esc(i.label || i.id)}</strong>${i.required ? ' <span class="req">*</span>' : ""}
            ${i.description ? `<br><span class="muted">${esc(i.description)}</span>` : ""}</span>
            <input type="${i.secret ? "password" : "text"}" data-input="${esc(i.id)}"
              placeholder="${esc(i.placeholder || "")}" autocomplete="off" spellcheck="false" />
          </label>`).join("")}
        ${opts.map((o) => `
          <label class="check wizard-opt">
            <input type="checkbox" data-opt="${esc(o.id)}" ${o.default ? "checked" : ""} />
            <span><strong>${esc(o.label || o.id)}</strong>
            ${o.description ? `<br><span class="muted">${esc(o.description)}</span>` : ""}</span>
          </label>`).join("")}
      </div>`;
    }
    host.innerHTML = `${wizardHTML}<div class="section"><button class="primary" id="do">Import blueprint (kubectl apply)</button><div class="error" id="err"></div></div>`;
    document.getElementById("do").addEventListener("click", (e) => {
      const err = document.getElementById("err");
      err.textContent = "";
      const selections = Array.from(host.querySelectorAll("[data-opt]:checked")).map((c) => c.dataset.opt);
      const inputVals = {};
      for (const el of host.querySelectorAll("[data-input]")) inputVals[el.dataset.input] = el.value.trim();
      // Client-side required check so we don't hit the cluster with a missing token.
      const missing = inputs.find((i) => i.required && !inputVals[i.id]);
      if (missing) { err.textContent = `${missing.label || missing.id} is required`; return; }
      e.target.disabled = true; e.target.innerHTML = `<span class="spinner"></span>Importing…`;
      streamPost(`/api/blueprints/${bp.id}/import`, { selections, inputs: inputVals, context: state.bpContext, modelSize: state.bpModel }, {
        onLog: logLine,
        onDone: (m) => { logLine("✓ " + m); e.target.disabled = false; e.target.textContent = "Re-import"; markDone(); },
        onError: (m) => { err.textContent = m; e.target.disabled = false; e.target.textContent = "Retry import"; },
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
      renderComponentUIs();  // refresh the component-access panel with the new namespace
    });

  } else if (a.type === "start-frontend") {
    host.innerHTML = `<div class="section"><button class="primary" id="do">Start local frontend</button>
      <button class="danger" id="stop" style="margin-left:8px">Stop</button>
      <span id="link" style="margin-left:10px"></span><div class="error" id="err"></div></div>`;
    document.getElementById("do").addEventListener("click", (e) => {
      if (!state.namespace) { document.getElementById("err").textContent = "Set the AIWorkload namespace in the previous step first."; return; }
      e.target.disabled = true; e.target.innerHTML = `<span class="spinner"></span>Starting…`;
      streamPost(`/api/blueprints/${bp.id}/frontend/start`, { namespace: state.namespace, context: state.bpContext, modelSize: state.bpModel }, {
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
  host.innerHTML = `<div class="card"><h3>Running</h3>${procs.map((p, i) => procRowHTML(p, i)).join("")}</div>`;
  host.querySelectorAll("button[data-stop]").forEach((b) =>
    b.addEventListener("click", async () => { await stopProc(procs[+b.dataset.stop]); refreshProcesses(); }));
}

function kcRow(p) {
  return `<div class="kc-item"><code>${esc(p)}</code><button class="link" data-kc-remove="${esc(p)}">remove</button></div>`;
}

function wireKubeconfigImport() {
  const err = () => document.getElementById("kc-err");
  document.getElementById("kc-import").addEventListener("click", async (e) => {
    err().textContent = "";
    const content = document.getElementById("kc-content").value.trim();
    const name = document.getElementById("kc-name").value.trim();
    const path = document.getElementById("kc-path").value.trim();
    if (!content && !path) { err().textContent = "Paste a kubeconfig or enter a file path."; return; }
    e.target.disabled = true; e.target.innerHTML = `<span class="spinner"></span>Importing…`;
    try {
      const d = await postJSON("/api/kubeconfig/import", { content, name, path });
      state.contexts = d.contexts || [];
      state.settings = { ...(state.settings || {}), kubeconfigs: d.kubeconfigs || [] };
      renderSettings(); renderClusterChip();
    } catch (ex) {
      err().textContent = String(ex.message || ex);
      e.target.disabled = false; e.target.textContent = "Import";
    }
  });
  document.querySelectorAll("[data-kc-remove]").forEach((b) => b.addEventListener("click", async () => {
    try {
      const d = await postJSON("/api/kubeconfig/remove", { path: b.dataset.kcRemove });
      state.contexts = d.contexts || [];
      state.settings = { ...(state.settings || {}), kubeconfigs: d.kubeconfigs || [] };
      renderSettings(); renderClusterChip();
    } catch (ex) { err().textContent = String(ex.message || ex); }
  }));
}

// rancherClustersHTML renders the list of downstream clusters from the last connect,
// each with an Import button (or an "imported" badge).
function rancherClustersHTML() {
  const cl = state.rancherClusters || [];
  if (!cl.length) return "";
  return `<div class="section" style="margin-top:14px"><label>Downstream clusters</label>${cl.map(rancherRow).join("")}</div>`;
}
function rancherRow(c) {
  const right = c.imported
    ? `<span class="badge ok">imported</span>`
    : `<button class="link" data-rancher-import="${esc(c.id)}" data-rancher-name="${esc(c.name)}">import</button>`;
  return `<div class="kc-item"><code>${esc(c.name)}</code>${right}</div>`;
}

function wireRancher() {
  const err = () => document.getElementById("rancher-err");
  document.getElementById("rancher-connect").addEventListener("click", async (e) => {
    err().textContent = "";
    const url = document.getElementById("rancher-url").value.trim();
    const token = document.getElementById("rancher-token").value.trim();
    const insecure = document.getElementById("rancher-insecure").checked;
    if (!url || !token) { err().textContent = "Enter the Rancher URL and an API token."; return; }
    e.target.disabled = true; e.target.innerHTML = `<span class="spinner"></span>Connecting…`;
    try {
      const d = await postJSON("/api/rancher/connect", { url, token, insecure });
      state.rancherClusters = d.clusters || [];
      state.settings = { ...(state.settings || {}), rancherUrl: d.url || url, rancherInsecure: insecure, rancherConnected: true };
      renderSettings();
    } catch (ex) {
      err().textContent = String(ex.message || ex);
      e.target.disabled = false; e.target.textContent = "Connect";
    }
  });
  document.querySelectorAll("[data-rancher-import]").forEach((b) => b.addEventListener("click", async () => {
    err().textContent = "";
    b.disabled = true; b.textContent = "importing…";
    try {
      const d = await postJSON("/api/rancher/clusters/import", { id: b.dataset.rancherImport, name: b.dataset.rancherName });
      state.contexts = d.contexts || [];
      state.settings = { ...(state.settings || {}), kubeconfigs: d.kubeconfigs || [] };
      // Mark this cluster imported in the in-memory list, then re-render.
      state.rancherClusters = (state.rancherClusters || []).map((c) =>
        c.id === b.dataset.rancherImport ? { ...c, imported: true } : c);
      renderSettings(); renderClusterChip();
    } catch (ex) {
      err().textContent = String(ex.message || ex);
      b.disabled = false; b.textContent = "import";
    }
  }));
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
      <div class="row">
        <select id="ctx"><option value="">— host current-context —</option>${opts}</select>
        <button id="refresh-ctx" style="max-width:110px;margin-top:0">Refresh</button>
      </div>
      <div class="section row"><button class="primary" id="save">Save</button><span id="msg" class="muted"></span></div>
      <div class="error" id="err"></div>
    </div>

    <div class="card section" style="max-width:640px">
      <h3 style="margin:0 0 6px">Import a kubeconfig</h3>
      <div class="muted" style="margin-bottom:8px">Merge another kubeconfig so its contexts appear in the list above — paste its YAML or give a file path on this machine.</div>
      ${(s.kubeconfigs || []).length ? `<div id="kc-list">${(s.kubeconfigs || []).map(kcRow).join("")}</div>` : `<div class="muted" id="kc-list">No imported kubeconfigs.</div>`}
      <label>Paste kubeconfig YAML</label>
      <textarea id="kc-content" placeholder="apiVersion: v1\nkind: Config\n..." style="min-height:90px"></textarea>
      <div class="row">
        <input id="kc-name" placeholder="name (e.g. lima-k3s)" />
        <input id="kc-path" placeholder="…or a path, e.g. ~/.kube/lima.yaml" />
      </div>
      <div class="row"><button class="primary" id="kc-import" style="max-width:160px">Import</button><span id="kc-msg" class="muted"></span></div>
      <div class="error" id="kc-err"></div>
    </div>

    <div class="card section" style="max-width:640px">
      <h3 style="margin:0 0 6px">Rancher — import downstream clusters</h3>
      <div class="muted" style="margin-bottom:8px">Connect to a Rancher server with an API token to list its downstream clusters and import their kubeconfigs. Imported clusters become selectable targets everywhere. The token is kept in memory only — never written to disk.</div>
      <label>Rancher URL</label>
      <input id="rancher-url" placeholder="https://rancher.example.com" value="${esc(s.rancherUrl || "")}" />
      <label>API token</label>
      <input id="rancher-token" type="password" placeholder="token-xxxxx:xxxxxxxx" autocomplete="off" />
      <label class="check" style="text-transform:none;letter-spacing:normal;font-size:.85rem;color:var(--text)">
        <input type="checkbox" id="rancher-insecure" ${s.rancherInsecure ? "checked" : ""} style="width:16px;height:16px;flex:0 0 auto" />
        <span>Skip TLS verification (self-signed Rancher certificate)</span>
      </label>
      <div class="row"><button class="primary" id="rancher-connect" style="max-width:160px">Connect</button><span id="rancher-msg" class="muted">${s.rancherConnected ? "Connected this session." : ""}</span></div>
      <div class="error" id="rancher-err"></div>
      <div id="rancher-clusters">${rancherClustersHTML()}</div>
    </div>`;

  document.getElementById("refresh-ctx").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { state.contexts = await getJSON("/api/contexts"); } catch { state.contexts = []; }
    renderSettings();
  });
  wireKubeconfigImport();
  wireRancher();
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
// Copy-to-clipboard for md() fenced code blocks (delegated so it survives the
// full re-renders render() does). Works on localhost/HTTPS (secure context).
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".copy-btn");
  if (!btn) return;
  const code = btn.parentElement.querySelector("code");
  if (!code) return;
  const done = (label, ok) => {
    btn.textContent = label;
    btn.classList.toggle("copied", ok);
    setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("copied"); }, 1500);
  };
  try {
    await navigator.clipboard.writeText(code.textContent);
    done("Copied!", true);
  } catch {
    // Fallback: select the text so the user can hit ⌘/Ctrl-C.
    const r = document.createRange(); r.selectNodeContents(code);
    const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
    done("Press ⌘C", false);
  }
});

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
