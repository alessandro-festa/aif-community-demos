// Astra — Airflow GenAI RAG demo UI.
// Talks to the FastAPI backend (app/main.py), which proxies Ollama + Milvus.

const $ = (id) => document.getElementById(id);

function esc(s) {
  return (s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// --- Health + model list -------------------------------------------------- //
async function loadHealth() {
  try {
    const r = await fetch("/api/health");
    const h = await r.json();
    const ok = (b) => (b ? "✓" : "✗");
    $("health").textContent =
      `Ollama ${ok(h.ollama)} · Milvus ${ok(h.milvus)} · collection "${h.collection}" ${ok(h.collection_ready)}`;
  } catch (e) {
    $("health").textContent = "Backend unreachable: " + e;
  }
}

async function loadModels() {
  try {
    const r = await fetch("/api/models");
    const data = await r.json();
    const list = $("model-list");
    list.innerHTML = "";
    (data.models || []).forEach((m) => {
      const o = document.createElement("option");
      o.value = m;
      list.appendChild(o);
    });
    if (data.default && !$("model").value) $("model").value = data.default;
    $("model-status").textContent = (data.models || []).length
      ? `${data.models.length} model(s) available`
      : "No models found on Ollama yet.";
  } catch (e) {
    $("model-status").textContent = "Could not list models: " + e;
  }
}

// --- Generate ------------------------------------------------------------- //
async function generate() {
  const topic = $("topic").value.trim();
  $("gen-error").textContent = "";
  if (!topic) {
    $("gen-error").textContent = "Enter a topic first.";
    return;
  }
  const btn = $("run");
  btn.disabled = true;
  $("post").innerHTML = '<span class="spinner"></span>Generating…';
  $("sources").textContent = "…";
  try {
    const r = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        topic,
        model: $("model").value.trim() || undefined,
        top_k: parseInt($("topk").value, 10) || 4,
      }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const data = await r.json();
    $("post").innerHTML = `<div class="summary">${esc(data.post)}</div>`;
    renderSources($("sources"), data.sources);
  } catch (e) {
    $("gen-error").textContent = String(e);
    $("post").textContent = "Generation failed.";
  } finally {
    btn.disabled = false;
  }
}

// --- Search --------------------------------------------------------------- //
async function search() {
  const query = $("query").value.trim();
  $("search-error").textContent = "";
  if (!query) return;
  $("hits").innerHTML = '<span class="spinner"></span>Searching…';
  try {
    const r = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, top_k: parseInt($("topk").value, 10) || 4 }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const data = await r.json();
    renderSources($("hits"), data.sources);
  } catch (e) {
    $("search-error").textContent = String(e);
    $("hits").innerHTML = "";
  }
}

function renderSources(el, sources) {
  if (!sources || !sources.length) {
    el.innerHTML = '<span class="muted">No matches.</span>';
    return;
  }
  el.innerHTML = sources
    .map(
      (s) => `
      <div class="hit">
        <div class="hit-body">
          <div><strong>${esc(s.title)}</strong> <span class="score">${(s.score ?? 0).toFixed(3)}</span></div>
          <div class="src">${esc(s.source)}</div>
          <div>${esc(s.text)}</div>
        </div>
      </div>`
    )
    .join("");
}

// --- Wire up -------------------------------------------------------------- //
$("run").addEventListener("click", generate);
$("search").addEventListener("click", search);
loadHealth();
loadModels();
