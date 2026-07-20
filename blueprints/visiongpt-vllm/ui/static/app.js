// VisionGPT (SUSE) — hazard-detection demo UI.

const $ = (id) => document.getElementById(id);
const esc = (s) => (s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let abort = null;

async function loadHealth() {
  try {
    const h = await (await fetch("/api/health")).json();
    $("health").textContent = `Model ${h.model} · endpoint ${h.base_url} · ${h.vlm ? "reachable ✓" : "unreachable ✗"}`;
    $("tag").textContent = `${h.model} · SUSE`;
  } catch (e) { $("health").textContent = "Backend unreachable: " + e; }
}

async function loadSamples() {
  try {
    const d = await (await fetch("/api/samples")).json();
    const sel = $("sample");
    sel.innerHTML = "";
    (d.samples || []).forEach((s) => { const o = document.createElement("option"); o.value = s; o.textContent = s; sel.appendChild(o); });
    if (!(d.samples || []).length) { const o = document.createElement("option"); o.value = ""; o.textContent = "(no bundled samples — upload one)"; sel.appendChild(o); }
  } catch { /* ignore */ }
}

function frameCard(r) {
  const danger = r.danger_score === 1;
  const color = danger ? "var(--danger)" : "var(--success)";
  const label = danger ? "HAZARD" : "clear";
  return `<div class="frame" style="border-color:${color}">
    <img src="data:image/jpeg;base64,${r.thumb}" />
    <div class="meta">
      <div class="ts">t=${r.t}s · <span style="color:${color};font-weight:700">${label}</span></div>
      <div>${esc(r.reason)}</div>
    </div>
  </div>`;
}

async function analyse() {
  $("error").textContent = "";
  $("frames").innerHTML = "";
  $("summary").innerHTML = '<span class="spinner"></span>Analysing…';
  const fd = new FormData();
  fd.append("sensitivity", $("sensitivity").value);
  const file = $("file").files[0];
  if (file) fd.append("file", file);
  else fd.append("sample", $("sample").value);

  $("run").disabled = true; $("stop").hidden = false;
  abort = new AbortController();
  let frames = 0, hazards = 0;
  try {
    const r = await fetch("/api/analyze", { method: "POST", body: fd, signal: abort.signal });
    if (!r.ok || !r.body) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n"); buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const obj = JSON.parse(line);
        if (obj.done) {
          $("summary").innerHTML = `<div class="summary">Analysed <strong>${obj.frames}</strong> frames · <strong style="color:var(--danger)">${obj.hazards}</strong> flagged as hazards (sensitivity: ${esc($("sensitivity").value)}).</div>`;
        } else {
          frames++; hazards += obj.danger_score;
          $("frames").insertAdjacentHTML("beforeend", frameCard(obj));
          $("summary").innerHTML = `<span class="spinner"></span>${frames} frames · ${hazards} hazards so far…`;
        }
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") { $("error").textContent = String(e); $("summary").textContent = "Analysis failed."; }
    else { $("summary").textContent = "Stopped."; }
  } finally {
    $("run").disabled = false; $("stop").hidden = true; abort = null;
  }
}

$("run").addEventListener("click", analyse);
$("stop").addEventListener("click", () => abort && abort.abort());
loadHealth();
loadSamples();
