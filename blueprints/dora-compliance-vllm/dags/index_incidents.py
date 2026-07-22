"""
DAG: index_incidents

Embed every classified incident and (re)load the vectors into Milvus so the compliance
agent in the UI can do semantic search ("find incidents like a cyber attack on the payments
gateway"). Embeddings use a lightweight deterministic hashing embedding (common.hash_embed) —
no extra model to pull, and identical on the CPU (Ollama) and GPU (vLLM) variants so search
behaves the same everywhere.
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import (EMBED_DIM, INCIDENTS_COLLECTION, hash_embed,
                    milvus_create_collection, milvus_drop_collection, milvus_insert,
                    pg_query)


@dag(
    dag_id="index_incidents",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["dora", "index"],
)
def index_incidents():
    @task
    def index() -> dict:
        rows = pg_query(
            "SELECT incident_id, dora_severity, incident_type, affected_systems, "
            "clients_affected_pct, financial_impact_eur, ict_third_party_provider, "
            "classification_reason, description FROM incidents_classified")
        if not rows:
            raise RuntimeError("no incidents_classified — run classify_and_load first")

        milvus_drop_collection(INCIDENTS_COLLECTION)
        milvus_create_collection(EMBED_DIM, INCIDENTS_COLLECTION)

        data = []
        for i, r in enumerate(rows):
            (iid, sev, itype, systems, pct, eur, provider, reason, desc) = r
            text = (f"{itype} | severity {sev} | systems {systems} | "
                    f"{pct}% clients | €{eur} | provider {provider or 'none'} | "
                    f"{reason} | {desc}")
            data.append({
                "id": i,
                "vector": hash_embed(text),
                "incident_id": iid,
                "dora_severity": sev or "",
                "incident_type": itype or "",
                "text": text[:4096],
            })
        milvus_insert(data, INCIDENTS_COLLECTION)
        return {"indexed": len(data)}

    index()


index_incidents()
