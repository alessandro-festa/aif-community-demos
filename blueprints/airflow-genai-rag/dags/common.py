"""
Shared helpers for the Airflow GenAI RAG DAGs.

Everything here talks to Ollama and Qdrant over plain HTTP using `requests`
(shipped in the stock Apache Airflow image), so the DAGs need no extra pip
packages when delivered via git-sync.

Endpoints (in-cluster defaults, overridable via env on the Airflow pods):
  OLLAMA_BASE_URL   http://ollama:11434     Ollama REST API
  QDRANT_URL        http://qdrant:6333      Qdrant REST API
  EMBED_MODEL       nomic-embed-text        embedding model
  BASE_MODEL        llama3.2:1b             base chat model to customize from
  CUSTOM_MODEL      astra-custom            name of the created custom model
  QDRANT_API_KEY    (unset)                 optional Qdrant API key
  KB_COLLECTION     kb                      Qdrant collection name
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
BASE_MODEL = os.environ.get("BASE_MODEL", "llama3.2:1b")
CUSTOM_MODEL = os.environ.get("CUSTOM_MODEL", "astra-custom")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
KB_COLLECTION = os.environ.get("KB_COLLECTION", "kb")

# The knowledge base + example posts ship alongside the DAGs in this repo.
# dags/ and include/ are siblings under blueprints/airflow-genai-rag/.
INCLUDE_DIR = Path(__file__).resolve().parent.parent / "include"
KB_DIR = INCLUDE_DIR / "knowledge_base"
EXAMPLES_DIR = INCLUDE_DIR / "examples"

HTTP_TIMEOUT = 120


# --------------------------------------------------------------------------- #
# Ollama
# --------------------------------------------------------------------------- #
def ollama_embed(text: str, model: str = EMBED_MODEL) -> list[float]:
    """Return the embedding vector for `text` using Ollama /api/embed."""
    resp = requests.post(
        f"{OLLAMA_BASE_URL}/api/embed",
        json={"model": model, "input": text},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    embeddings = data.get("embeddings") or []
    if not embeddings:
        raise RuntimeError(f"Ollama returned no embedding for model {model!r}: {data}")
    return embeddings[0]


def ollama_create_model(
    name: str,
    from_model: str,
    system: str,
    messages: list[dict],
) -> None:
    """
    Create/overwrite an Ollama model from a base model plus a system persona and
    few-shot example messages. This is the CPU-friendly, no-GPU analogue of the
    original use case's OpenAI hosted fine-tuning.
    """
    payload = {
        "model": name,
        "from": from_model,
        "system": system,
        "stream": False,
    }
    if messages:
        payload["messages"] = messages
    resp = requests.post(
        f"{OLLAMA_BASE_URL}/api/create", json=payload, timeout=HTTP_TIMEOUT
    )
    resp.raise_for_status()


def ollama_tags() -> list[str]:
    """List the model tags currently available on the Ollama server."""
    resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return [m["name"] for m in resp.json().get("models", [])]


# --------------------------------------------------------------------------- #
# Qdrant (REST API)
# --------------------------------------------------------------------------- #
def _qdrant_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if QDRANT_API_KEY:
        headers["api-key"] = QDRANT_API_KEY
    return headers


def qdrant_has_collection(name: str = KB_COLLECTION) -> bool:
    resp = requests.get(
        f"{QDRANT_URL}/collections/{name}",
        headers=_qdrant_headers(),
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return True


def qdrant_drop_collection(name: str = KB_COLLECTION) -> None:
    if qdrant_has_collection(name):
        resp = requests.delete(
            f"{QDRANT_URL}/collections/{name}",
            headers=_qdrant_headers(),
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()


def qdrant_create_collection(dim: int, name: str = KB_COLLECTION) -> None:
    """Create a KB collection sized for `dim`-length COSINE vectors. Qdrant
    collections are schemaless for payload fields (text/title/source), so no
    field-by-field schema is needed beyond the vector itself."""
    resp = requests.put(
        f"{QDRANT_URL}/collections/{name}",
        json={"vectors": {"size": dim, "distance": "Cosine"}},
        headers=_qdrant_headers(),
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()


def qdrant_insert(rows: list[dict], name: str = KB_COLLECTION) -> None:
    """Insert rows (each: id, vector, text, title, source) in batches."""
    batch = 100
    for i in range(0, len(rows), batch):
        points = [
            {
                "id": row["id"],
                "vector": row["vector"],
                "payload": {
                    k: v for k, v in row.items() if k not in ("id", "vector")
                },
            }
            for row in rows[i : i + batch]
        ]
        resp = requests.put(
            f"{QDRANT_URL}/collections/{name}/points",
            json={"points": points},
            headers=_qdrant_headers(),
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()


# --------------------------------------------------------------------------- #
# Text utilities
# --------------------------------------------------------------------------- #
def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    """Simple fixed-size character chunker with overlap (no LangChain needed)."""
    text = " ".join(text.split())
    if not text:
        return []
    chunks, start = [], 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        start = end - overlap
        if start <= 0:
            break
    return chunks
