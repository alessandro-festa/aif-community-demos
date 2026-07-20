"""
Shared helpers for the Airflow GenAI RAG DAGs.

Everything here talks to Ollama and Milvus over plain HTTP using `requests`
(shipped in the stock Apache Airflow image), so the DAGs need no extra pip
packages when delivered via git-sync.

Endpoints (in-cluster defaults, overridable via env on the Airflow pods):
  OLLAMA_BASE_URL   http://ollama:11434     Ollama REST API
  MILVUS_URI        http://milvus:19530     Milvus proxy REST v2 API
  EMBED_MODEL       nomic-embed-text        embedding model
  BASE_MODEL        llama3.2:1b             base chat model to customize from
  CUSTOM_MODEL      astra-custom            name of the created custom model
  MILVUS_TOKEN      (unset)                 optional "user:password" bearer token
  KB_COLLECTION     kb                      Milvus collection name
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
MILVUS_URI = os.environ.get("MILVUS_URI", "http://milvus:19530").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
BASE_MODEL = os.environ.get("BASE_MODEL", "llama3.2:1b")
CUSTOM_MODEL = os.environ.get("CUSTOM_MODEL", "astra-custom")
MILVUS_TOKEN = os.environ.get("MILVUS_TOKEN", "")
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
# Milvus (REST v2 API via the proxy)
# --------------------------------------------------------------------------- #
def _milvus_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if MILVUS_TOKEN:
        headers["Authorization"] = f"Bearer {MILVUS_TOKEN}"
    return headers


def _milvus_post(path: str, body: dict) -> dict:
    resp = requests.post(
        f"{MILVUS_URI}{path}",
        json=body,
        headers=_milvus_headers(),
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    # Milvus REST wraps errors in {"code": <non-zero>, "message": ...}.
    if isinstance(data, dict) and data.get("code") not in (0, None):
        raise RuntimeError(f"Milvus error on {path}: {data}")
    return data


def milvus_has_collection(name: str = KB_COLLECTION) -> bool:
    data = _milvus_post("/v2/vectordb/collections/list", {})
    return name in (data.get("data") or [])


def milvus_drop_collection(name: str = KB_COLLECTION) -> None:
    if milvus_has_collection(name):
        _milvus_post("/v2/vectordb/collections/drop", {"collectionName": name})


def milvus_create_collection(dim: int, name: str = KB_COLLECTION) -> None:
    """Create a KB collection with an explicit schema + a COSINE vector index."""
    body = {
        "collectionName": name,
        "schema": {
            "autoID": False,
            "enableDynamicField": True,
            "fields": [
                {"fieldName": "id", "dataType": "Int64", "isPrimary": True},
                {
                    "fieldName": "vector",
                    "dataType": "FloatVector",
                    "elementTypeParams": {"dim": dim},
                },
                {
                    "fieldName": "text",
                    "dataType": "VarChar",
                    "elementTypeParams": {"max_length": 8192},
                },
                {
                    "fieldName": "title",
                    "dataType": "VarChar",
                    "elementTypeParams": {"max_length": 512},
                },
                {
                    "fieldName": "source",
                    "dataType": "VarChar",
                    "elementTypeParams": {"max_length": 512},
                },
            ],
        },
        "indexParams": [
            {
                "fieldName": "vector",
                "metricType": "COSINE",
                "indexName": "vector_index",
                "indexType": "AUTOINDEX",
            }
        ],
    }
    _milvus_post("/v2/vectordb/collections/create", body)


def milvus_insert(rows: list[dict], name: str = KB_COLLECTION) -> None:
    """Insert rows (each: id, vector, text, title, source) in batches."""
    batch = 100
    for i in range(0, len(rows), batch):
        _milvus_post(
            "/v2/vectordb/entities/insert",
            {"collectionName": name, "data": rows[i : i + batch]},
        )


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
