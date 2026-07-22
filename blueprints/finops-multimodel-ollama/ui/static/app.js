// FinOps multi-model chat UI.

const $ = (id) => document.getElementById(id);
const esc = (s) => (s ?? "").toString().replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (v) => "$" + (Number(v) || 0).toFixed(6);

let total = { cost: 0, tokens: 0, msgs: 0 };

async function getJSON(u, o) {
  const r = await fetch(u, o);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

async function loadHealth() {
  try {
    const h = await getJSON("/api/health");
    $("health").textContent = h.litellm ? `LiteLLM reachable ✓ (${h.base_url})` : `LiteLLM unreachable ✗ (${h.base_url})`;
  } catch (e) { $("health").textContent = "backend unreachable: " + e; }
}

async function loadConfig() {
  try {
    const c = await getJSON("/api/config");
    $("model").innerHTML = c.models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
    $("team").innerHTML = c.teams.map((t) => `<option value="${esc(t.alias)}">${esc(t.alias)} · ${esc(t.use_case)}</option>`).join("");
  } catch (e) {
    $("model").innerHTML = `<option>error loading models</option>`;
  }
}

function addMessage(cls, html) {
  const el = document.createElement("div");
  el.className = "msg " + cls;
  el.innerHTML = html;
  $("messages").appendChild(el);
  $("messages").scrollTop = $("messages").scrollHeight;
  return el;
}

function updateTotals() {
  $("t-cost").textContent = usd(total.cost);
  $("t-tokens").textContent = `${total.tokens} tokens`;
  $("t-msgs").textContent = `${total.msgs} messages`;
}

async function send() {
  const message = $("input").value.trim();
  if (!message) return;
  const model = $("model").value, team = $("team").value;
  $("input").value = "";
  $("send").disabled = true;

  addMessage("user", esc(message) + `<div class="meta"><span>${esc(team)}</span><span>${esc(model)}</span></div>`);
  const pending = addMessage("bot", '<span class="spinner"></span>Thinking…');

  try {
    const d = await getJSON("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model, team, message }),
    });
    if (d.blocked) {
      pending.className = "msg blocked";
      pending.innerHTML = `🛡️ <strong>Blocked by guardrail</strong><div style="margin-top:6px">${esc(d.detail)}</div>` +
        `<div class="meta"><span>${esc(d.model)}</span><span class="cost">${usd(d.cost)}</span></div>`;
    } else {
      const u = d.usage || {};
      pending.innerHTML = esc(d.content) +
        `<div class="meta"><span>${esc(d.model)}</span><span>${esc(d.team)}</span>` +
        `<span class="cost">${usd(d.cost)}</span>` +
        `<span>${u.prompt_tokens || 0}+${u.completion_tokens || 0}=${u.total_tokens || 0} tok</span></div>`;
      total.cost += Number(d.cost) || 0;
      total.tokens += Number(u.total_tokens) || 0;
    }
    total.msgs += 1;
    updateTotals();
  } catch (e) {
    pending.className = "msg blocked";
    pending.innerHTML = `<span class="error">${esc(String(e))}</span>`;
  } finally {
    $("send").disabled = false;
    $("input").focus();
  }
}

$("send").addEventListener("click", send);
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});

loadHealth();
loadConfig();
updateTotals();
