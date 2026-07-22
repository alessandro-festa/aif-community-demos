"""
DAG: build_marts

Build the DORA compliance marts from `incidents_classified` (the lean analogue of the
reference pipeline's dbt marts + Great Expectations checks):

  * mart_bafin_report  — one row per reportable (CRITICAL/MAJOR) incident with its BaFin
                         deadline and a `reported` flag (all start unreported).
  * mart_vendor_risk   — per ICT third-party provider: incident counts by severity, total
                         financial impact and worst client-impact (ICT concentration risk).

Then run basic data-quality assertions (row counts, null / range checks). Failures are
logged loudly and raise — no silent pass (replaces Great Expectations).
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import pg_exec, pg_query


@dag(
    dag_id="build_marts",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["dora", "marts"],
)
def build_marts():
    @task
    def bafin_report() -> dict:
        pg_exec("DROP TABLE IF EXISTS mart_bafin_report CASCADE")
        pg_exec("""
            CREATE TABLE mart_bafin_report AS
            SELECT incident_id, institution_id, institution_type, incident_type,
                   ict_third_party_provider, dora_severity, clients_affected_pct,
                   financial_impact_eur, detection_ts, deadline_hours, deadline_ts,
                   classification_reason, FALSE AS reported
            FROM incidents_classified
            WHERE bafin_notification_required = TRUE
            ORDER BY deadline_ts ASC""")
        pg_exec("ALTER TABLE mart_bafin_report ADD PRIMARY KEY (incident_id)")
        n = pg_query("SELECT count(*) FROM mart_bafin_report")[0][0]
        return {"reportable": n}

    @task
    def vendor_risk() -> dict:
        pg_exec("DROP TABLE IF EXISTS mart_vendor_risk CASCADE")
        pg_exec("""
            CREATE TABLE mart_vendor_risk AS
            SELECT ict_third_party_provider AS provider,
                   MAX(provider_tier) AS provider_tier,
                   count(*) AS incidents,
                   count(*) FILTER (WHERE dora_severity='critical') AS critical,
                   count(*) FILTER (WHERE dora_severity='major') AS major,
                   round(sum(financial_impact_eur)::numeric, 2) AS total_impact_eur,
                   round(max(clients_affected_pct)::numeric, 1) AS max_clients_pct
            FROM incidents_classified
            WHERE ict_third_party_provider IS NOT NULL
            GROUP BY ict_third_party_provider
            ORDER BY critical DESC, major DESC, total_impact_eur DESC""")
        n = pg_query("SELECT count(*) FROM mart_vendor_risk")[0][0]
        return {"vendors": n}

    @task
    def quality_checks(_a: dict, _b: dict) -> str:
        failures = []
        total = pg_query("SELECT count(*) FROM incidents_classified")[0][0]
        if total == 0:
            failures.append("incidents_classified is empty")
        bad_sev = pg_query(
            "SELECT count(*) FROM incidents_classified "
            "WHERE dora_severity NOT IN ('critical','major','minor')")[0][0]
        if bad_sev:
            failures.append(f"{bad_sev} rows with invalid dora_severity")
        bad_pct = pg_query(
            "SELECT count(*) FROM incidents_classified "
            "WHERE clients_affected_pct < 0 OR clients_affected_pct > 100")[0][0]
        if bad_pct:
            failures.append(f"{bad_pct} rows with clients_affected_pct out of [0,100]")
        null_deadline = pg_query(
            "SELECT count(*) FROM mart_bafin_report WHERE deadline_ts IS NULL")[0][0]
        if null_deadline:
            failures.append(f"{null_deadline} reportable incidents with no deadline_ts")

        if failures:
            raise RuntimeError("data-quality checks FAILED: " + "; ".join(failures))
        print(f"[dq] OK — {total} classified incidents passed all checks", flush=True)
        return "ok"

    quality_checks(bafin_report(), vendor_risk())


build_marts()
