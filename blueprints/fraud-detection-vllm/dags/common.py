"""
Shared helpers for the fraud / AML detection DAGs.

The DAGs generate a synthetic fraud graph (SantanderAI/gen-fraud-graph), load it into
PostgreSQL, engineer graph/behavioural features, train an XGBoost classifier, and push
per-account feature vectors into Milvus for embedding-based anomaly detection.

These DAGs need real Python libraries (pandas, numpy, networkx, scikit-learn, xgboost,
imbalanced-learn, psycopg2, gen-fraud-graph). They are installed into the Airflow image at
start via the chart's `_PIP_ADDITIONAL_REQUIREMENTS` env (see the Blueprint CR).

Config (env, injected by the apache-airflow component):
  POSTGRES_URI   postgresql://fraud:fraud@fraud-db:5432/fraud
  MILVUS_URI     http://milvus:19530
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
MILVUS_URI = os.environ.get("MILVUS_URI", "http://milvus:19530").rstrip("/")
MILVUS_TOKEN = os.environ.get("MILVUS_TOKEN", "")
SCALE_FACTOR = float(os.environ.get("SCALE_FACTOR", "0.001"))
HIGH_VALUE = float(os.environ.get("HIGH_VALUE", "1000"))
ACCOUNTS_COLLECTION = os.environ.get("ACCOUNTS_COLLECTION", "accounts")
HTTP_TIMEOUT = 120

# Per-account feature vector (fixed order — used for both XGBoost and the Milvus vector).
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
# Milvus (REST v2 API via the proxy — requests only)
# --------------------------------------------------------------------------- #
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


def milvus_has_collection(name: str = ACCOUNTS_COLLECTION) -> bool:
    data = _milvus_post("/v2/vectordb/collections/list", {})
    return name in (data.get("data") or [])


def milvus_drop_collection(name: str = ACCOUNTS_COLLECTION) -> None:
    if milvus_has_collection(name):
        _milvus_post("/v2/vectordb/collections/drop", {"collectionName": name})


def milvus_create_collection(dim: int, name: str = ACCOUNTS_COLLECTION) -> None:
    body = {
        "collectionName": name,
        "schema": {
            "autoID": False,
            "enableDynamicField": True,
            "fields": [
                {"fieldName": "id", "dataType": "Int64", "isPrimary": True},
                {"fieldName": "vector", "dataType": "FloatVector",
                 "elementTypeParams": {"dim": dim}},
                {"fieldName": "account_id", "dataType": "VarChar",
                 "elementTypeParams": {"max_length": 64}},
                {"fieldName": "is_fraud", "dataType": "Int64"},
            ],
        },
        "indexParams": [
            {"fieldName": "vector", "metricType": "COSINE",
             "indexName": "vector_index", "indexType": "AUTOINDEX"}
        ],
    }
    _milvus_post("/v2/vectordb/collections/create", body)


def milvus_insert(rows: list[dict], name: str = ACCOUNTS_COLLECTION) -> None:
    batch = 200
    for i in range(0, len(rows), batch):
        _milvus_post("/v2/vectordb/entities/insert",
                     {"collectionName": name, "data": rows[i:i + batch]})


def milvus_search(vector: list[float], top_k: int, name: str = ACCOUNTS_COLLECTION) -> list[dict]:
    data = _milvus_post("/v2/vectordb/entities/search", {
        "collectionName": name,
        "data": [vector],
        "limit": top_k,
        "outputFields": ["account_id", "is_fraud"],
        "searchParams": {"metricType": "COSINE"},
    })
    return data.get("data") or []


def acc_num(account_id: str) -> int:
    """gen-fraud-graph account ids look like 'acc_123' -> 123."""
    try:
        return int(str(account_id).split("_")[-1])
    except ValueError:
        return abs(hash(account_id)) % (10**12)
