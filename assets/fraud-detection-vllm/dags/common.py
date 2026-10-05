"""
Shared helpers for the fraud / AML detection DAGs.

The DAGs generate a synthetic fraud graph (SantanderAI/gen-fraud-graph), load it into
PostgreSQL, engineer graph/behavioural features, train an XGBoost classifier, and push
per-account feature vectors into Qdrant for embedding-based anomaly detection.

These DAGs need real Python libraries (pandas, numpy, networkx, scikit-learn, xgboost,
imbalanced-learn, psycopg2, gen-fraud-graph). They are installed into the Airflow image at
start via the chart's `_PIP_ADDITIONAL_REQUIREMENTS` env (see the Blueprint CR).

Config (env, injected by the apache-airflow component):
  POSTGRES_URI   postgresql://fraud:fraud@fraud-db:5432/fraud
  QDRANT_URL     http://qdrant:6333
  SCALE_FACTOR   0.001   (gen-fraud-graph scale; ~10k accounts / ~90k tx / ~10 rings)
  HIGH_VALUE     1000    (amount threshold for "suspicious" edges used in ring detection)
  ACCOUNTS_COLLECTION  accounts
"""
from __future__ import annotations

import os

import psycopg2
import psycopg2.extras
import requests

POSTGRES_URI = os.environ.get("POSTGRES_URI", "postgresql://fraud:fraud@fraud-db:5432/fraud")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333").rstrip("/")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
SCALE_FACTOR = float(os.environ.get("SCALE_FACTOR", "0.001"))
HIGH_VALUE = float(os.environ.get("HIGH_VALUE", "1000"))
ACCOUNTS_COLLECTION = os.environ.get("ACCOUNTS_COLLECTION", "accounts")
HTTP_TIMEOUT = 120

# Per-account feature vector (fixed order — used for both XGBoost and the Qdrant vector).
FEATURES = [
    "out_degree", "in_degree", "out_amount", "in_amount", "mean_amount",
    "max_amount", "high_value_edges", "in_cycle", "balance", "risk_score",
]
FEATURE_DIM = len(FEATURES)


# --------------------------------------------------------------------------- #
# PostgreSQL
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
# Qdrant (REST API — requests only)
# --------------------------------------------------------------------------- #
def _qdrant_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if QDRANT_API_KEY:
        h["api-key"] = QDRANT_API_KEY
    return h


def _qdrant_request(method: str, path: str, body: dict | None = None) -> requests.Response:
    return requests.request(
        method, f"{QDRANT_URL}{path}", json=body, headers=_qdrant_headers(), timeout=HTTP_TIMEOUT
    )


def qdrant_has_collection(name: str = ACCOUNTS_COLLECTION) -> bool:
    r = _qdrant_request("GET", f"/collections/{name}")
    if r.status_code == 404:
        return False
    r.raise_for_status()
    return True


def qdrant_drop_collection(name: str = ACCOUNTS_COLLECTION) -> None:
    if qdrant_has_collection(name):
        r = _qdrant_request("DELETE", f"/collections/{name}")
        r.raise_for_status()


def qdrant_create_collection(dim: int, name: str = ACCOUNTS_COLLECTION) -> None:
    body = {"vectors": {"size": dim, "distance": "Cosine"}}
    r = _qdrant_request("PUT", f"/collections/{name}", body)
    r.raise_for_status()


def qdrant_insert(rows: list[dict], name: str = ACCOUNTS_COLLECTION) -> None:
    batch = 200
    for i in range(0, len(rows), batch):
        points = [
            {
                "id": row["id"],
                "vector": row["vector"],
                "payload": {k: v for k, v in row.items() if k not in ("id", "vector")},
            }
            for row in rows[i:i + batch]
        ]
        r = _qdrant_request("PUT", f"/collections/{name}/points", {"points": points})
        r.raise_for_status()


def qdrant_search(vector: list[float], top_k: int, name: str = ACCOUNTS_COLLECTION) -> list[dict]:
    r = _qdrant_request("POST", f"/collections/{name}/points/search", {
        "vector": vector,
        "limit": top_k,
        "with_payload": True,
    })
    r.raise_for_status()
    hits = (r.json() or {}).get("result") or []
    return [{**(h.get("payload") or {}), "distance": h.get("score")} for h in hits]


def acc_num(account_id: str) -> int:
    """gen-fraud-graph account ids look like 'acc_123' -> 123."""
    try:
        return int(str(account_id).split("_")[-1])
    except ValueError:
        return abs(hash(account_id)) % (10**12)
