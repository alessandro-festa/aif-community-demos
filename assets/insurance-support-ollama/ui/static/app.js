// Insurance Support Copilot — front-end (vanilla JS, no build step).
const $ = (id) => document.getElementById(id);
const esc = (s) => (s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Tiny safe markdown (headings/bold/code/lists) for assistant replies.
function mdToHtml(text) {
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  const lines = String(text || "").split("\n");
  const out = []; let list = null;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of lines) {
    const l = raw.trim();
    const h = l.match(/^(#{1,6})\s+(.*)$/);
    const ol = l.match(/^\d+[.)]\s+(.*)$/);
    const ul = l.match(/^[-*+]\s+(.*)$/);
    if (h) { close(); out.push(`<h4>${inline(h[2])}</h4>`); }
    else if (ol) { if (list !== "ol") { close(); out.push("<ol>"); list = "ol"; } out.push(`<li>${inline(ol[1])}</li>`); }
    else if (ul) { if (list !== "ul") { close(); out.push("<ul>"); list = "ul"; } out.push(`<li>${inline(ul[1])}</li>`); }
    else if (l === "") { close(); }
    else { close(); out.push(`<p>${inline(l)}</p>`); }
  }
  close(); return out.join("");
}

const history = [];   // [{role, content}] prior turns for the API
let attached = null;  // File (accident photo) for the next message

(async function init() {
  await loadModels();
  await loadTickets();
  $("send").addEventListener("click", send);
  $("msg").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send(); });
  $("file").addEventListener("change", () => {
    attached = $("file").files[0] || null;
    $("attach-name").textContent = attached ? `📎 ${attached.name}` : "";
  });
  $("t-create").addEventListener("click", createTicket);
  $("c-close").addEventListener("click", closeTicket);
  $("search").addEventListener("click", () => semanticSearch($("query").value.trim()));
  $("query").addEventListener("keydown", (e) => { if (e.key === "Enter") semanticSearch($("query").value.trim()); });
  addBubble("assistant", "Hi, I'm Ava. Tell me what happened — you can attach a photo of the damage. I can open or close a ticket and find similar past cases for you.");
})();

async function loadModels() {
  try {
    const d = await (await fetch("/api/models")).json();
    $("model").innerHTML = (d.models || []).map((m) =>
      `<option ${m === d.default ? "selected" : ""}>${esc(m)}</option>`).join("")
      || `<option value="">no model available</option>`;
    if (!(d.models || []).length) $("model-status").textContent = "Model endpoint reports no models yet.";
  } catch { $("model-status").textContent = "Could not reach the model endpoint."; }
}

async function loadTickets() {
  try {
    const d = await (await fetch("/api/tickets?limit=8")).json();
    const t = d.tickets || [];
    $("tickets").innerHTML = t.length ? t.map((x) =>
      `<div class="tick"><span class="tid">#${x.ticket_id}</span> ${esc(x.subject || "")}
       <span class="badge ${x.status === 'closed' ? 'ok' : 'warn'}">${esc(x.status)}</span></div>`).join("")
      : "No tickets yet.";
  } catch { $("tickets").textContent = "DB not reachable."; }
}

// ---- chat ----
function addBubble(role, html, isMd) {
  const el = document.createElement("div");
  el.className = `bubble ${role}`;
  el.innerHTML = isMd ? mdToHtml(html) : esc(html);
  $("chat").appendChild(el);
  $("chat").scrollTop = $("chat").scrollHeight;
  return el;
}

async function send() {
  const text = $("msg").value.trim();
  if (!text && !attached) return;
  $("chat-error").textContent = "";
  addBubble("user", text + (attached ? `  📎 ${attached.name}` : ""));
  $("msg").value = "";
  const fd = new FormData();
  fd.append("message", text);
  fd.append("history", JSON.stringify(history.slice(-8)));
  fd.append("model", $("model").value);
  if (attached) fd.append("file", attached);
  const btn = $("send"); btn.disabled = true; btn.innerHTML = `<span class="spinner"></span>`;
  const thinking = addBubble("assistant", "…");
  try {
    const r = await fetch("/api/chat", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    thinking.innerHTML = mdToHtml(d.reply || "(no reply)");
    history.push({ role: "user", content: text });
    if (d.reply) history.push({ role: "assistant", content: d.reply });
    if (d.cases && d.cases.length) renderCases(d.cases);
    if (d.proposed_action) renderProposed(d.proposed_action);
  } catch (e) {
    thinking.remove();
    $("chat-error").textContent = String(e.message || e);
  } finally {
    btn.disabled = false; btn.textContent = "Send";
    attached = null; $("file").value = ""; $("attach-name").textContent = "";
  }
}

// A model-proposed write action the user must confirm.
function renderProposed(a) {
  const wrap = addBubble("assistant", "");
  const args = a.arguments || {};
  if (a.name === "escalate_to_human") {
    wrap.innerHTML = `<div class="action"><strong>Escalate to a human agent?</strong>
      <div class="muted">${esc(args.reason || "")}</div>
      <button class="primary" id="do-esc">Escalate</button></div>`;
    wrap.querySelector("#do-esc").addEventListener("click", () => {
      wrap.innerHTML = mdToHtml("✓ Escalated to a human agent. Someone will follow up.");
    });
    return;
  }
  const isCreate = a.name === "create_ticket";
  wrap.innerHTML = `<div class="action">
    <strong>${isCreate ? "Open this ticket?" : "Close this ticket?"}</strong>
    <pre class="args">${esc(JSON.stringify(args, null, 2))}</pre>
    <button class="primary" id="do-act">${isCreate ? "Open ticket" : "Close ticket"}</button>
    <button id="cancel-act">Cancel</button></div>`;
  wrap.querySelector("#cancel-act").addEventListener("click", () => { wrap.innerHTML = mdToHtml("_Action cancelled._"); });
  wrap.querySelector("#do-act").addEventListener("click", async () => {
    try {
      const fd = new FormData();
      let url;
      if (isCreate) {
        url = "/api/tickets/create";
        fd.append("subject", args.subject || "Support request");
        fd.append("body", args.body || "");
        fd.append("priority", args.priority || "medium");
      } else {
        url = "/api/tickets/close";
        fd.append("ticket_id", args.ticket_id || 0);
        fd.append("resolution_notes", args.resolution_notes || "");
      }
      const d = await (await fetch(url, { method: "POST", body: fd })).json();
      if (d.error) throw new Error(d.error);
      wrap.innerHTML = mdToHtml(isCreate
        ? `✓ Opened ticket **#${d.ticket_id}** (status: ${d.status}).`
        : `✓ Closed ticket **#${d.ticket_id}**.`);
      loadTickets();
    } catch (e) { wrap.innerHTML = `<div class="error">${esc(String(e.message || e))}</div>`; }
  });
}

// ---- similar cases ----
function renderCases(cases) {
  if (!cases.length) { $("cases").textContent = "No similar cases found yet."; return; }
  $("cases").classList.remove("muted");
  $("cases").innerHTML = cases.map((c) => `
    <div class="case">
      <div class="case-head">
        <span class="score">cosine ${c.score}</span>
        <span class="badge neutral">${esc(c.accident_type || "")}</span>
        <span class="badge ${c.was_paid ? 'ok' : 'bad'}">${c.was_paid ? "paid" : "not paid"}</span>
        <span class="badge ${c.within_policy ? 'ok' : 'warn'}">${c.within_policy ? "within policy" : "out of policy"}</span>
      </div>
      <div><strong>${esc(c.subject || "")}</strong></div>
      <div class="muted">${esc(c.body || "")}</div>
      ${c.resolution ? `<div class="res">→ ${esc(c.resolution)}</div>` : ""}
    </div>`).join("");
}

async function semanticSearch(q) {
  if (!q) return;
  $("cases").innerHTML = `<div class="muted"><span class="spinner"></span>Searching…</div>`;
  try {
    const fd = new FormData(); fd.append("query", q); fd.append("top_k", "5");
    const d = await (await fetch("/api/search/semantic", { method: "POST", body: fd })).json();
    if (d.error) throw new Error(d.error);
    renderCases(d.cases || []);
  } catch (e) { $("cases").innerHTML = `<div class="error">${esc(String(e.message || e))}</div>`; }
}

// ---- guided ticket forms ----
async function createTicket() {
  const subject = $("t-subject").value.trim();
  const body = $("t-body").value.trim();
  if (!subject) { $("side-msg").textContent = "Subject is required."; return; }
  try {
    const fd = new FormData();
    fd.append("subject", subject); fd.append("body", body); fd.append("priority", $("t-priority").value);
    const d = await (await fetch("/api/tickets/create", { method: "POST", body: fd })).json();
    if (d.error) throw new Error(d.error);
    $("side-msg").textContent = `Opened ticket #${d.ticket_id}.`;
    $("t-subject").value = ""; $("t-body").value = ""; loadTickets();
  } catch (e) { $("side-msg").textContent = String(e.message || e); }
}

async function closeTicket() {
  const id = $("c-id").value.trim();
  if (!id) { $("side-msg").textContent = "Ticket # is required."; return; }
  try {
    const fd = new FormData();
    fd.append("ticket_id", id); fd.append("resolution_notes", $("c-notes").value.trim());
    const d = await (await fetch("/api/tickets/close", { method: "POST", body: fd })).json();
    if (d.error) throw new Error(d.error);
    $("side-msg").textContent = `Closed ticket #${d.ticket_id}.`;
    $("c-id").value = ""; $("c-notes").value = ""; loadTickets();
  } catch (e) { $("side-msg").textContent = String(e.message || e); }
}
