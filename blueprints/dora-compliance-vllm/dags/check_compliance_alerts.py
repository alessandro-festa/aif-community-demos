"""
DAG: check_compliance_alerts

Compute the SLA-breach mart from the BaFin report: for every reportable (CRITICAL/MAJOR)
incident that has not been reported, work out how long until (or since) its BaFin deadline and
label it BREACHED (deadline already passed), IMMINENT (< 25% of the window remaining) or
ON_TRACK. This is what the reference pipeline's `check_compliance_alerts` DAG task does.
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import pg_exec, pg_query


@dag(
    dag_id="check_compliance_alerts",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["dora", "alerts"],
)
def check_compliance_alerts():
    @task
    def alerts() -> dict:
        now = pendulum.now("UTC")
        rows = pg_query(
            "SELECT incident_id, institution_id, dora_severity, deadline_hours, "
            "detection_ts, deadline_ts, reported FROM mart_bafin_report")
        if not rows:
            # Not an error — there may simply be no reportable incidents.
            pg_exec("DROP TABLE IF EXISTS mart_sla_breach CASCADE")
            pg_exec("""CREATE TABLE mart_sla_breach (
                incident_id TEXT PRIMARY KEY, institution_id TEXT, dora_severity TEXT,
                deadline_ts TIMESTAMPTZ, hours_remaining DOUBLE PRECISION, status TEXT)""")
            return {"breached": 0, "imminent": 0, "on_track": 0}

        out, counts = [], {"BREACHED": 0, "IMMINENT": 0, "ON_TRACK": 0}
        for (iid, inst, sev, hours, detected, deadline, reported) in rows:
            if reported or deadline is None:
                continue
            remaining = (deadline - now).total_seconds() / 3600.0
            if remaining < 0:
                status = "BREACHED"
            elif hours and remaining < 0.25 * hours:
                status = "IMMINENT"
            else:
                status = "ON_TRACK"
            counts[status] += 1
            out.append((iid, inst, sev, deadline, round(remaining, 2), status))

        pg_exec("DROP TABLE IF EXISTS mart_sla_breach CASCADE")
        pg_exec("""CREATE TABLE mart_sla_breach (
            incident_id TEXT PRIMARY KEY, institution_id TEXT, dora_severity TEXT,
            deadline_ts TIMESTAMPTZ, hours_remaining DOUBLE PRECISION, status TEXT)""")
        if out:
            from common import pg_insert_rows
            pg_insert_rows("mart_sla_breach",
                           ["incident_id", "institution_id", "dora_severity",
                            "deadline_ts", "hours_remaining", "status"], out)
        print(f"[alerts] {counts}", flush=True)
        return {"breached": counts["BREACHED"], "imminent": counts["IMMINENT"],
                "on_track": counts["ON_TRACK"]}

    alerts()


check_compliance_alerts()
