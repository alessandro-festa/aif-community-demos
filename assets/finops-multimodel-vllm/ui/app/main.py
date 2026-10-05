"""
FinOps multi-model chat UI (SUSE) — backend.

A small custom chat frontend wired straight to the LiteLLM gateway's OpenAI-
compatible endpoint (Open WebUI is intentionally not used). It lets you:
  * pick one of the three priced models,
  * chat AS a given team (using that team's virtual key), so spend is attributed,
  * see each reply's COST, token usage and any guardrail block inline.

Every message is real token consumption that flows into LiteLLM's spend logs →
the litellm-exporter → Prometheus → the Grafana FinOps dashboards. So chatting here
is the interactive way to move the FinOps numbers.

Config (env; the marketplace injects these when it runs the frontend + port-forward):
  LITELLM_BASE_URL     http://localhost:4000   (port-forwarded litellm service)
  LITELLM_MASTER_KEY   sk-guardrails-demo      (used only to list models)
"""
from __future__ import annotations

import os
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000").rstrip("/")
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-guardrails-demo")
HTTP_TIMEOUT = int(os.environ.get("CHAT_TIMEOUT", "180"))
# The chat UI uses a SINGLE small model. The multi-model cost story lives in the
# backfilled dashboards, and the generate_traffic DAG still exercises all three.
CHAT_MODEL = os.environ.get("CHAT_MODEL", "qwen-0.5b")
# Optional system prompt. EMPTY by default on purpose: an instruction-style system
# message ("do not explain your reasoning…") trips the detect_prompt_injection
# guardrail ("Rejected message. This is a prompt injection attack."), which would
# block every chat. The models used here don't emit chain-of-thought anyway, so speed
# comes from the small warm model + low max_tokens. Set CHAT_SYSTEM_PROMPT to opt in
# (only sensible when the prompt-injection guardrail is off).
SYSTEM_PROMPT = os.environ.get("CHAT_SYSTEM_PROMPT", "").strip()
MAX_TOKENS = int(os.environ.get("CHAT_MAX_TOKENS", "256"))
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Teams for the selector. The chat UI calls LiteLLM with the MASTER key and attaches
# a team tag (x-litellm-tags) for attribution — virtual key values are hashed in the
# DB and can't be retrieved after creation, so the UI can't use per-team keys. Team +
# per-key spend is exercised by the generate_traffic DAG (which holds the keys).
TEAMS = [
    {"alias": "engineering",  "use_case": "code-assist"},
    {"alias": "data-science", "use_case": "analysis"},
    {"alias": "marketing",    "use_case": "copywriting"},
    {"alias": "support",      "use_case": "helpdesk"},
]
app = FastAPI(title="FinOps Multi-Model Chat (SUSE)")


@app.on_event("startup")
def _warm_up() -> None:
    """Pre-load the chat model in the background so the first message is fast."""
    import threading

    def _warm() -> None:
        try:
            requests.post(
                f"{LITELLM_BASE_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"},
                json={"model": CHAT_MODEL,
                      "messages": [{"role": "user", "content": "hi"}],
                      "max_tokens": 1},
                timeout=HTTP_TIMEOUT,
            )
        except Exception:  # noqa: BLE001
            pass  # best-effort; the model will load on the first real message

    threading.Thread(target=_warm, daemon=True).start()


def _team(alias: str) -> dict:
    for t in TEAMS:
        if t["alias"] == alias:
            return t
    raise HTTPException(400, f"unknown team {alias}")


@app.get("/api/health")
def health():
    ok = False
    try:
        requests.get(f"{LITELLM_BASE_URL}/health/liveliness", timeout=10).raise_for_status()
        ok = True
    except Exception:  # noqa: BLE001
        pass
    return {"litellm": ok, "base_url": LITELLM_BASE_URL}


@app.get("/api/config")
def config():
    """The single warm chat model + teams. Only one model is exposed on purpose
    (memory): the dashboards carry the multi-model cost story."""
    return {"models": [CHAT_MODEL],
            "teams": [{"alias": t["alias"], "use_case": t["use_case"]} for t in TEAMS]}


class ChatReq(BaseModel):
    model: str
    team: str
    message: str


@app.post("/api/chat")
def chat(req: ChatReq):
    team = _team(req.team)
    try:
        r = requests.post(
            f"{LITELLM_BASE_URL}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {LITELLM_MASTER_KEY}",
                "Content-Type": "application/json",
                "x-litellm-tags": f"team:{team['alias']},use_case:{team['use_case']}",
            },
            json={
                "model": req.model,
                "messages": (
                    ([{"role": "system", "content": SYSTEM_PROMPT}] if SYSTEM_PROMPT else [])
                    + [{"role": "user", "content": req.message}]
                ),
                "max_tokens": MAX_TOKENS,
                "temperature": 0.7,
            },
            timeout=HTTP_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"LiteLLM unreachable at {LITELLM_BASE_URL}: {e}")

    cost = float(r.headers.get("x-litellm-response-cost", 0) or 0)

    if r.status_code in (400, 403):
        # A guardrail block or budget/permission rejection — surface it as a "blocked"
        # message rather than an error so the demo reads clearly.
        detail = ""
        try:
            detail = (r.json().get("error", {}) or {}).get("message", "") or r.text
        except Exception:  # noqa: BLE001
            detail = r.text
        return {"blocked": True, "detail": detail[:500], "cost": cost, "team": req.team, "model": req.model}

    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text[:500])

    data = r.json()
    content = (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "")
    usage = data.get("usage", {}) or {}
    return {
        "blocked": False,
        "content": content,
        "cost": cost,
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "team": req.team,
        "model": req.model,
    }


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
