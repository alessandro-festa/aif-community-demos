"""
Data Governance Copilot (SUSE) — backend.

A small FastAPI UI that reads OpenMetadata's REST API and uses a local LLM (Ollama /
vLLM, OpenAI-compatible) to help with governance tasks:
  * discover  — natural-language search over the catalog (grounded in OpenMetadata);
  * glossary  — draft a business-glossary definition for a term;
  * lineage   — explain the lineage / impact of a data asset;
  * quality   — summarise data-quality status.

Config (env; the marketplace injects these + remaps localhost to the port-forwards):
  OM_BASE_URL     http://localhost:8585
  OM_USER         admin@open-metadata.org
  OM_PASSWORD     admin
  OPENAI_BASE_URL http://localhost:11434/v1
  LLM_MODEL       qwen2.5:1.5b
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
from urllib.parse import quote

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

OM_BASE_URL = os.environ.get("OM_BASE_URL", "http://localhost:8585").rstrip("/")
OM_USER = os.environ.get("OM_USER", "admin@open-metadata.org")
OM_PASSWORD = os.environ.get("OM_PASSWORD", "admin")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1").rstrip("/")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "EMPTY")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5:1.5b")
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "180"))
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Data Governance Copilot (SUSE)")

# --------------------------------------------------------------------------- #
# OpenMetadata REST (basic login -> JWT)
# --------------------------------------------------------------------------- #
_tok: dict = {"value": None}


def _om_token() -> str:
    if _tok["value"]:
        return _tok["value"]
    pw = base64.b64encode(OM_PASSWORD.encode()).decode()
    r = requests.post(f"{OM_BASE_URL}/api/v1/users/login",
                      json={"email": OM_USER, "password": pw}, timeout=30)
    r.raise_for_status()
    _tok["value"] = (r.json() or {}).get("accessToken", "")
    return _tok["value"]


def om(method: str, path: str, **kw) -> dict:
    def _call():
        h = {"Authorization": f"Bearer {_om_token()}"}
        return requests.request(method, f"{OM_BASE_URL}/api/v1{path}",
                                headers=h, timeout=HTTP_TIMEOUT, **kw)
    r = _call()
    if r.status_code == 401:
        _tok["value"] = None
        r = _call()
    r.raise_for_status()
    return r.json() if r.content else {}


def om_search(query: str, index: str = "table_search_index", size: int = 8) -> list[dict]:
    try:
        data = om("GET", f"/search/query?q={quote(query or '*')}"
                         f"&index={index}&size={size}")
        return [h.get("_source", {}) for h in
                (data.get("hits", {}).get("hits", []) if isinstance(data, dict) else [])]
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------- #
# LLM (OpenAI-compatible)
# --------------------------------------------------------------------------- #
def llm(system: str, user: str) -> str:
    try:
        r = requests.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"model": LLM_MODEL, "temperature": 0.3, "max_tokens": 400,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"LLM unreachable at {OPENAI_BASE_URL}: {e}")


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    ok = False
    try:
        requests.get(f"{OM_BASE_URL}/api/v1/system/version", timeout=10).raise_for_status()
        ok = True
    except Exception:  # noqa: BLE001
        pass
    return {"openmetadata": ok, "base_url": OM_BASE_URL, "model": LLM_MODEL}


@app.get("/api/config")
def config():
    return {"model": LLM_MODEL}


class Q(BaseModel):
    query: str


@app.post("/api/discover")
def discover(q: Q):
    """NL discovery: search the catalog, then let the LLM summarise the matches."""
    hits = om_search(q.query)
    ctx = "\n".join(
        f"- {h.get('fullyQualifiedName', h.get('name'))}: "
        f"{(h.get('description') or '')[:120]} "
        f"tags={[t.get('tagFQN') for t in (h.get('tags') or [])]}" for h in hits) or "(no matches)"
    answer = llm(
        "You are a data-governance assistant. Answer ONLY from the catalog context. "
        "Be concise; cite asset names.",
        f"Question: {q.query}\n\nCatalog matches:\n{ctx}")
    return {"answer": answer, "matches": [
        {"name": h.get("fullyQualifiedName", h.get("name")),
         "description": h.get("description"),
         "tags": [t.get("tagFQN") for t in (h.get("tags") or [])]} for h in hits]}


class Term(BaseModel):
    term: str


@app.post("/api/glossary-draft")
def glossary_draft(t: Term):
    existing = om_search(t.term, index="glossary_term_search_index", size=3)
    hint = "; ".join(e.get("description", "") for e in existing if e.get("description"))
    draft = llm(
        "You write concise business-glossary definitions (1-2 sentences) for a data catalog.",
        f"Define the business term '{t.term}'." + (f" Related context: {hint}" if hint else ""))
    return {"term": t.term, "definition": draft}


class Asset(BaseModel):
    fqn: str


@app.post("/api/lineage")
def lineage(a: Asset):
    try:
        lin = om("GET", f"/lineage/table/name/{a.fqn}?upstreamDepth=3&downstreamDepth=3")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(404, f"lineage for {a.fqn}: {e}")
    up = [n.get("fullyQualifiedName") for n in lin.get("nodes", [])]
    expl = llm(
        "You are a data-lineage analyst. Explain the impact of a change to the given asset "
        "using ONLY the connected assets listed.",
        f"Asset: {a.fqn}\nConnected assets: {up}")
    return {"fqn": a.fqn, "connected": up, "impact": expl}


@app.get("/api/quality")
def quality():
    try:
        data = om("GET", "/dataQuality/testCases?limit=100&fields=testCaseResult")
        cases = data.get("data", []) if isinstance(data, dict) else []
    except Exception:  # noqa: BLE001
        cases = []
    summary = [{"name": c.get("name"),
                "status": (c.get("testCaseResult") or {}).get("testCaseStatus", "unknown")}
               for c in cases]
    verdict = llm(
        "Summarise data-quality health in 2 sentences from the test results.",
        f"Test cases: {summary}") if summary else "No data-quality test cases found yet."
    return {"cases": summary, "summary": verdict}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
