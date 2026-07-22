// DORA Compliance Analysis dashboard.

const $ = (id) => document.getElementById(id);
const esc = (s) => (s ?? "").toString().replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v) => (v == null ? "—" : (typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(2)) : v));
const eur = (v) => (v == null ? "—" : "€" + Math.round(v).toLocaleString());
const sevClass = (s) => ({ critical: "bad", major: "warn", minor: "neutral" }[s] || "neutral");
const when = (v) => (v ? new Date(v).toLocaleString() : "—");

async function getJSON(u, o) {
  const r = await fetch(u, o);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

async function loadHealth() {
  try {
    const h = await getJSON("/api/health");
    const ok = (b) => (b ? "✓" : "✗");
    $("health").textContent = `Postgres ${ok(h.postgres)} · Milvus ${ok(h.milvus)} · LLM ${ok(h.llm)} (${h.model}) · Airflow ${ok(h.airflow)} · pipeline ${h.ready ? "ready ✓" : "not run ✗"}`;
    $("tag").textContent = `${h.model} · SUSE`;
  } catch (e) { $("health").textContent = "backend unreachable: " + e; }
}

async function loadOverview() {
  try {
    const o = await getJSON("/api/overview");
    const s = o.stats, b = o.breaches || {};
    const vendors = (o.vendors || []).map((v) => `
      <div class="check">
        <span class="badge ${v.critical ? "bad" : v.major ? "warn" : "neutral"}">${esc(v.provider_tier || "—")}</span>
        <span><strong>${esc(v.provider)}</strong></span>
        <span class="muted">${v.incidents} inc · ${v.critical}C/${v.major}M · ${eur(v.total_impact_eur)}</span>
      </div>`).join("");
    $("overview").innerHTML = `
      <div class="tags">
        <span class="badge neutral">${s.incidents} incidents</span>
        <span class="badge bad">${s.critical} critical</span>
        <span class="badge warn">${s.major} major</span>
        <span class="badge neutral">${s.minor} minor</span>
      </div>
      <div class="tags" style="margin-top:8px">
        <span class="badge">${s.reportable} BaFin-reportable</span>
        <span class="badge ${b.BREACHED ? "bad" : "neutral"}">${b.BREACHED || 0} deadline breached</span>
        <span class="badge ${b.IMMINENT ? "warn" : "neutral"}">${b.IMMINENT || 0} imminent</span>
      </div>
      ${vendors ? `<h2 style="font-size:.85rem;margin:14px 0 6px">Top ICT provider risk</h2>${vendors}` : ""}`;
  } catch (e) { $("overview").innerHTML = `<span class="muted">${esc(String(e))}</span>`; }
}

let currentSev = "";
async function loadIncidents() {
  try {
    const d = await getJSON("/api/incidents" + (currentSev ? `?severity=${currentSev}` : ""));
    if (!d.incidents.length) { $("incidents").innerHTML = `<span class="muted">none</span>`; return; }
    $("incidents").innerHTML = d.incidents.map((i) => `
      <div class="check" data-id="${esc(i.incident_id)}" style="cursor:pointer">
        <span class="badge ${sevClass(i.dora_severity)}">${esc(i.dora_severity)}</span>
        <span><strong>${esc(i.incident_type)}</strong> <span class="muted">${esc(i.ict_third_party_provider || "")}</span></span>
        <span style="margin-left:auto" class="muted">${num(i.clients_affected_pct)}% · ${eur(i.financial_impact_eur)}</span>
      </div>`).join("");
    $("incidents").querySelectorAll("[data-id]").forEach((el) =>
      el.addEventListener("click", () => openIncident(el.dataset.id)));
  } catch (e) { $("incidents").innerHTML = `<span class="muted">${esc(String(e))}</span>`; }
}

async function openIncident(id) {
  $("detail").innerHTML = '<span class="spinner"></span>Loading…';
  $("verdict").innerHTML = `<button class="primary" id="explain">Explain</button>`;
  $("explain").addEventListener("click", () => explain(id));
  try {
    const d = await getJSON(`/api/incident/${encodeURIComponent(id)}`);
    const i = d.incident;
    $("detail").innerHTML = `
      <div><span class="badge ${sevClass(i.dora_severity)}">${esc(i.dora_severity)}</span>
        <strong>${esc(i.incident_type)}</strong> — ${esc(i.institution_type)} ${esc(i.institution_id)}</div>
      <div class="muted" style="margin:8px 0">${esc(i.incident_id)}</div>
      <div class="summary">${esc(i.description)}</div>
      <div class="tags" style="margin-top:10px">
        <span class="badge neutral">clients ${num(i.clients_affected_pct)}%</span>
        <span class="badge neutral">impact ${eur(i.financial_impact_eur)}</span>
        <span class="badge neutral">provider ${esc(i.ict_third_party_provider || "none")}</span>
        <span class="badge neutral">cross-border ${i.is_cross_border ? "yes" : "no"}</span>
      </div>
      <div style="margin-top:10px" class="muted">
        Detected ${when(i.detection_ts)}${i.deadline_ts ? ` · BaFin deadline ${when(i.deadline_ts)} (${i.deadline_hours}h)` : " · no reporting required"}
      </div>
      <div style="margin-top:6px"><strong>Rule:</strong> ${esc(i.classification_reason)}</div>`;
  } catch (e) { $("detail").innerHTML = `<span class="error">${esc(String(e))}</span>`; }
}

async function explain(id) {
  $("verdict").innerHTML = '<span class="spinner"></span>Analysing…';
  try {
    const d = await getJSON("/api/explain", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ incident_id: id }),
    });
    const v = d.verdict || {};
    $("verdict").innerHTML = `<div class="summary">
      <div><strong>Severity:</strong> ${esc(d.classification)} — ${esc(v.severity_justification)}</div>
      <div style="margin-top:6px"><strong>Authority &amp; deadline:</strong> ${esc(v.authority_and_deadline)}</div>
      <div style="margin-top:6px"><strong>Action:</strong> ${esc(v.recommended_action)}</div>
      <div class="muted" style="margin-top:6px">confidence ${num(v.confidence)}</div>
    </div>`;
  } catch (e) { $("verdict").innerHTML = `<span class="error">${esc(String(e))}</span>`; }
}

// -------- compliance agent (tool-calling) --------
const history = [];

function renderTrace(trace) {
  if (!trace || !trace.length) return "";
  return `<div class="trace">${trace.map((t) => {
    const r = t.result || {};
    const n = r.count ?? (r.matches || r.triggered || r.status || r.breaches || r.vendors || r.reportable || r.incidents || []).length;
    const summary = r.error ? `error: ${esc(r.error)}` : (n != null ? `${n} result${n === 1 ? "" : "s"}` : "ok");
    return `<div class="tstep">🔧 <strong>${esc(t.tool)}</strong>(${esc(JSON.stringify(t.args))}) → ${summary}</div>`;
  }).join("")}</div>`;
}

function addBubble(role, html) {
  const div = document.createElement("div");
  div.className = "bubble " + role;
  div.innerHTML = html;
  $("chat").appendChild(div);
  $("chat").scrollTop = $("chat").scrollHeight;
  return div;
}

async function ask(text) {
  const q = (text ?? $("q").value).trim();
  if (!q) return;
  $("q").value = "";
  addBubble("user", esc(q));
  const thinking = addBubble("assistant", '<span class="spinner"></span>Working…');
  $("ask").disabled = true;
  try {
    const d = await getJSON("/api/agent", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: q, history }),
    });
    thinking.innerHTML = esc(d.reply) + renderTrace(d.trace);
    history.push({ role: "user", content: q }, { role: "assistant", content: d.reply });
  } catch (e) {
    thinking.innerHTML = `<span class="error">${esc(String(e))}</span>`;
  } finally { $("ask").disabled = false; }
}

const EXAMPLES = [
  "Which ICT providers caused critical incidents?",
  "Show breached reporting deadlines",
  "Find incidents like a cyber attack on the payments gateway",
  "Run the whole pipeline",
];

function init() {
  $("filters").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      $("filters").querySelectorAll("button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      currentSev = b.dataset.sev;
      loadIncidents();
    }));
  $("ask").addEventListener("click", () => ask());
  $("q").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) ask(); });
  $("examples").innerHTML = EXAMPLES.map((e) => `<span class="chip">${esc(e)}</span>`).join("");
  $("examples").querySelectorAll(".chip").forEach((c, i) =>
    c.addEventListener("click", () => ask(EXAMPLES[i])));

  loadHealth();
  loadOverview();
  loadIncidents();
}

init();
