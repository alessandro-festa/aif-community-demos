"""
Shared helpers for the Insurance Support Copilot DAGs.

The DAGs generate a synthetic insurance support dataset into PostgreSQL, embed the
ticket text into Qdrant (semantic "similar case" index), and (Ollama variant) create
a customized support-agent persona model. Everything talks to Postgres via psycopg2
and to the embedding endpoint + Qdrant over plain HTTP (`requests`) — all present in
the STOCK AppCo Airflow image (psycopg2 ships for Airflow's own Postgres metadata),
so no custom image is needed. Synthetic data is generated with the stdlib only.

Config (env, injected by the apache-airflow component):
  POSTGRES_URI     postgresql://insurance:insurance@support-db:5432/insurance
  QDRANT_URL       http://qdrant:6333                  Qdrant REST API
  EMBED_BASE_URL   http://ollama:11434/v1              OpenAI-compatible /embeddings
  EMBED_MODEL      nomic-embed-text
  CHAT_BASE_URL    http://ollama:11434/v1              OpenAI-compatible chat (persona base)
  BASE_MODEL       qwen2.5vl:3b                        base model to customize from (Ollama)
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
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333").rstrip("/")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "http://ollama:11434/v1").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
CHAT_BASE_URL = os.environ.get("CHAT_BASE_URL", "http://ollama:11434/v1").rstrip("/")
BASE_MODEL = os.environ.get("BASE_MODEL", "qwen2.5vl:3b")
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


def ollama_tags() -> list[str]:
    """List model tags on the Ollama server (also used as a readiness probe)."""
    r = requests.get(f"{_ollama_root()}/api/tags", timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return [m["name"] for m in r.json().get("models", [])]


# --------------------------------------------------------------------------- #
# Qdrant (plain REST API — requests only)
# --------------------------------------------------------------------------- #
def _qdrant_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if QDRANT_API_KEY:
        h["api-key"] = QDRANT_API_KEY
    return h


def qdrant_has_collection(name: str = CASES_COLLECTION) -> bool:
    r = requests.get(f"{QDRANT_URL}/collections/{name}", headers=_qdrant_headers(), timeout=HTTP_TIMEOUT)
    return r.status_code == 200


def qdrant_drop_collection(name: str = CASES_COLLECTION) -> None:
    if qdrant_has_collection(name):
        r = requests.delete(f"{QDRANT_URL}/collections/{name}", headers=_qdrant_headers(), timeout=HTTP_TIMEOUT)
        r.raise_for_status()


def qdrant_create_collection(dim: int, name: str = CASES_COLLECTION) -> None:
    """Create the support-cases collection: a COSINE vector. Qdrant collections are
    schemaless for payload data, so the filterable metadata (ticket_id, subject, body,
    accident_type, product_type, status, was_paid, within_policy, resolution) simply
    rides along in each point's payload — no field/type declarations needed."""
    r = requests.put(
        f"{QDRANT_URL}/collections/{name}",
        json={"vectors": {"size": dim, "distance": "Cosine"}},
        headers=_qdrant_headers(), timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()


def qdrant_insert(rows: list[dict], name: str = CASES_COLLECTION) -> None:
    batch = 100
    for i in range(0, len(rows), batch):
        points = [
            {"id": row["id"], "vector": row["vector"],
             "payload": {k: v for k, v in row.items() if k not in ("id", "vector")}}
            for row in rows[i:i + batch]
        ]
        r = requests.put(
            f"{QDRANT_URL}/collections/{name}/points",
            json={"points": points}, headers=_qdrant_headers(), timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
