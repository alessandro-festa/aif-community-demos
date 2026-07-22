"""
DAG: run_pipeline

One-click orchestrator — runs the whole DORA pipeline in the CORRECT order by triggering each
stage DAG and waiting for it to finish before starting the next:

  simulate_incidents -> classify_and_load -> build_marts -> index_incidents -> check_compliance_alerts

The stage DAGs have data dependencies (classify needs simulate's tables, marts need the
classification, etc.), so they must not run concurrently. This is what the compliance agent's
"run the whole pipeline" triggers, so a single trigger runs everything correctly in the
background.
"""
from __future__ import annotations

import pendulum
from airflow import DAG
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator

STAGES = ["simulate_incidents", "classify_and_load", "build_marts",
          "index_incidents", "check_compliance_alerts"]

with DAG(
    dag_id="run_pipeline",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["dora", "pipeline", "orchestrator"],
) as dag:
    prev = None
    for stage in STAGES:
        step = TriggerDagRunOperator(
            task_id=f"run_{stage}",
            trigger_dag_id=stage,
            wait_for_completion=True,   # block until the stage finishes before the next
            poke_interval=15,
            reset_dag_run=True,         # allow re-runs of run_pipeline
            allowed_states=["success"],
            failed_states=["failed"],
        )
        if prev:
            prev >> step
        prev = step
