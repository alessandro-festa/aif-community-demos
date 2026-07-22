"""
DAG: clear_data

Reset utility — drops the DORA tables and the Milvus incidents collection so you can
regenerate from scratch. Trigger manually.
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import INCIDENTS_COLLECTION, milvus_drop_collection, pg_exec


@dag(
    dag_id="clear_data",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["dora", "utility"],
)
def clear_data():
    @task
    def clear() -> str:
        pg_exec("DROP TABLE IF EXISTS incidents, incidents_classified, "
                "mart_bafin_report, mart_vendor_risk, mart_sla_breach CASCADE")
        milvus_drop_collection(INCIDENTS_COLLECTION)
        return "cleared"

    clear()


clear_data()
