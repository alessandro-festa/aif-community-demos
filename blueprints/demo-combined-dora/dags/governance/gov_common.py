"""
Shared helpers for the Data Governance Copilot DAGs.

The DAGs SEED and drive OpenMetadata (the Apache-2.0 governance platform) through its
REST API and compute data-quality results over PostgreSQL sample data. OpenMetadata then
provides the catalog, glossary, lineage + impact analysis, data-quality suites,
roles/policies and alerting; a local LLM copilot reads it back.

Design note: OpenMetadata's own ingestion Airflow is DISABLED
(pipelineServiceClientConfig.enabled=false) — this stock AppCo Airflow pushes metadata
directly via REST, so the DAGs need only `requests` + `psycopg2` (both already in the
image). No custom OpenMetadata image, no second Airflow.

Config (env, injected by the apache-airflow component):
  OM_BASE_URL       http://openmetadata:8585
  OM_USER           admin@open-metadata.org
  OM_PASSWORD       admin
  PGHOST / PGPORT   gov-db / 5432
  PG_ADMIN_USER     postgres
  PG_ADMIN_PASSWORD GovAdmin-Demo-1
"""
from __future__ import annotations

import base64
import os

import psycopg2
import psycopg2.extras
import requests

OM_BASE_URL = os.environ.get("OM_BASE_URL", "http://openmetadata:8585").rstrip("/")
OM_USER = os.environ.get("OM_USER", "admin@open-metadata.org")
OM_PASSWORD = os.environ.get("OM_PASSWORD", "admin")

PGHOST = os.environ.get("PGHOST", "gov-db")
PGPORT = int(os.environ.get("PGPORT", "5432"))
PG_ADMIN_USER = os.environ.get("PG_ADMIN_USER", "postgres")
PG_ADMIN_PASSWORD = os.environ.get("PG_ADMIN_PASSWORD", "GovAdmin-Demo-1")

HTTP_TIMEOUT = 120

# The two sample data sources the DAGs create + catalog.
ENTERPRISE_DB = "enterprise_dwh"          # the "enterprise data warehouse"
REGISTRY_DB = "gov_registry"              # the "public administration" external DB

# The OpenMetadata database SERVICE names the catalog is organised under.
ENTERPRISE_SERVICE = "enterprise-postgres"
REGISTRY_SERVICE = "govregistry-postgres"


# --------------------------------------------------------------------------- #
# OpenMetadata REST client (basic-auth login -> JWT, then Bearer on /api/v1)
# --------------------------------------------------------------------------- #
_om_token: dict = {"value": None}


def _om_login() -> str:
    if _om_token["value"]:
        return _om_token["value"]
    # OpenMetadata basic auth: password is base64-encoded in the login payload.
    pw = base64.b64encode(OM_PASSWORD.encode()).decode()
    r = requests.post(
        f"{OM_BASE_URL}/api/v1/users/login",
        json={"email": OM_USER, "password": pw},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    tok = (r.json() or {}).get("accessToken", "")
    if not tok:
        raise RuntimeError(f"OpenMetadata login returned no accessToken: {r.text[:300]}")
    _om_token["value"] = tok
    return tok


def om_request(method: str, path: str, body: dict | None = None,
               ok_status: tuple = (200, 201)) -> dict:
    """Call the OpenMetadata API, refreshing the JWT once on 401. Raises with the
    response body on error so the real OM message shows up in the Airflow logs."""
    def _call():
        headers = {"Authorization": f"Bearer {_om_login()}",
                   "Content-Type": "application/json"}
        return requests.request(method, f"{OM_BASE_URL}/api/v1{path}",
                                headers=headers, json=body, timeout=HTTP_TIMEOUT)
    r = _call()
    if r.status_code == 401:  # token expired — refresh once
        _om_token["value"] = None
        r = _call()
    if r.status_code not in ok_status:
        raise RuntimeError(f"OM {method} {path} -> HTTP {r.status_code}: {r.text[:600]}")
    return r.json() if r.content else {}


def om_put(path: str, body: dict) -> dict:
    """createOrUpdate (idempotent) — OpenMetadata PUT upserts by fully-qualified name."""
    return om_request("PUT", path, body)


def om_get(path: str) -> dict:
    return om_request("GET", path, ok_status=(200,))


def om_try(path: str, body: dict, what: str) -> dict | None:
    """Best-effort PUT that logs and swallows errors — used for version-sensitive
    endpoints (data quality / alerts) so the pipeline still completes on older/newer OM."""
    try:
        return om_put(path, body)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {what}: {e}")
        return None


def om_entity_ref(path: str, fqn: str) -> dict:
    """Return an {id, type} entity reference for use in lineage edges, resolving the id
    from the fully-qualified name. `path` e.g. '/tables', type inferred from it."""
    ent = om_get(f"{path}/name/{fqn}")
    etype = path.strip("/").rstrip("s")  # /tables -> table
    return {"id": ent["id"], "type": etype}


def om_add_lineage(from_path: str, from_fqn: str, to_path: str, to_fqn: str) -> None:
    om_put("/lineage", {"edge": {
        "fromEntity": om_entity_ref(from_path, from_fqn),
        "toEntity": om_entity_ref(to_path, to_fqn),
    }})


# --------------------------------------------------------------------------- #
# PostgreSQL (superuser) — create + seed the sample source databases
# --------------------------------------------------------------------------- #
def pg_admin_conn(dbname: str = "postgres"):
    return psycopg2.connect(host=PGHOST, port=PGPORT, user=PG_ADMIN_USER,
                            password=PG_ADMIN_PASSWORD, dbname=dbname)


def pg_ensure_database(name: str) -> None:
    """CREATE DATABASE <name> if it doesn't already exist (autocommit)."""
    conn = pg_admin_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()


def pg_exec(dbname: str, sql: str, params=None) -> None:
    with pg_admin_conn(dbname) as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())
        conn.commit()


def pg_insert(dbname: str, table: str, columns: list[str], rows: list[tuple]) -> None:
    if not rows:
        return
    cols = ", ".join(columns)
    with pg_admin_conn(dbname) as conn, conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur, f"INSERT INTO {table} ({cols}) VALUES %s", rows, page_size=1000)
        conn.commit()


def pg_query(dbname: str, sql: str, params=None) -> list[tuple]:
    with pg_admin_conn(dbname) as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def wait_for_openmetadata(retries: int = 60, delay: int = 10) -> None:
    """Block until OpenMetadata answers /health-check (server + DB migrations ready)."""
    import time
    last = None
    for _ in range(retries):
        try:
            r = requests.get(f"{OM_BASE_URL}/api/v1/system/version", timeout=10)
            if r.ok:
                return
            last = r.status_code
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(delay)
    raise RuntimeError(f"OpenMetadata not ready at {OM_BASE_URL} (last={last})")
