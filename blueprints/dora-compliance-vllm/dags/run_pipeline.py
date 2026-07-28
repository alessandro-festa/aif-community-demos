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
from airflow.decorators import task
from airflow.models import DagModel
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator

STAGES = ["simulate_incidents", "classify_and_load", "build_marts",
          "index_incidents", "check_compliance_alerts"]


@task
def unpause(dag_id: str) -> None:
    # git-sync resets is_paused=True on every DAG-bag refresh, and TriggerDagRunOperator
    # doesn't unpause its target — without this the triggered run sits queued forever.
    dm = DagModel.get_dagmodel(dag_id)
    if dm is not None:
        dm.set_is_paused(False)


with DAG(
    dag_id="run_pipeline",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["dora", "pipeline", "orchestrator"],
) as dag:
    prev = None
    for stage in STAGES:
        unpause_step = unpause.override(task_id=f"unpause_{stage}")(stage)
        step = TriggerDagRunOperator(
            task_id=f"run_{stage}",
            trigger_dag_id=stage,
            wait_for_completion=True,   # block until the stage finishes before the next
            poke_interval=15,
            reset_dag_run=True,         # allow re-runs of run_pipeline
            allowed_states=["success"],
            failed_states=["failed"],
        )
        unpause_step >> step
        if prev:
            prev >> unpause_step
        prev = step
