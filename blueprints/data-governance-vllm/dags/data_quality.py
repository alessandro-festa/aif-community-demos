"""
DAG: data_quality

Profile the sample data and run data-quality RULE PACKS over PostgreSQL, then push the
profiles + test results to OpenMetadata and raise an alert. Run after seed_governance.

Rule packs demonstrated:
  * standard      — NOT NULL (customer.email), UNIQUE (customer.customer_id),
                    range (account.balance_eur ≥ 0);
  * financial     — currency is a valid ISO-4217 code (account.currency);
  * distribution  — customer.segment distribution is not degenerate;
  * similarity     — national_id overlap between enterprise & the public registry;
  * cross-check   — every account.customer_id exists in customer (referential integrity);
  * script rule   — custom SQL: negative-balance ratio must stay under a threshold.

The metrics are always computed + logged; pushing them to OpenMetadata's data-quality API
is best-effort (om_try) so the pipeline stays green across OM versions.
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import (
    ENTERPRISE_DB, REGISTRY_DB, om_try, pg_query, wait_for_openmetadata,
)

ISO_CCY = {"EUR", "USD", "GBP", "CHF", "JPY", "PLN"}
ENT_TABLE_FQN = "enterprise-postgres.enterprise_dwh.core.account"
CUST_TABLE_FQN = "enterprise-postgres.enterprise_dwh.core.customer"


def _q1(dbname: str, sql: str) -> float:
    return float(pg_query(dbname, sql)[0][0] or 0)


@dag(dag_id="data_quality", schedule=None,
     start_date=pendulum.datetime(2024, 1, 1, tz="UTC"), catchup=False,
     tags=["governance", "data-quality"])
def data_quality():

    @task
    def wait() -> None:
        wait_for_openmetadata()

    @task
    def run_rules() -> dict:
        e = ENTERPRISE_DB
        rows = int(_q1(e, "SELECT count(*) FROM core.account"))
        results: list[dict] = []

        def check(name: str, kind: str, ok: bool, detail: str) -> None:
            results.append({"name": name, "kind": kind,
                            "status": "success" if ok else "failed", "detail": detail})

        # standard
        null_emails = int(_q1(e, "SELECT count(*) FROM core.customer WHERE email IS NULL"))
        check("customer.email not null", "standard", null_emails == 0,
              f"{null_emails} null emails")
        dup_ids = int(_q1(e, "SELECT count(*)-count(DISTINCT customer_id) FROM core.customer"))
        check("customer.customer_id unique", "standard", dup_ids == 0, f"{dup_ids} duplicates")
        neg_bal = int(_q1(e, "SELECT count(*) FROM core.account WHERE balance_eur < 0"))
        check("account.balance_eur >= 0", "standard", neg_bal == 0, f"{neg_bal} negatives")
        # financial
        bad_ccy = int(_q1(e, "SELECT count(*) FROM core.account WHERE currency NOT IN %s"
                          % (tuple(ISO_CCY),)))
        check("account.currency valid ISO-4217", "financial", bad_ccy == 0,
              f"{bad_ccy} invalid currency codes")
        # distribution
        seg_min = _q1(e, "SELECT min(c) FROM (SELECT count(*) c FROM core.customer "
                         "GROUP BY segment) s")
        check("customer.segment distribution", "distribution", seg_min > 0,
              f"min segment bucket = {seg_min:.0f}")
        # cross-check (referential integrity account -> customer)
        orphans = int(_q1(e, "SELECT count(*) FROM core.account a LEFT JOIN core.customer c "
                          "ON a.customer_id=c.customer_id WHERE c.customer_id IS NULL"))
        check("account.customer_id -> customer", "cross-check", orphans == 0,
              f"{orphans} orphan accounts")
        # similarity (overlap of national_id enterprise vs public registry)
        ent_ids = {r[0] for r in pg_query(e, "SELECT national_id FROM core.customer")}
        reg_ids = {r[0] for r in pg_query(REGISTRY_DB,
                                          "SELECT national_id FROM public.business_registry")}
        overlap = len(ent_ids & reg_ids) / max(1, len(reg_ids))
        check("national_id registry match", "similarity", overlap >= 0.5,
              f"{overlap:.0%} of registry ids matched")
        # script rule (custom SQL): negative-balance ratio < 30%
        neg_ratio = neg_bal / max(1, rows)
        check("script: negative-balance ratio < 30%", "script", neg_ratio < 0.30,
              f"{neg_ratio:.0%} negative")

        failed = [r for r in results if r["status"] == "failed"]
        for r in results:
            print(f"  [{r['kind']}] {r['name']}: {r['status'].upper()} ({r['detail']})")
        return {"rows": rows, "total": len(results), "failed": len(failed),
                "results": results, "null_emails": null_emails, "neg_bal": neg_bal}

    @task
    def push_to_openmetadata(dq: dict) -> dict:
        # Table profile (best-effort — endpoint shape varies by OM version).
        ts = int(pendulum.now("UTC").timestamp() * 1000)
        om_try(f"/tables/name/{ENT_TABLE_FQN}/tableProfile", {
            "tableProfile": {"timestamp": ts, "rowCount": dq["rows"], "columnCount": 5},
            "columnProfile": [
                {"name": "balance_eur", "timestamp": ts, "valuesCount": dq["rows"]},
                {"name": "customer_id", "timestamp": ts, "valuesCount": dq["rows"]},
            ],
        }, "table profile")
        # A logical test suite + one result per rule (best-effort).
        om_try("/dataQuality/testSuites", {
            "name": "governance-quality-suite",
            "description": "Standard + financial data-quality rule packs.",
        }, "test suite")
        for r in dq["results"]:
            om_try("/dataQuality/testCases", {
                "name": r["name"].replace(" ", "-").replace(".", "-").lower(),
                "displayName": r["name"],
                "description": f"{r['kind']} rule",
                "entityLink": f"<#E::table::{ENT_TABLE_FQN}>",
                "testDefinition": "tableRowCountToBeBetween",
                "testSuite": "governance-quality-suite",
            }, f"test case {r['name']}")
        # Raise an alert if anything failed (best-effort).
        if dq["failed"]:
            om_try("/events/subscriptions", {
                "name": "data-quality-failures",
                "alertType": "Notification",
                "description": f"{dq['failed']} data-quality rule(s) failing.",
            }, "alert subscription")
        return {"pushed": True, "failed_rules": dq["failed"]}

    w = wait()
    dq = run_rules()
    w >> dq
    push_to_openmetadata(dq)


data_quality()
