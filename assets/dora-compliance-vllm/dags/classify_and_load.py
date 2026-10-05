"""
DAG: classify_and_load

Apply the DORA / BaFin Article 18 rules engine (common.classify_incident) to every raw
incident, enrich it with the ICT third-party provider concentration tier, compute the BaFin
notification deadline timestamp (detection time + 4h for CRITICAL / 72h for MAJOR), and write
the enriched rows to `incidents_classified`. This is the lean analogue of the reference
pipeline's PySpark classifier + dbt staging.
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import VENDORS, classify_incident, pg_exec, pg_insert_rows, pg_query

RAW_COLS = ["incident_id", "ts", "institution_id", "institution_type", "incident_type",
            "affected_systems", "clients_affected_pct", "financial_impact_eur",
            "detection_ts", "containment_ts", "ict_third_party_provider",
            "is_cross_border", "description"]


@dag(
    dag_id="classify_and_load",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["dora", "classify"],
)
def classify_and_load():
    @task
    def classify() -> dict:
        raw = pg_query(f"SELECT {', '.join(RAW_COLS)} FROM incidents")
        if not raw:
            raise RuntimeError("no incidents — run simulate_incidents first")

        pg_exec("DROP TABLE IF EXISTS incidents_classified CASCADE")
        pg_exec("""
            CREATE TABLE incidents_classified (
              incident_id TEXT PRIMARY KEY,
              ts TIMESTAMPTZ,
              institution_id TEXT,
              institution_type TEXT,
              incident_type TEXT,
              affected_systems TEXT,
              clients_affected_pct DOUBLE PRECISION,
              financial_impact_eur DOUBLE PRECISION,
              detection_ts TIMESTAMPTZ,
              containment_ts TIMESTAMPTZ,
              ict_third_party_provider TEXT,
              provider_tier TEXT,
              is_cross_border BOOLEAN,
              description TEXT,
              dora_severity TEXT,
              bafin_notification_required BOOLEAN,
              deadline_hours INT,
              deadline_ts TIMESTAMPTZ,
              classification_reason TEXT)""")

        counts = {"critical": 0, "major": 0, "minor": 0}
        out = []
        for r in raw:
            inc = dict(zip(RAW_COLS, r))
            v = classify_incident(inc)
            counts[v["dora_severity"]] += 1
            hours = v["deadline_hours"]
            deadline_ts = None
            if hours is not None and inc["detection_ts"] is not None:
                deadline_ts = inc["detection_ts"] + pendulum.duration(hours=hours)
            provider_tier = VENDORS.get(inc["ict_third_party_provider"]) if inc["ict_third_party_provider"] else None
            out.append((
                inc["incident_id"], inc["ts"], inc["institution_id"], inc["institution_type"],
                inc["incident_type"], inc["affected_systems"], inc["clients_affected_pct"],
                inc["financial_impact_eur"], inc["detection_ts"], inc["containment_ts"],
                inc["ict_third_party_provider"], provider_tier, inc["is_cross_border"],
                inc["description"], v["dora_severity"], v["bafin_notification_required"],
                hours, deadline_ts, v["reason"],
            ))

        pg_insert_rows("incidents_classified",
                       ["incident_id", "ts", "institution_id", "institution_type",
                        "incident_type", "affected_systems", "clients_affected_pct",
                        "financial_impact_eur", "detection_ts", "containment_ts",
                        "ict_third_party_provider", "provider_tier", "is_cross_border",
                        "description", "dora_severity", "bafin_notification_required",
                        "deadline_hours", "deadline_ts", "classification_reason"],
                       out)
        return {"classified": len(out), **counts}

    classify()


classify_and_load()
