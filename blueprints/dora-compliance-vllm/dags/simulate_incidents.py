"""
DAG: simulate_incidents

Generate a batch of synthetic ICT operational incidents (the DORA-Pipeline "incident
simulator", ported to a single Airflow task instead of Kafka+PySpark) and load them into
PostgreSQL. Each incident carries the impact metrics BaFin Article 18 classification needs:
clients affected %, financial impact €, incident type, cross-border flag and the ICT
third-party provider (if any).

`N_INCIDENTS` (default 600) controls the batch size. Detection timestamps are spread over
the last few days — some deliberately old enough that CRITICAL/MAJOR reporting deadlines have
already lapsed, so the SLA-breach mart and the compliance agent have something to find.
"""
from __future__ import annotations

import random
import uuid

import pendulum
from airflow.decorators import dag, task

from common import (INCIDENT_TYPES, INSTITUTION_TYPES, N_INCIDENTS, VENDORS,
                    pg_exec, pg_insert_rows)

SYSTEMS = ["core-banking", "payments-gateway", "mobile-app", "online-banking",
           "card-processing", "trading-platform", "kyc-service", "auth-service",
           "data-warehouse", "customer-portal"]


@dag(
    dag_id="simulate_incidents",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["dora", "simulate"],
)
def simulate_incidents():
    @task
    def generate() -> dict:
        rnd = random.Random(42)
        now = pendulum.now("UTC")
        vendors = list(VENDORS)

        pg_exec("DROP TABLE IF EXISTS incidents, incidents_classified, "
                "mart_bafin_report, mart_vendor_risk, mart_sla_breach CASCADE")
        pg_exec("""
            CREATE TABLE incidents (
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
              is_cross_border BOOLEAN,
              description TEXT)""")

        rows = []
        for _ in range(N_INCIDENTS):
            itype = rnd.choice(INCIDENT_TYPES)
            # Skew impact so most incidents are MINOR, with a realistic tail of MAJOR/CRITICAL.
            pct = round(min(100.0, rnd.gammavariate(1.4, 6.0)), 1)
            eur = round(rnd.gammavariate(1.3, 40_000.0), 2)
            # Third-party involvement is likely for third_party_failure / system_outage.
            provider = None
            if itype in ("third_party_failure", "system_outage") and rnd.random() < 0.7:
                provider = rnd.choice(vendors)
            elif rnd.random() < 0.15:
                provider = rnd.choice(vendors)
            cross = rnd.random() < 0.3

            detected = now.subtract(hours=rnd.randint(0, 120), minutes=rnd.randint(0, 59))
            occurred = detected.subtract(minutes=rnd.randint(5, 600))
            contained = None
            if rnd.random() < 0.6:
                contained = detected.add(hours=rnd.randint(1, 96))

            systems = ", ".join(rnd.sample(SYSTEMS, rnd.randint(1, 3)))
            desc = (f"{itype.replace('_', ' ').title()} affecting {systems}; "
                    f"~{pct:.0f}% of clients impacted, estimated €{eur:,.0f} loss"
                    + (f"; ICT provider {provider} involved" if provider else "")
                    + ("; cross-border exposure" if cross else "") + ".")

            rows.append((
                f"inc_{uuid.uuid4().hex[:12]}", occurred, f"inst_{rnd.randint(1, 40):03d}",
                rnd.choice(INSTITUTION_TYPES), itype, systems, pct, eur,
                detected, contained, provider, cross, desc,
            ))

        pg_insert_rows("incidents",
                       ["incident_id", "ts", "institution_id", "institution_type",
                        "incident_type", "affected_systems", "clients_affected_pct",
                        "financial_impact_eur", "detection_ts", "containment_ts",
                        "ict_third_party_provider", "is_cross_border", "description"],
                       rows)
        return {"incidents": len(rows)}

    generate()


simulate_incidents()
