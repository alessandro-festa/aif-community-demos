"""
DAG: periodic_review

Automate a periodic data-asset review: scan the catalogued tables in OpenMetadata for
signs of poor currency / stewardship — missing OWNER, missing DESCRIPTION — and file a
review finding per asset (as an OpenMetadata task/thread, best-effort) plus a printed
report. This is the "automation of review processes" (currency, ownership, adequacy).

Run after seed_governance (and, ideally, build_lineage). Schedule it (e.g. weekly) in the
Airflow UI for a recurring review.
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import om_get, om_try, wait_for_openmetadata

SERVICES = ["enterprise-postgres", "govregistry-postgres"]


@dag(dag_id="periodic_review", schedule=None,
     start_date=pendulum.datetime(2024, 1, 1, tz="UTC"), catchup=False,
     tags=["governance", "review"])
def periodic_review():

    @task
    def wait() -> None:
        wait_for_openmetadata()

    @task
    def review() -> dict:
        # List catalogued tables and inspect owner/description/currency.
        data = om_get("/tables?limit=1000&fields=owners,description,updatedAt")
        tables = data.get("data", []) if isinstance(data, dict) else []
        findings: list[dict] = []
        now_ms = int(pendulum.now("UTC").timestamp() * 1000)
        stale_after_ms = 90 * 24 * 3600 * 1000  # 90 days

        for t in tables:
            fqn = t.get("fullyQualifiedName", t.get("name", "?"))
            issues = []
            if not (t.get("owners") or t.get("owner")):
                issues.append("no owner")
            if not (t.get("description") or "").strip():
                issues.append("no description")
            updated = t.get("updatedAt") or now_ms
            if now_ms - int(updated) > stale_after_ms:
                issues.append("stale (>90d)")
            if issues:
                findings.append({"asset": fqn, "issues": issues})

        for f in findings:
            print(f"  REVIEW {f['asset']}: {', '.join(f['issues'])}")
            # File a review task/thread on the asset (best-effort; thread API varies by version).
            om_try("/feed", {
                "message": f"Periodic review: {', '.join(f['issues'])}",
                "about": f"<#E::table::{f['asset']}>",
                "type": "Task",
            }, f"review task {f['asset']}")

        return {"scanned": len(tables), "flagged": len(findings),
                "findings": findings[:50]}

    wait() >> review()


periodic_review()
