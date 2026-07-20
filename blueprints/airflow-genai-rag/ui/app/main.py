"""
Astra — Airflow GenAI RAG demo UI (backend).

A small FastAPI app that mirrors the suse-vss UI style. It performs RAG generation
against the same in-cluster services the Airflow pipeline populates:

    embed the topic (Ollama nomic-embed-text)
      -> search Milvus `kb` for the closest chunks
      -> build a grounded prompt
      -> generate a post (Ollama, default the customized `astra-custom` model)

Run locally (with `kubectl port-forward` to ollama:11434 and milvus:19530):

    pip install -r requirements.txt
    uvicorn app.main:app --host 0.0.0.0 --port 8000
    open http://localhost:8000

Configuration (env):
    OLLAMA_BASE_URL  default http://localhost:11434
    MILVUS_URI       default http://localhost:19530
    EMBED_MODEL      default nomic-embed-text
    GEN_MODEL        default astra-custom
    KB_COLLECTION    default kb
    MILVUS_TOKEN     optional "user:password" bearer token
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
MILVUS_URI = os.environ.get("MILVUS_URI", "http://localhost:19530").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
GEN_MODEL = os.environ.get("GEN_MODEL", "astra-custom")
KB_COLLECTION = os.environ.get("KB_COLLECTION", "kb")
MILVUS_TOKEN = os.environ.get("MILVUS_TOKEN", "")

HTTP_TIMEOUT = 120
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Astra — Airflow GenAI RAG")


# --------------------------------------------------------------------------- #
# Ollama + Milvus helpers (same HTTP contracts as the DAGs)
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


def _milvus_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if MILVUS_TOKEN:
        h["Authorization"] = f"Bearer {MILVUS_TOKEN}"
    return h


def _milvus_post(path: str, body: dict) -> dict:
    r = requests.post(
        f"{MILVUS_URI}{path}", json=body, headers=_milvus_headers(), timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("code") not in (0, None):
        raise RuntimeError(f"Milvus error on {path}: {data}")
    return data


def milvus_collection_ready() -> bool:
    try:
        data = _milvus_post("/v2/vectordb/collections/list", {})
        return KB_COLLECTION in (data.get("data") or [])
    except Exception:
        return False


def milvus_search(vector: list[float], top_k: int) -> list[dict]:
    data = _milvus_post(
        "/v2/vectordb/entities/search",
        {
            "collectionName": KB_COLLECTION,
            "data": [vector],
            "limit": top_k,
            "outputFields": ["text", "title", "source"],
            "searchParams": {"metricType": "COSINE"},
        },
    )
    hits = data.get("data") or []
    out = []
    for h in hits:
        out.append(
            {
                "title": h.get("title", ""),
                "source": h.get("source", ""),
                "text": h.get("text", ""),
                "score": h.get("distance", h.get("score", 0.0)),
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
    milvus_ok = False
    try:
        requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10).raise_for_status()
        ollama_ok = True
    except Exception:
        pass
    try:
        _milvus_post("/v2/vectordb/collections/list", {})
        milvus_ok = True
    except Exception:
        pass
    return {
        "ollama": ollama_ok,
        "milvus": milvus_ok,
        "collection": KB_COLLECTION,
        "collection_ready": milvus_collection_ready(),
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
    if not milvus_collection_ready():
        raise HTTPException(
            409, f"Collection {KB_COLLECTION!r} not found — run the ingest DAG first."
        )
    try:
        vector = ollama_embed(req.query)
        return {"sources": milvus_search(vector, req.top_k)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/api/generate")
def generate(req: GenerateReq):
    if not req.topic.strip():
        raise HTTPException(400, "topic is required")
    if not milvus_collection_ready():
        raise HTTPException(
            409, f"Collection {KB_COLLECTION!r} not found — run the ingest DAG first."
        )
    model = (req.model or GEN_MODEL).strip()
    try:
        vector = ollama_embed(req.topic)
        sources = milvus_search(vector, req.top_k)
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
