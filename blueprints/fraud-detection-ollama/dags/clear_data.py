"""
DAG: clear_data

Reset utility — drops the fraud tables and the Milvus accounts collection so you can
regenerate from scratch. Trigger manually.
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import ACCOUNTS_COLLECTION, milvus_drop_collection, pg_exec


@dag(
    dag_id="clear_data",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["fraud", "utility"],
)
def clear_data():
    @task
    def clear() -> str:
        pg_exec("DROP TABLE IF EXISTS accounts, transactions, fraud_cases, "
                "account_scores, flagged_accounts, model_metrics CASCADE")
        milvus_drop_collection(ACCOUNTS_COLLECTION)
        return "cleared"

    clear()


clear_data()
