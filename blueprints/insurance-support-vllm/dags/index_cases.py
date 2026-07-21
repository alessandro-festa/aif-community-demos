"""
DAG: index_cases

Build the "similar case" semantic index. Reads resolved/closed support tickets from
Postgres, embeds each (subject + body) via the OpenAI-compatible /v1/embeddings
endpoint, and upserts them into the Milvus `support_cases` collection with filterable
metadata. The chat UI queries this index and REDACTS each hit (Presidio) before
showing it. Re-runnable: drops and recreates the collection.

Only resolved/closed tickets are indexed — those are the useful precedents (they
carry a decision + resolution). Trigger after generate_dataset.
"""
from __future__ import annotations

import time
from datetime import timedelta

import pendulum
from airflow.decorators import dag, task

from common import (
    CASES_COLLECTION,
    embed,
    milvus_create_collection,
    milvus_drop_collection,
    milvus_insert,
    pg_query,
)


def _wait_for_embeddings(timeout_s: int = 1800, interval_s: int = 15):
    """Block until the embedding endpoint answers (Ollama/vLLM can take a while to
    pull the model on first start). Raises if still unreachable after timeout."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            if embed("readiness probe"):
                return
        except Exception as e:  # noqa: BLE001 — endpoint not up / model not pulled yet
            last = e
        print("[index_cases] waiting for the embedding endpoint…", flush=True)
        time.sleep(interval_s)
    raise RuntimeError(f"embedding endpoint not ready after {timeout_s}s: {last}")


@dag(
    dag_id="index_cases",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["insurance-support", "rag", "index"],
)
def index_cases():
    @task(retries=3, retry_delay=timedelta(minutes=3))
    def build_index() -> dict:
        _wait_for_embeddings()  # tolerate a slow first-start model pull
        rows = pg_query("""
            SELECT t.ticket_id, t.subject, t.body, t.status,
                   COALESCE(c.accident_type, ''), COALESCE(p.product_type, ''),
                   COALESCE(c.was_paid, false), COALESCE(c.within_policy, false),
                   COALESCE(t.resolution_notes, '')
            FROM support_tickets t
            LEFT JOIN claims c   ON c.claim_id  = t.claim_id
            LEFT JOIN policies p ON p.policy_id = t.policy_id
            WHERE t.status IN ('resolved', 'closed')
        """)
        if not rows:
            raise RuntimeError("no resolved/closed tickets found — run generate_dataset first")

        # Determine embedding dimension from the first row, then (re)create the collection.
        first_vec = embed(f"{rows[0][1]}\n{rows[0][2]}")
        milvus_drop_collection(CASES_COLLECTION)
        milvus_create_collection(len(first_vec), CASES_COLLECTION)

        out = []
        for i, (tid, subject, body, status, acc, product, paid, within, res) in enumerate(rows):
            vec = first_vec if i == 0 else embed(f"{subject}\n{body}")
            out.append({
                "id": int(tid),
                "vector": vec,
                "ticket_id": int(tid),
                "subject": (subject or "")[:512],
                "body": (body or "")[:8192],
                "accident_type": (acc or "")[:64],
                "product_type": (product or "")[:32],
                "status": (status or "")[:32],
                "was_paid": 1 if paid else 0,
                "within_policy": 1 if within else 0,
                "resolution": (res or "")[:4096],
            })
        milvus_insert(out, CASES_COLLECTION)
        return {"indexed": len(out), "dim": len(first_vec), "collection": CASES_COLLECTION}

    build_index()


index_cases()
