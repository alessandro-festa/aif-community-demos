"""
Shared helpers for the Insurance Support Copilot DAGs.

The DAGs generate a synthetic insurance support dataset into PostgreSQL, embed the
ticket text into Milvus (semantic "similar case" index), and (Ollama variant) create
a customized support-agent persona model. Everything talks to Postgres via psycopg2
and to the embedding endpoint + Milvus over plain HTTP (`requests`) — all present in
the STOCK AppCo Airflow image (psycopg2 ships for Airflow's own Postgres metadata),
so no custom image is needed. Synthetic data is generated with the stdlib only.

Config (env, injected by the apache-airflow component):
  POSTGRES_URI     postgresql://insurance:insurance@support-db:5432/insurance
  MILVUS_URI       http://milvus:19530                 Milvus proxy REST v2 API
  EMBED_BASE_URL   http://ollama:11434/v1              OpenAI-compatible /embeddings
  EMBED_MODEL      nomic-embed-text
  CHAT_BASE_URL    http://ollama:11434/v1              OpenAI-compatible chat (persona base)
  BASE_MODEL       qwen2.5vl:7b                        base model to customize from (Ollama)
  CUSTOM_MODEL     support-agent                       created persona model tag
  N_TICKETS        400                                 synthetic support tickets to generate
  CASES_COLLECTION support_cases
"""
from __future__ import annotations

import os

import psycopg2
import psycopg2.extras
import requests

POSTGRES_URI = os.environ.get("POSTGRES_URI", "postgresql://insurance:insurance@support-db:5432/insurance")
MILVUS_URI = os.environ.get("MILVUS_URI", "http://milvus:19530").rstrip("/")
MILVUS_TOKEN = os.environ.get("MILVUS_TOKEN", "")
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "http://ollama:11434/v1").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
CHAT_BASE_URL = os.environ.get("CHAT_BASE_URL", "http://ollama:11434/v1").rstrip("/")
BASE_MODEL = os.environ.get("BASE_MODEL", "qwen2.5vl:7b")
CUSTOM_MODEL = os.environ.get("CUSTOM_MODEL", "support-agent")
def _int_env(name: str, default: int) -> int:
    """Parse an int env var, tolerating an unfilled '{{...}}' wizard placeholder."""
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


N_TICKETS = _int_env("N_TICKETS", 400)
CASES_COLLECTION = os.environ.get("CASES_COLLECTION", "support_cases")
HTTP_TIMEOUT = 120


# --------------------------------------------------------------------------- #
# PostgreSQL (raw psycopg2 — same pattern as the fraud blueprint)
# --------------------------------------------------------------------------- #
def pg_conn():
    return psycopg2.connect(POSTGRES_URI)


def pg_exec(sql: str, params=None):
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())


def pg_insert_rows(table: str, columns: list[str], rows: list[tuple]):
    if not rows:
        return
    cols = ", ".join(columns)
    with pg_conn() as conn, conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur, f"INSERT INTO {table} ({cols}) VALUES %s", rows, page_size=1000
        )


def pg_query(sql: str, params=None) -> list[tuple]:
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


# --------------------------------------------------------------------------- #
# Embeddings (OpenAI-compatible /v1/embeddings — Ollama and vLLM both expose it)
# --------------------------------------------------------------------------- #
def embed(text: str, model: str = EMBED_MODEL) -> list[float]:
    r = requests.post(
        f"{EMBED_BASE_URL}/embeddings",
        json={"model": model, "input": text},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json().get("data") or []
    if not data or "embedding" not in data[0]:
        raise RuntimeError(f"embeddings endpoint returned no vector for {model!r}: {r.text[:200]}")
    return data[0]["embedding"]


# --------------------------------------------------------------------------- #
# Ollama model customization (persona) — Ollama variant only
# --------------------------------------------------------------------------- #
def _ollama_root() -> str:
    return CHAT_BASE_URL[:-3].rstrip("/") if CHAT_BASE_URL.endswith("/v1") else CHAT_BASE_URL


def ollama_create_model(name: str, from_model: str, system: str, messages: list[dict]) -> None:
    payload = {"model": name, "from": from_model, "system": system, "stream": False}
    if messages:
        payload["messages"] = messages
    r = requests.post(f"{_ollama_root()}/api/create", json=payload, timeout=HTTP_TIMEOUT)
    r.raise_for_status()


# --------------------------------------------------------------------------- #
# Milvus (REST v2 API via the proxy — requests only)
# --------------------------------------------------------------------------- #
def _milvus_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if MILVUS_TOKEN:
        h["Authorization"] = f"Bearer {MILVUS_TOKEN}"
    return h


def _milvus_post(path: str, body: dict) -> dict:
    r = requests.post(f"{MILVUS_URI}{path}", json=body, headers=_milvus_headers(), timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("code") not in (0, None):
        raise RuntimeError(f"Milvus error on {path}: {data}")
    return data


def milvus_has_collection(name: str = CASES_COLLECTION) -> bool:
    return name in (_milvus_post("/v2/vectordb/collections/list", {}).get("data") or [])


def milvus_drop_collection(name: str = CASES_COLLECTION) -> None:
    if milvus_has_collection(name):
        _milvus_post("/v2/vectordb/collections/drop", {"collectionName": name})


def milvus_create_collection(dim: int, name: str = CASES_COLLECTION) -> None:
    """Create the support-cases collection: a COSINE vector + filterable metadata."""
    body = {
        "collectionName": name,
        "schema": {
            "autoID": False,
            "enableDynamicField": True,
            "fields": [
                {"fieldName": "id", "dataType": "Int64", "isPrimary": True},
                {"fieldName": "vector", "dataType": "FloatVector",
                 "elementTypeParams": {"dim": dim}},
                {"fieldName": "ticket_id", "dataType": "Int64"},
                {"fieldName": "subject", "dataType": "VarChar",
                 "elementTypeParams": {"max_length": 512}},
                {"fieldName": "body", "dataType": "VarChar",
                 "elementTypeParams": {"max_length": 8192}},
                {"fieldName": "accident_type", "dataType": "VarChar",
                 "elementTypeParams": {"max_length": 64}},
                {"fieldName": "product_type", "dataType": "VarChar",
                 "elementTypeParams": {"max_length": 32}},
                {"fieldName": "status", "dataType": "VarChar",
                 "elementTypeParams": {"max_length": 32}},
                {"fieldName": "was_paid", "dataType": "Int64"},
                {"fieldName": "within_policy", "dataType": "Int64"},
                {"fieldName": "resolution", "dataType": "VarChar",
                 "elementTypeParams": {"max_length": 4096}},
            ],
        },
        "indexParams": [
            {"fieldName": "vector", "metricType": "COSINE",
             "indexName": "vector_index", "indexType": "AUTOINDEX"}
        ],
    }
    _milvus_post("/v2/vectordb/collections/create", body)


def milvus_insert(rows: list[dict], name: str = CASES_COLLECTION) -> None:
    batch = 100
    for i in range(0, len(rows), batch):
        _milvus_post("/v2/vectordb/entities/insert",
                     {"collectionName": name, "data": rows[i:i + batch]})
