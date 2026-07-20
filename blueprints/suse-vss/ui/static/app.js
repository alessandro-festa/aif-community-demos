// SUSE VSC frontend — minimal, dependency-free.
const $ = (id) => document.getElementById(id);
let activeTab = "url";

// Tab switching
$("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (!btn) return;
  activeTab = btn.dataset.tab;
  [...document.querySelectorAll("#tabs button")].forEach((b) =>
    b.classList.toggle("active", b === btn));
  ["url", "upload", "webcam", "youtube", "rtsp"].forEach((t) =>
    ($("pane-" + t).hidden = t !== activeTab));
  // Acquire the camera when entering the webcam tab; release it when leaving so
  // it isn't held open for other apps.
  if (activeTab === "webcam") ensureCam(); else stopCam();
  renderPreview();
});

// ---- Webcam ----------------------------------------------------------------
let camStream = null;

async function listCameras() {
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const cams = devices.filter((d) => d.kind === "videoinput");
    const sel = $("camera");
    const cur = sel.value;
    sel.innerHTML = cams.map((c, i) =>
      `<option value="${c.deviceId}">${escapeHtml(c.label || "Camera " + (i + 1))}</option>`).join("");
    if (cur) sel.value = cur;
  } catch (e) { /* ignore */ }
}

async function startCam() {
  stopCam();
  const sel = $("camera");
  const status = $("cam-status");
  const deviceId = sel.value;
  status.textContent = "Starting camera…";
  try {
    camStream = await navigator.mediaDevices.getUserMedia({
      video: deviceId ? { deviceId: { exact: deviceId } } : true, audio: false });
    $("cam").srcObject = camStream;
    status.textContent = "Camera live.";
    await listCameras(); // labels become available once permission is granted
    if (deviceId) sel.value = deviceId;
  } catch (e) {
    let msg = e.message || String(e);
    if (e.name === "NotReadableError" || e.name === "TrackStartError" || e.name === "AbortError")
      msg = "This camera is busy (in use by another app). Pick the other camera and press Start.";
    else if (e.name === "NotAllowedError") msg = "Camera permission was denied.";
    else if (e.name === "NotFoundError") msg = "No camera found.";
    status.innerHTML = '<span class="error">' + escapeHtml(msg) + "</span>";
  }
}

function stopCam() {
  if (camStream) { camStream.getTracks().forEach((t) => t.stop()); camStream = null; }
  const v = $("cam"); if (v) v.srcObject = null;
}

async function ensureCam() {
  await listCameras();
  if (!camStream) await startCam();
}

async function captureFrames(n) {
  const v = $("cam");
  if (!camStream || !v.videoWidth) throw new Error("camera is not running");
  const canvas = document.createElement("canvas");
  canvas.width = v.videoWidth; canvas.height = v.videoHeight;
  const ctx = canvas.getContext("2d");
  const blobs = [];
  for (let i = 0; i < n; i++) {
    ctx.drawImage(v, 0, 0);
    const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.85));
    if (blob) blobs.push(blob);
    if (i < n - 1) await new Promise((r) => setTimeout(r, 400));
  }
  return blobs;
}

document.addEventListener("DOMContentLoaded", () => {
  $("cam-start").addEventListener("click", (e) => { e.preventDefault(); startCam(); });
  $("camera").addEventListener("change", () => { if (camStream) startCam(); });
});

// Extract a YouTube video id from common URL shapes.
function ytId(url) {
  const m = (url || "").match(/(?:v=|youtu\.be\/|embed\/|shorts\/)([A-Za-z0-9_-]{11})/);
  return m ? m[1] : "";
}

// Render the input video so the user can compare input vs. model output.
function renderPreview() {
  const p = $("preview");
  if (activeTab === "youtube") {
    const id = ytId($("youtube").value);
    p.innerHTML = id
      ? `<div class="video-wrap"><iframe src="https://www.youtube.com/embed/${id}" allowfullscreen frameborder="0"></iframe></div>`
      : '<span class="muted">Enter a valid YouTube URL.</span>';
  } else if (activeTab === "upload") {
    const f = $("file").files[0];
    p.innerHTML = f
      ? `<video class="preview-vid" controls src="${URL.createObjectURL(f)}"></video>`
      : '<span class="muted">Choose a file to preview.</span>';
  } else if (activeTab === "url") {
    const u = $("url").value.trim();
    p.innerHTML = u
      ? `<video class="preview-vid" controls src="${u.replace(/"/g, "%22")}"></video>`
      : '<span class="muted">Enter a URL to preview.</span>';
  } else if (activeTab === "webcam") {
    p.innerHTML = '<span class="muted">The live camera is shown in the controls on the left; the captured frames appear below after analysis.</span>';
  } else {
    p.innerHTML = '<span class="muted">Live RTSP preview isn\'t supported in the browser — see the sampled frames below.</span>';
  }
}

// Load prompt presets
fetch("/api/prompts").then((r) => r.json()).then((d) => {
  const sel = $("prompt-id");
  d.prompts.forEach((p) => {
    const o = document.createElement("option");
    o.value = p.id; o.textContent = p.label; sel.appendChild(o);
  });
}).catch(() => {});

// Load model list (default + installed + CPU-friendly suggestions)
function loadModels(selectDefault) {
  return fetch("/api/models").then((r) => r.json()).then((d) => {
    const dl = $("model-list");
    const opts = [...new Set([...(d.installed || []), ...(d.suggested || [])])];
    dl.innerHTML = opts.map((m) => `<option value="${escapeHtml(m)}">`).join("");
    if (selectDefault && !$("model").value) $("model").value = d.default || "";
    $("model-status").textContent = (d.installed || []).length
      ? "Installed: " + d.installed.join(", ")
      : "No models pulled yet.";
  }).catch(() => {});
}
loadModels(true);

// Detect CPU-adaptive defaults (interval + cap + resolution) and show a hint.
let autoFrames = 6;
let autoInterval = 2;
fetch("/api/health").then((r) => r.json()).then((d) => {
  autoFrames = d.auto_frames || autoFrames;
  autoInterval = d.auto_interval || autoInterval;
  $("cpu-hint").textContent =
    `Auto: ~${d.cpus} CPU → 1 frame every ${d.auto_interval}s, up to ${d.auto_frames} frames @ ${d.max_side}px (override above).`;
}).catch(() => {});

// Pull a model on demand
$("pull").addEventListener("click", async (e) => {
  e.preventDefault();
  const name = $("model").value.trim();
  if (!name) { $("model-status").innerHTML = '<span class="error">Enter a model name.</span>'; return; }
  $("model-status").innerHTML = '<span class="spinner"></span> Pulling ' + escapeHtml(name) + ' … (this can take a while)';
  try {
    const fd = new FormData(); fd.append("name", name);
    const r = await fetch("/api/pull", { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    await loadModels(false);
    $("model-status").innerHTML = "Pulled <b>" + escapeHtml(name) + "</b>. " + $("model-status").textContent;
  } catch (e) {
    $("model-status").innerHTML = '<span class="error">Pull failed: ' + escapeHtml(e.message) + "</span>";
  }
});

// Analyse
$("run").addEventListener("click", async () => {
  const err = $("analyze-error"); err.textContent = "";
  const fd = new FormData();
  fd.append("source_type", activeTab);
  fd.append("prompt_id", $("prompt-id").value || "describe");
  fd.append("prompt", $("prompt").value || "");
  fd.append("model", $("model").value.trim() || "");
  fd.append("interval", $("interval").value || "0");   // seconds between frames; 0 = auto by CPU
  fd.append("frame_count", "0");                        // cap = auto by CPU

  if (activeTab === "upload") {
    const f = $("file").files[0];
    if (!f) { err.textContent = "Please choose a video file."; return; }
    fd.append("file", f);
  } else if (activeTab === "webcam") {
    let blobs;
    try { blobs = await captureFrames(autoFrames); }  // webcam: a quick burst of the auto count
    catch (e) { err.textContent = "Webcam capture failed: " + e.message + " — press Start first."; return; }
    blobs.forEach((b, i) => fd.append("frames", b, `frame${i}.jpg`));
  } else {
    const src = activeTab === "rtsp" ? $("rtsp").value
      : activeTab === "youtube" ? $("youtube").value
      : $("url").value;
    if (!src) { err.textContent = "Please enter a source URL."; return; }
    fd.append("source", src);
  }

  renderPreview();
  const btn = $("run");
  btn.disabled = true;
  $("stop").hidden = false;
  $("summary").className = "muted";
  $("summary").innerHTML = '<span class="spinner"></span> Starting…';
  $("frames-out").innerHTML = "";

  analyzeAbort = new AbortController();
  let frameCount = 0, total = 0, gotSummary = false;
  const setStatus = (t) => { if (!gotSummary) $("summary").innerHTML = '<span class="spinner"></span> ' + escapeHtml(t); };

  try {
    const r = await fetch("/api/analyze", { method: "POST", body: fd, signal: analyzeAbort.signal });
    if (!r.ok) { let m; try { m = (await r.json()).detail; } catch { m = r.statusText; } throw new Error(m); }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const raw = buf.slice(0, nl); buf = buf.slice(nl + 1);
        if (!raw.trim()) continue;
        const ev = JSON.parse(raw);
        if (ev.type === "status") { total = ev.total || total; setStatus(ev.msg); }
        else if (ev.type === "frame") {
          frameCount++;
          $("frames-out").insertAdjacentHTML("beforeend", `
            <div class="frame">
              <img src="data:image/jpeg;base64,${ev.thumb}" alt="frame" />
              <div class="meta"><div class="ts">t=${ev.ts}s</div>${escapeHtml(ev.caption)}</div>
            </div>`);
          setStatus(`Captioned ${frameCount}${total ? "/" + total : ""} frames…`);
        }
        else if (ev.type === "summary") {
          gotSummary = true;
          $("summary").className = "summary";
          $("summary").textContent = ev.summary;
        }
        else if (ev.type === "stopped") { if (!gotSummary) { $("summary").className = "muted"; $("summary").textContent = "Stopped. " + frameCount + " frame(s) captioned above."; } }
        else if (ev.type === "error") { throw new Error(ev.detail); }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") {
      $("summary").className = "muted";
      $("summary").textContent = "Stopped. " + frameCount + " frame(s) captioned above.";
    } else {
      if (!gotSummary) { $("summary").className = "muted"; $("summary").textContent = "Run an analysis to see the model's summary here."; }
      err.textContent = "Analysis failed: " + e.message;
    }
  } finally {
    btn.disabled = false;
    $("stop").hidden = true;
    analyzeAbort = null;
  }
});

// Stop the running analysis (aborts the request; backend stops calling the model).
let analyzeAbort = null;
$("stop").addEventListener("click", (e) => {
  e.preventDefault();
  if (analyzeAbort) analyzeAbort.abort();
});

// Search
$("search").addEventListener("click", async () => {
  const err = $("search-error"); err.textContent = "";
  const q = $("query").value.trim();
  if (!q) { err.textContent = "Enter a search query."; return; }
  const fd = new FormData(); fd.append("query", q);
  $("hits").innerHTML = '<span class="spinner"></span> Searching…';
  try {
    const r = await fetch("/api/search", { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const d = await r.json();
    if (!d.hits.length) { $("hits").innerHTML = '<div class="muted">No matches yet — analyse some videos first.</div>'; return; }
    $("hits").innerHTML = d.hits.map((h) => `
      <div class="hit">
        ${h.thumb ? `<img class="hit-img" src="data:image/jpeg;base64,${h.thumb}" alt="frame" />` : ""}
        <div class="hit-body">
          <div class="score">score ${h.score} · t=${h.ts}s</div>
          <div>${escapeHtml(h.caption)}</div>
          <div class="src">${escapeHtml(h.source || "")} · ${h.video_id}</div>
        </div>
      </div>`).join("");
  } catch (e) {
    $("hits").innerHTML = "";
    err.textContent = "Search failed: " + e.message;
  }
});

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
