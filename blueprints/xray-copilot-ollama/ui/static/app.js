// Chest X-ray Copilot — front-end logic (vanilla JS, no build step).
const $ = (id) => document.getElementById(id);
const esc = (s) => (s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Tiny, safe markdown-ish renderer for the model's analysis (escapes first).
// Handles # / ## / ### headings, **bold**, `code`, and - / * / 1. lists.
function mdToHtml(text) {
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  const lines = String(text || "").split("\n");
  const out = [];
  let list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of lines) {
    const l = raw.trim();
    const h = l.match(/^(#{1,6})\s+(.*)$/);
    const ol = l.match(/^\d+[.)]\s+(.*)$/);
    const ul = l.match(/^[-*+]\s+(.*)$/);
    if (h) { closeList(); const lvl = Math.min(h[1].length + 2, 6); out.push(`<h${lvl}>${inline(h[2])}</h${lvl}>`); }
    else if (ol) { if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; } out.push(`<li>${inline(ol[1])}</li>`); }
    else if (ul) { if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; } out.push(`<li>${inline(ul[1])}</li>`); }
    else if (l === "") { closeList(); }
    else { closeList(); out.push(`<p>${inline(l)}</p>`); }
  }
  closeList();
  return out.join("");
}

let selectedSample = "";       // chosen sample filename ("" when a file is uploaded)
let lastImage = null;          // {sample} or {file} used for the last analysis (for "find similar")

// ---- init ------------------------------------------------------------------
(async function init() {
  await Promise.all([loadModels(), loadSamples()]);
  $("file").addEventListener("change", () => {
    selectedSample = "";
    document.querySelectorAll(".sample.active").forEach((e) => e.classList.remove("active"));
    const f = $("file").files[0];
    if (f) showPreview(URL.createObjectURL(f));
  });
  $("run").addEventListener("click", analyze);
  $("find-similar").addEventListener("click", findSimilar);
  $("search").addEventListener("click", semanticSearch);
  $("query").addEventListener("keydown", (e) => { if (e.key === "Enter") semanticSearch(); });
})();

async function loadModels() {
  try {
    const r = await fetch("/api/models");
    const d = await r.json();
    const sel = $("model");
    sel.innerHTML = (d.models || []).map((m) =>
      `<option value="${esc(m)}" ${m === d.default ? "selected" : ""}>${esc(m)}</option>`).join("");
    if (!(d.models || []).length) {
      sel.innerHTML = `<option value="">no model available</option>`;
      $("model-status").textContent = "The model endpoint reports no models yet — is the workload Ready?";
    }
  } catch {
    $("model-status").textContent = "Could not reach the model endpoint.";
  }
}

async function loadSamples() {
  try {
    const r = await fetch("/api/samples");
    const d = await r.json();
    const host = $("samples");
    if (!(d.samples || []).length) { host.textContent = "No bundled samples."; return; }
    host.classList.remove("muted");
    host.innerHTML = d.samples.map((s) => `
      <button class="sample" data-name="${esc(s.name)}" title="${esc(s.label)}">
        <img src="${esc(s.url)}" alt="${esc(s.label)}" />
        <span>${esc(s.label)}</span>
      </button>`).join("");
    host.querySelectorAll(".sample").forEach((btn) => btn.addEventListener("click", () => {
      selectedSample = btn.dataset.name;
      $("file").value = "";
      host.querySelectorAll(".sample.active").forEach((e) => e.classList.remove("active"));
      btn.classList.add("active");
      showPreview(btn.querySelector("img").src);
    }));
  } catch {
    $("samples").textContent = "Could not load samples.";
  }
}

function showPreview(url) {
  const img = $("preview");
  img.src = url; img.hidden = false;
  $("preview-hint").hidden = true;
}

function imageFormData() {
  const fd = new FormData();
  const f = $("file").files[0];
  if (f) fd.append("file", f);
  else if (selectedSample) fd.append("sample", selectedSample);
  else return null;
  return fd;
}

// ---- analyze ---------------------------------------------------------------
async function analyze() {
  const err = $("analyze-error"); err.textContent = "";
  const fd = imageFormData();
  if (!fd) { err.textContent = "Upload an X-ray or pick a sample first."; return; }
  fd.append("model", $("model").value);
  fd.append("question", $("question").value);
  lastImage = $("file").files[0] ? { file: $("file").files[0] } : { sample: selectedSample };

  const btn = $("run");
  btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>Analysing…`;
  $("analysis").textContent = "";
  try {
    const r = await fetch("/api/analyze", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    const a = $("analysis"); a.classList.remove("muted");
    a.innerHTML = mdToHtml(d.analysis);
    $("store-status").textContent = d.stored
      ? `Indexed in Milvus (model: ${d.model}). You can now search for similar X-rays.`
      : `Analysis done (model: ${d.model}). Not indexed — Milvus/embeddings unavailable.`;
    $("find-similar").hidden = !d.stored;
  } catch (e) {
    err.textContent = String(e.message || e);
  } finally {
    btn.disabled = false; btn.textContent = "Analyse";
  }
}

// ---- search ----------------------------------------------------------------
async function findSimilar() {
  const err = $("search-error"); err.textContent = "";
  const fd = new FormData();
  if (lastImage?.file) fd.append("file", lastImage.file);
  else if (lastImage?.sample) fd.append("sample", lastImage.sample);
  else { err.textContent = "Analyse an image first."; return; }
  fd.append("top_k", "8");
  await runSearch("/api/search/similar", fd, "Similar to the analysed X-ray");
}

async function semanticSearch() {
  const err = $("search-error"); err.textContent = "";
  const q = $("query").value.trim();
  if (!q) { err.textContent = "Type a search query."; return; }
  const fd = new FormData();
  fd.append("query", q); fd.append("top_k", "8");
  await runSearch("/api/search/semantic", fd, `Results for “${q}”`);
}

async function runSearch(url, fd, heading) {
  const hits = $("hits");
  hits.innerHTML = `<div class="muted"><span class="spinner"></span>Searching…</div>`;
  try {
    const r = await fetch(url, { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    if (!(d.hits || []).length) { hits.innerHTML = `<div class="muted">No matches yet — analyse a few X-rays to build the index.</div>`; return; }
    hits.innerHTML = `<div class="muted" style="margin-bottom:8px">${esc(heading)}</div>` +
      d.hits.map((h) => `
        <div class="hit">
          <img class="hit-img" src="data:image/jpeg;base64,${h.thumb}" alt="" />
          <div class="hit-body">
            <div class="score">cosine ${h.score} · ${esc(h.model || "")}</div>
            <div>${esc(h.filename || "")}</div>
            <div class="src">${esc((h.analysis || "").slice(0, 160))}</div>
          </div>
        </div>`).join("");
  } catch (e) {
    $("search-error").textContent = String(e.message || e);
    hits.innerHTML = "";
  }
}
