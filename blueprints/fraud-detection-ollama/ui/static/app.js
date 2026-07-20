// Fraud / AML investigator dashboard.

const $ = (id) => document.getElementById(id);
const esc = (s) => (s ?? "").toString().replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v) => (v == null ? "—" : (typeof v === "number" ? v.toFixed(3) : v));

async function getJSON(u, o) {
  const r = await fetch(u, o);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

async function loadHealth() {
  try {
    const h = await getJSON("/api/health");
    const ok = (b) => (b ? "✓" : "✗");
    $("health").textContent = `Postgres ${ok(h.postgres)} · Milvus ${ok(h.milvus)} · LLM ${ok(h.llm)} (${h.model}) · pipeline ${h.ready ? "ready ✓" : "not run ✗"}`;
    $("tag").textContent = `${h.model} · SUSE`;
  } catch (e) { $("health").textContent = "backend unreachable: " + e; }
}

async function loadOverview() {
  try {
    const o = await getJSON("/api/overview");
    const m = o.metrics;
    $("overview").innerHTML = `
      <div class="tags">
        <span class="badge neutral">${o.stats.accounts} accounts</span>
        <span class="badge neutral">${o.stats.transactions} transactions</span>
        <span class="badge">${o.stats.rings} laundering rings</span>
        <span class="badge warn">${o.stats.flagged} flagged</span>
      </div>
      ${m ? `<div class="summary" style="margin-top:12px">Model — precision ${num(m.precision)} · recall ${num(m.recall)} · F1 ${num(m.f1)} · AUC ${num(m.auc)} (${m.n_fraud}/${m.n_accounts} labelled fraud)</div>` : `<div class="muted" style="margin-top:8px">No model metrics yet — run engineer_and_train.</div>`}`;
  } catch (e) { $("overview").innerHTML = `<span class="muted">${esc(String(e))}</span>`; }
}

async function loadFlagged() {
  try {
    const d = await getJSON("/api/flagged");
    if (!d.flagged.length) { $("flagged").innerHTML = `<span class="muted">none</span>`; return; }
    $("flagged").innerHTML = d.flagged.map((f) => `
      <div class="check" data-acc="${esc(f.account_id)}" style="cursor:pointer">
        <span class="badge ${f.in_ring ? "bad" : "neutral"}">${f.in_ring ? "ring" : "—"}</span>
        <span><strong>${esc(f.account_id)}</strong></span>
        <span class="muted">fraud ${num(f.xgb_score)} · anomaly ${num(f.anomaly_score)}</span>
        <span style="margin-left:auto" class="muted">#${f.rank}</span>
      </div>`).join("");
    $("flagged").querySelectorAll("[data-acc]").forEach((el) =>
      el.addEventListener("click", () => openCase(el.dataset.acc)));
  } catch (e) { $("flagged").innerHTML = `<span class="muted">${esc(String(e))}</span>`; }
}

let currentAcc = null;
async function openCase(id) {
  currentAcc = id;
  $("detail").innerHTML = '<span class="spinner"></span>Loading…';
  $("verdict").innerHTML = `<button class="primary" id="explain">Explain with LLM</button>`;
  $("explain").addEventListener("click", () => explain(id));
  try {
    const d = await getJSON(`/api/account/${encodeURIComponent(id)}`);
    const f = d.features || {};
    const txs = d.transactions.map((t) => `
      <div class="check">
        <span class="badge ${t.is_fraud_edge ? "bad" : "neutral"}">${t.is_fraud_edge ? "fraud" : "tx"}</span>
        <span class="muted">${esc(t.src_id)} → ${esc(t.dst_id)}</span>
        <span style="margin-left:auto">${num(t.amount)}</span>
      </div>`).join("");
    $("detail").innerHTML = `
      <div><strong>${esc(id)}</strong> — ${esc(d.account.customer_name || "")}</div>
      <div class="muted" style="margin:6px 0">balance ${num(d.account.balance)} · risk ${num(d.account.risk_score)} · in-ring ${f.in_cycle ? "yes" : "no"} · fraud score ${num(f.xgb_score)}</div>
      <div class="tags">${["out_degree","in_degree","high_value_edges","max_amount"].map((k) => `<span class="badge neutral">${k}=${num(f[k])}</span>`).join("")}</div>
      <h2 style="font-size:.9rem;margin:14px 0 6px">Top transactions</h2>${txs}`;
  } catch (e) { $("detail").innerHTML = `<span class="error">${esc(String(e))}</span>`; }
}

async function explain(id) {
  $("verdict").innerHTML = '<span class="spinner"></span>Analysing…';
  try {
    const d = await getJSON("/api/explain", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account_id: id }),
    });
    const v = d.verdict || {};
    $("verdict").innerHTML = `<div class="summary">
      <div><strong>Typology:</strong> ${esc(v.typology)}</div>
      <div style="margin-top:6px">${esc(v.risk_rationale)}</div>
      <div style="margin-top:6px"><strong>Action:</strong> ${esc(v.recommended_action)}</div>
      <div class="muted" style="margin-top:6px">confidence ${num(v.confidence)}</div>
    </div>`;
  } catch (e) { $("verdict").innerHTML = `<span class="error">${esc(String(e))}</span>`; }
}

loadHealth();
loadOverview();
loadFlagged();
