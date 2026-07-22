"""
Shared helpers for the DORA Compliance Analysis DAGs.

The DAGs simulate synthetic ICT operational incidents, classify each one against the
EU DORA / BaFin Article 18 thresholds (CRITICAL / MAJOR / MINOR + notification deadline),
build compliance marts in PostgreSQL, index the incidents in Milvus for semantic search,
and raise SLA-breach alerts. A local SUSE-styled UI then lets an LLM *explain* incidents and
an LLM *agent* search the data (through the same Airflow Postgres connection) and drive the
pipeline via the Airflow REST API.

Faithful to Chirag-Kathuria-009/DORA-Pipeline's classifier rules; the heavy streaming/
lakehouse plumbing (Kafka/Spark/Iceberg/dbt/Great Expectations/Superset) is replaced by
Airflow Python tasks + PostgreSQL + Milvus + a local FastAPI UI — the all-SUSE-AppCo pattern.

Config (env, injected by the apache-airflow component):
  POSTGRES_URI   postgresql://dora:dora@dora-db:5432/dora
  MILVUS_URI     http://milvus:19530
  N_INCIDENTS    600     (how many synthetic incidents to simulate)
  EMBED_DIM      256     (deterministic hashing-embedding size for semantic search)
  INCIDENTS_COLLECTION  incidents
"""
from __future__ import annotations

import hashlib
import math
import os
import re

import psycopg2
import psycopg2.extras
import requests

POSTGRES_URI = os.environ.get("POSTGRES_URI", "postgresql://dora:dora@dora-db:5432/dora")
MILVUS_URI = os.environ.get("MILVUS_URI", "http://milvus:19530").rstrip("/")
MILVUS_TOKEN = os.environ.get("MILVUS_TOKEN", "")
N_INCIDENTS = int(os.environ.get("N_INCIDENTS", "600"))
EMBED_DIM = int(os.environ.get("EMBED_DIM", "256"))
INCIDENTS_COLLECTION = os.environ.get("INCIDENTS_COLLECTION", "incidents")
HTTP_TIMEOUT = 120

# ICT third-party providers the incidents may involve, with a coarse concentration tier.
# "critical" providers carry more systemic (concentration) risk under DORA.
VENDORS = {
    "AWS": "critical", "Microsoft Azure": "critical", "Google Cloud": "critical",
    "Cloudflare": "important", "Akamai": "important", "Temenos": "important",
    "FIS": "important", "Finastra": "important", "Stripe": "standard",
    "Twilio": "standard", "SendGrid": "standard", "Datadog": "standard",
}

INSTITUTION_TYPES = ["bank", "insurer", "payment_provider", "asset_manager"]
INCIDENT_TYPES = ["system_outage", "data_breach", "third_party_failure",
                  "cyber_attack", "transaction_failure", "authentication_failure"]


# --------------------------------------------------------------------------- #
# DORA / BaFin Article 18 classifier (ported from DORA-Pipeline, pure functions)
# --------------------------------------------------------------------------- #
# Frozen thresholds — keep these in one place so the unit test and the DAG agree.
CRITICAL_CLIENT_PCT = 25.0
CRITICAL_FINANCIAL_EUR = 1_000_000.0
CRITICAL_CYBER_CLIENT_PCT = 10.0
CRITICAL_CROSS_BORDER_CLIENT_PCT = 10.0
MAJOR_CLIENT_PCT = 10.0
MAJOR_FINANCIAL_EUR = 100_000.0

DEADLINE_HOURS = {"critical": 4, "major": 72, "minor": None}


def classify_incident(inc: dict) -> dict:
    """Classify one incident dict against the BaFin Article 18 thresholds.

    Returns {dora_severity, bafin_notification_required, deadline_hours, reason}.
    `inc` keys used: clients_affected_pct, financial_impact_eur, incident_type,
    is_cross_border, ict_third_party_provider.
    """
    pct = float(inc.get("clients_affected_pct") or 0.0)
    eur = float(inc.get("financial_impact_eur") or 0.0)
    itype = inc.get("incident_type") or ""
    cross = bool(inc.get("is_cross_border"))
    provider = inc.get("ict_third_party_provider")
    is_cyber = itype == "cyber_attack"

    severity, reason = "minor", ""
    # CRITICAL — any of:
    if pct >= CRITICAL_CLIENT_PCT:
        severity, reason = "critical", f"clients affected {pct:.0f}% ≥ {CRITICAL_CLIENT_PCT:.0f}%"
    elif eur >= CRITICAL_FINANCIAL_EUR:
        severity, reason = "critical", f"financial impact €{eur:,.0f} ≥ €{CRITICAL_FINANCIAL_EUR:,.0f}"
    elif is_cyber and pct >= CRITICAL_CYBER_CLIENT_PCT:
        severity, reason = "critical", f"cyber attack with {pct:.0f}% clients ≥ {CRITICAL_CYBER_CLIENT_PCT:.0f}%"
    elif cross and pct >= CRITICAL_CROSS_BORDER_CLIENT_PCT:
        severity, reason = "critical", f"cross-border with {pct:.0f}% clients ≥ {CRITICAL_CROSS_BORDER_CLIENT_PCT:.0f}%"
    # MAJOR — if not critical, any of:
    elif pct >= MAJOR_CLIENT_PCT:
        severity, reason = "major", f"clients affected {pct:.0f}% ≥ {MAJOR_CLIENT_PCT:.0f}%"
    elif eur >= MAJOR_FINANCIAL_EUR:
        severity, reason = "major", f"financial impact €{eur:,.0f} ≥ €{MAJOR_FINANCIAL_EUR:,.0f}"
    elif provider and itype == "system_outage":
        severity, reason = "major", f"third-party ({provider}) system outage"

    if severity == "minor":
        reason = "no BaFin reporting threshold met — internal logging only"

    hours = DEADLINE_HOURS[severity]
    return {
        "dora_severity": severity,
        "bafin_notification_required": severity in ("critical", "major"),
        "deadline_hours": hours,
        "reason": f"{severity.upper()}: {reason}",
    }


# --------------------------------------------------------------------------- #
# Deterministic hashing embedding (no extra model — identical on CPU and GPU)
# --------------------------------------------------------------------------- #
def hash_embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Bag-of-words hashing embedding, L2-normalised. Cheap, dependency-free and
    identical in the DAG (indexing) and the UI (querying) so COSINE search matches."""
    vec = [0.0] * dim
    for tok in re.findall(r"[a-z0-9]+", (text or "").lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


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


def milvus_has_collection(name: str = INCIDENTS_COLLECTION) -> bool:
    data = _milvus_post("/v2/vectordb/collections/list", {})
    return name in (data.get("data") or [])


def milvus_drop_collection(name: str = INCIDENTS_COLLECTION) -> None:
    if milvus_has_collection(name):
        _milvus_post("/v2/vectordb/collections/drop", {"collectionName": name})


def milvus_create_collection(dim: int = EMBED_DIM, name: str = INCIDENTS_COLLECTION) -> None:
    body = {
        "collectionName": name,
        "schema": {
            "autoID": False,
            "enableDynamicField": True,
            "fields": [
                {"fieldName": "id", "dataType": "Int64", "isPrimary": True},
                {"fieldName": "vector", "dataType": "FloatVector",
                 "elementTypeParams": {"dim": dim}},
                {"fieldName": "incident_id", "dataType": "VarChar",
                 "elementTypeParams": {"max_length": 64}},
                {"fieldName": "dora_severity", "dataType": "VarChar",
                 "elementTypeParams": {"max_length": 16}},
                {"fieldName": "incident_type", "dataType": "VarChar",
                 "elementTypeParams": {"max_length": 32}},
                {"fieldName": "text", "dataType": "VarChar",
                 "elementTypeParams": {"max_length": 4096}},
            ],
        },
        "indexParams": [
            {"fieldName": "vector", "metricType": "COSINE",
             "indexName": "vector_index", "indexType": "AUTOINDEX"}
        ],
    }
    _milvus_post("/v2/vectordb/collections/create", body)


def milvus_insert(rows: list[dict], name: str = INCIDENTS_COLLECTION) -> None:
    batch = 200
    for i in range(0, len(rows), batch):
        _milvus_post("/v2/vectordb/entities/insert",
                     {"collectionName": name, "data": rows[i:i + batch]})
