"""
Astra — Airflow GenAI RAG demo UI (backend).

A small FastAPI app that mirrors the suse-vss UI style. It performs RAG generation
against the same in-cluster services the Airflow pipeline populates:

    embed the topic (Ollama nomic-embed-text)
      -> search Qdrant `kb` for the closest chunks
      -> build a grounded prompt
      -> generate a post (Ollama, default the customized `astra-custom` model)

Run locally (with `kubectl port-forward` to ollama:11434 and qdrant:6333):

    pip install -r requirements.txt
    uvicorn app.main:app --host 0.0.0.0 --port 8000
    open http://localhost:8000

Configuration (env):
    OLLAMA_BASE_URL  default http://localhost:11434
    QDRANT_URL       default http://localhost:6333
    EMBED_MODEL      default nomic-embed-text
    GEN_MODEL        default astra-custom
    KB_COLLECTION    default kb
    QDRANT_API_KEY   optional Qdrant API key
"""
from __future__ import annotations

import os
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
GEN_MODEL = os.environ.get("GEN_MODEL", "astra-custom")
KB_COLLECTION = os.environ.get("KB_COLLECTION", "kb")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")

HTTP_TIMEOUT = 120
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Astra — Airflow GenAI RAG")


# --------------------------------------------------------------------------- #
# Ollama + Qdrant helpers (same HTTP contracts as the DAGs)
# --------------------------------------------------------------------------- #
def ollama_embed(text: str) -> list[float]:
    r = requests.post(
        f"{OLLAMA_BASE_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": text},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    embeddings = r.json().get("embeddings") or []
    if not embeddings:
        raise RuntimeError("Ollama returned no embedding.")
    return embeddings[0]


def ollama_generate(model: str, prompt: str) -> str:
    r = requests.post(
        f"{OLLAMA_BASE_URL}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return (r.json().get("response") or "").strip()


def _qdrant_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if QDRANT_API_KEY:
        h["api-key"] = QDRANT_API_KEY
    return h


def qdrant_status() -> tuple[bool, bool]:
    """Return (reachable, has_kb_collection) so callers can distinguish a broken
    port-forward from a genuinely missing collection."""
    try:
        r = requests.get(
            f"{QDRANT_URL}/collections/{KB_COLLECTION}",
            headers=_qdrant_headers(),
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 404:
            return True, False
        r.raise_for_status()
        return True, True
    except Exception:
        return False, False


def qdrant_collection_ready() -> bool:
    return qdrant_status()[1]


def _require_qdrant_collection() -> None:
    """Raise a precise HTTP error if Qdrant is unreachable or kb is missing."""
    reachable, has_kb = qdrant_status()
    if not reachable:
        raise HTTPException(
            502,
            f"Qdrant unreachable at {QDRANT_URL} — is the port-forward up? "
            "(In the marketplace guide, Stop and re-Start the demo UI.)",
        )
    if not has_kb:
        raise HTTPException(
            409, f"Collection {KB_COLLECTION!r} not found — run the ingest DAG first."
        )


def qdrant_search(vector: list[float], top_k: int) -> list[dict]:
    r = requests.post(
        f"{QDRANT_URL}/collections/{KB_COLLECTION}/points/search",
        json={"vector": vector, "limit": top_k, "with_payload": True},
        headers=_qdrant_headers(),
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    hits = r.json().get("result") or []
    out = []
    for h in hits:
        payload = h.get("payload") or {}
        out.append(
            {
                "title": payload.get("title", ""),
                "source": payload.get("source", ""),
                "text": payload.get("text", ""),
                "score": h.get("score", 0.0),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# API models
# --------------------------------------------------------------------------- #
class GenerateReq(BaseModel):
    topic: str
    model: str | None = None
    top_k: int = 4


class SearchReq(BaseModel):
    query: str
    top_k: int = 4


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    ollama_ok = False
    qdrant_ok = False
    try:
        requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10).raise_for_status()
        ollama_ok = True
    except Exception:
        pass
    qdrant_ok = qdrant_status()[0]
    return {
        "ollama": ollama_ok,
        "qdrant": qdrant_ok,
        "collection": KB_COLLECTION,
        "collection_ready": qdrant_collection_ready(),
    }


@app.get("/api/models")
def models():
    try:
        r = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10)
        r.raise_for_status()
        names = [m["name"] for m in r.json().get("models", [])]
    except Exception as e:
        raise HTTPException(502, f"Ollama unreachable: {e}")
    return {"models": names, "default": GEN_MODEL}


@app.post("/api/search")
def search(req: SearchReq):
    _require_qdrant_collection()
    try:
        vector = ollama_embed(req.query)
        return {"sources": qdrant_search(vector, req.top_k)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/api/generate")
def generate(req: GenerateReq):
    if not req.topic.strip():
        raise HTTPException(400, "topic is required")
    _require_qdrant_collection()
    model = (req.model or GEN_MODEL).strip()
    try:
        vector = ollama_embed(req.topic)
        sources = qdrant_search(vector, req.top_k)
        context = "\n\n".join(
            f"[{i + 1}] {s['title']} ({s['source']}): {s['text']}"
            for i, s in enumerate(sources)
        )
        prompt = (
            "Use ONLY the facts in the context below to write a short, engaging "
            "social-media post (2-4 sentences) about the topic. Do not invent "
            "features that are not in the context.\n\n"
            f"Context:\n{context}\n\n"
            f"Topic: {req.topic}\n\nPost:"
        )
        post = ollama_generate(model, prompt)
        return {"post": post, "model": model, "sources": sources}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


# Serve the SUSE-styled static frontend at the root.
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
