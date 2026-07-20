"""
DAG: clear_milvus

Utility DAG that drops the Milvus `kb` collection, so you can re-ingest the
knowledge base from scratch. Trigger manually from the Airflow UI.
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import KB_COLLECTION, milvus_drop_collection


@dag(
    dag_id="clear_milvus",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["genai-rag", "utility"],
)
def clear_milvus():
    @task
    def drop() -> str:
        milvus_drop_collection(KB_COLLECTION)
        return f"dropped collection {KB_COLLECTION}"

    drop()


clear_milvus()
