"""
DAG: seed_governance

Populate OpenMetadata with a realistic governance baseline and seed the PostgreSQL
sample sources it describes:

  * two database SERVICES + databases: `enterprise_dwh` (enterprise data warehouse) and
    `gov_registry` (a "public administration" external registry) — external-DB integration;
  * schemas + tables with columns, owners and REGULATION tags — the catalog / master data;
  * a business GLOSSARY with reference-data terms;
  * CLASSIFICATIONS for internal & external regulations (GDPR, DORA, BaFin, PII, …);
  * TEAMS, ROLES and POLICIES — the access matrices (data permissions / democratization).

Idempotent (OpenMetadata PUT upserts by name). Run first.
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import (
    ENTERPRISE_DB, ENTERPRISE_SERVICE, REGISTRY_DB, REGISTRY_SERVICE,
    om_put, om_try, pg_ensure_database, pg_exec, pg_insert,
    wait_for_openmetadata,
)

# Sample catalog: service -> database -> schema -> [ (table, [(col, type, tags)]) ]
CATALOG = {
    ENTERPRISE_SERVICE: {
        "db": ENTERPRISE_DB, "schema": "core",
        "tables": {
            "customer": [
                ("customer_id", "BIGINT", []),
                ("full_name", "VARCHAR", ["Regulation.PII"]),
                ("national_id", "VARCHAR", ["Regulation.PII", "Regulation.GDPR"]),
                ("email", "VARCHAR", ["Regulation.PII"]),
                ("segment", "VARCHAR", []),
            ],
            "account": [
                ("account_id", "BIGINT", []),
                ("customer_id", "BIGINT", []),
                ("currency", "VARCHAR", []),
                ("balance_eur", "NUMERIC", ["Regulation.Confidential"]),
                ("opened_at", "TIMESTAMP", []),
            ],
            "transaction": [
                ("txn_id", "BIGINT", []),
                ("account_id", "BIGINT", []),
                ("amount_eur", "NUMERIC", ["Regulation.Confidential"]),
                ("txn_ts", "TIMESTAMP", []),
                ("counterparty_country", "VARCHAR", []),
            ],
        },
    },
    REGISTRY_SERVICE: {
        "db": REGISTRY_DB, "schema": "public",
        "tables": {
            "business_registry": [
                ("reg_no", "VARCHAR", []),
                ("legal_name", "VARCHAR", []),
                ("national_id", "VARCHAR", ["Regulation.PII", "Regulation.GDPR"]),
                ("status", "VARCHAR", []),
                ("registered_on", "DATE", []),
            ],
        },
    },
}

# Regulations / classifications to seed (classification -> [tags]).
CLASSIFICATIONS = {
    "Regulation": ["GDPR", "DORA", "BaFin", "PII", "Confidential", "Internal-Policy"],
}

# Business glossary (reference / master data vocabulary).
GLOSSARY = "EnterpriseGlossary"
GLOSSARY_TERMS = {
    "Customer": "A natural or legal person holding one or more accounts.",
    "Account": "A financial account owned by a customer; the master record for balances.",
    "PII": "Personally Identifiable Information; must be masked/blocked per GDPR.",
    "Reference-Currency": "ISO-4217 currency code used as reference data across systems.",
    "ICT-Provider": "A third-party ICT service provider tracked for DORA concentration risk.",
}

# Access matrices: policies -> roles -> teams.
POLICIES = {
    "DataConsumer-Policy": [
        {"name": "view-all", "resources": ["All"], "operations": ["ViewAll"], "effect": "allow"},
    ],
    "DataSteward-Policy": [
        {"name": "edit-metadata", "resources": ["All"],
         "operations": ["ViewAll", "EditDescription", "EditTags", "EditOwners"], "effect": "allow"},
    ],
}
ROLES = {
    "DataConsumer": ["DataConsumer-Policy"],
    "DataSteward": ["DataSteward-Policy"],
}
TEAMS = {
    "Finance": "Department",
    "DataPlatform": "Department",
    "PublicSector": "Department",
}


# OpenMetadata requires dataLength for these column types; add a sensible default.
_NEEDS_LENGTH = {"VARCHAR", "CHAR", "BINARY", "VARBINARY"}


def _column(name: str, dtype: str, tags: list[str]) -> dict:
    col = {"name": name, "dataType": dtype,
           "tags": [{"tagFQN": t} for t in tags]}
    if dtype in _NEEDS_LENGTH:
        col["dataLength"] = 256
    return col


@dag(dag_id="seed_governance", schedule=None,
     start_date=pendulum.datetime(2024, 1, 1, tz="UTC"), catchup=False,
     tags=["governance", "seed"])
def seed_governance():

    @task
    def wait() -> None:
        wait_for_openmetadata()

    @task
    def seed_postgres() -> dict:
        """Create + seed the two sample source databases (superuser)."""
        # enterprise_dwh
        pg_ensure_database(ENTERPRISE_DB)
        pg_exec(ENTERPRISE_DB, """
            CREATE SCHEMA IF NOT EXISTS core;
            CREATE TABLE IF NOT EXISTS core.customer(
              customer_id BIGINT PRIMARY KEY, full_name VARCHAR, national_id VARCHAR,
              email VARCHAR, segment VARCHAR);
            CREATE TABLE IF NOT EXISTS core.account(
              account_id BIGINT PRIMARY KEY, customer_id BIGINT, currency VARCHAR,
              balance_eur NUMERIC, opened_at TIMESTAMP);
            CREATE TABLE IF NOT EXISTS core.transaction(
              txn_id BIGINT PRIMARY KEY, account_id BIGINT, amount_eur NUMERIC,
              txn_ts TIMESTAMP, counterparty_country VARCHAR);
            TRUNCATE core.customer, core.account, core.transaction;
        """)
        custs = [(i, f"Customer {i}", f"ID{100000 + i}",
                  (f"user{i}@example.com" if i % 7 else None),  # some NULL emails (DQ)
                  ("retail" if i % 3 else "corporate")) for i in range(1, 201)]
        pg_insert(ENTERPRISE_DB, "core.customer",
                  ["customer_id", "full_name", "national_id", "email", "segment"], custs)
        accts = [(i, (i % 200) + 1, ("EUR" if i % 4 else "XXX"),  # some invalid currency (DQ)
                  round(((-1) ** i) * (i * 12.5), 2),             # some negative balances (DQ)
                  "2023-01-01") for i in range(1, 301)]
        pg_insert(ENTERPRISE_DB, "core.account",
                  ["account_id", "customer_id", "currency", "balance_eur", "opened_at"], accts)
        txns = [(i, (i % 300) + 1, round(i * 3.3, 2), "2024-06-01",
                 ("DE" if i % 5 else "US")) for i in range(1, 501)]
        pg_insert(ENTERPRISE_DB, "core.transaction",
                  ["txn_id", "account_id", "amount_eur", "txn_ts", "counterparty_country"], txns)

        # gov_registry (public-administration external DB)
        pg_ensure_database(REGISTRY_DB)
        pg_exec(REGISTRY_DB, """
            CREATE TABLE IF NOT EXISTS public.business_registry(
              reg_no VARCHAR PRIMARY KEY, legal_name VARCHAR, national_id VARCHAR,
              status VARCHAR, registered_on DATE);
            TRUNCATE public.business_registry;
        """)
        reg = [(f"REG{200000 + i}", f"Legal Entity {i}", f"ID{100000 + i}",
                ("active" if i % 6 else "dissolved"), "2015-05-05") for i in range(1, 151)]
        pg_insert(REGISTRY_DB, "public.business_registry",
                  ["reg_no", "legal_name", "national_id", "status", "registered_on"], reg)
        return {"enterprise_rows": 200 + 300 + 500, "registry_rows": 150}

    @task
    def seed_classifications() -> None:
        for cls, tags in CLASSIFICATIONS.items():
            om_put("/classifications", {"name": cls,
                    "description": f"{cls} tags (internal & external regulations)."})
            for t in tags:
                om_put("/tags", {"classification": cls, "name": t,
                        "description": f"{t} regulation / sensitivity tag."})

    @task
    def seed_glossary() -> None:
        om_put("/glossaries", {"name": GLOSSARY, "displayName": "Enterprise Glossary",
                "description": "Business glossary — reference & master-data vocabulary."})
        for term, desc in GLOSSARY_TERMS.items():
            om_put("/glossaryTerms", {"glossary": GLOSSARY, "name": term, "description": desc})

    @task
    def seed_catalog() -> dict:
        n_tables = 0
        for service, spec in CATALOG.items():
            om_put("/services/databaseServices", {
                "name": service, "serviceType": "Postgres",
                "connection": {"config": {
                    "type": "Postgres", "hostPort": "gov-db:5432",
                    "username": "openmetadata_user",
                    "authType": {"password": "Governance-Demo-1"},
                    "database": spec["db"],
                }},
            })
            db_fqn = f"{service}.{spec['db']}"
            om_put("/databases", {"name": spec["db"], "service": service})
            schema_fqn = f"{db_fqn}.{spec['schema']}"
            om_put("/databaseSchemas", {"name": spec["schema"], "database": db_fqn})
            for table, cols in spec["tables"].items():
                om_put("/tables", {
                    "name": table, "databaseSchema": schema_fqn,
                    "columns": [_column(c, dt, tags) for c, dt, tags in cols],
                })
                n_tables += 1
        return {"tables": n_tables}

    @task
    def seed_access() -> None:
        for name, rules in POLICIES.items():
            om_put("/policies", {"name": name, "description": f"{name} (access matrix).",
                                 "rules": rules})
        for name, policies in ROLES.items():
            om_put("/roles", {"name": name, "description": f"{name} role.", "policies": policies})
        for name, ttype in TEAMS.items():
            # teamType Department requires an Organization parent in OM; om_try tolerates
            # version differences in the team hierarchy rules.
            om_try("/teams", {"name": name, "displayName": name, "teamType": ttype},
                   f"team {name}")

    w = wait()
    w >> seed_postgres()
    w >> seed_classifications() >> seed_glossary() >> seed_catalog() >> seed_access()


seed_governance()
