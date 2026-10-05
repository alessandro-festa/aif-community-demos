"""
DAG: clear_qdrant

Utility DAG that drops the Qdrant `kb` collection, so you can re-ingest the
knowledge base from scratch. Trigger manually from the Airflow UI.
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import KB_COLLECTION, qdrant_drop_collection


@dag(
    dag_id="clear_qdrant",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["genai-rag", "utility"],
)
def clear_qdrant():
    @task
    def drop() -> str:
        qdrant_drop_collection(KB_COLLECTION)
        return f"dropped collection {KB_COLLECTION}"

    drop()


clear_qdrant()
