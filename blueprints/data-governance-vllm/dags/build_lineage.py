"""
DAG: build_lineage

Add data LINEAGE between the catalogued tables (architecture-as-lineage) so OpenMetadata's
graph + Impact Analysis work, and map DATA RISK by tagging tables with a Risk level derived
from their regulation/sensitivity tags. Run after seed_governance.

Flow modelled:
  gov_registry.business_registry ─▶ enterprise_dwh.core.customer ─▶ account ─▶ transaction
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import om_add_lineage, om_get, om_put, wait_for_openmetadata

ENT = "enterprise-postgres.enterprise_dwh.core"
REG = "govregistry-postgres.gov_registry.public"

# from_fqn -> to_fqn (data flows downstream left→right)
EDGES = [
    (f"{REG}.business_registry", f"{ENT}.customer"),
    (f"{ENT}.customer", f"{ENT}.account"),
    (f"{ENT}.account", f"{ENT}.transaction"),
]

# Data-risk map: table FQN -> risk level (High where PII/confidential + upstream breadth).
RISK = {
    f"{ENT}.customer": "High",       # PII master
    f"{REG}.business_registry": "High",
    f"{ENT}.account": "Medium",      # confidential balances
    f"{ENT}.transaction": "Medium",
}


def _add_table_tag(fqn: str, tag_fqn: str) -> None:
    """GET the table then re-PUT (createOrUpdate) with the extra table-level tag added,
    preserving its columns. Best-effort."""
    t = om_get(f"/tables/name/{fqn}?fields=columns,tags")
    tags = [{"tagFQN": x["tagFQN"]} for x in (t.get("tags") or [])]
    if tag_fqn not in [x["tagFQN"] for x in tags]:
        tags.append({"tagFQN": tag_fqn})
    schema_fqn = fqn.rsplit(".", 1)[0]
    cols = [{"name": c["name"], "dataType": c.get("dataType", "UNKNOWN"),
             "tags": [{"tagFQN": x["tagFQN"]} for x in (c.get("tags") or [])]}
            for c in (t.get("columns") or [])]
    om_put("/tables", {"name": fqn.rsplit(".", 1)[1], "databaseSchema": schema_fqn,
                       "columns": cols, "tags": tags})


@dag(dag_id="build_lineage", schedule=None,
     start_date=pendulum.datetime(2024, 1, 1, tz="UTC"), catchup=False,
     tags=["governance", "lineage"])
def build_lineage():

    @task
    def wait() -> None:
        wait_for_openmetadata()

    @task
    def add_edges() -> dict:
        for src, dst in EDGES:
            om_add_lineage("/tables", src, "/tables", dst)
        return {"edges": len(EDGES)}

    @task
    def map_risk() -> dict:
        # Risk classification + levels, then tag each table.
        om_put("/classifications", {"name": "Risk",
                "description": "Data-risk level derived from sensitivity + lineage breadth."})
        for level in ("High", "Medium", "Low"):
            om_put("/tags", {"classification": "Risk", "name": level,
                             "description": f"{level} data risk."})
        for fqn, level in RISK.items():
            try:
                _add_table_tag(fqn, f"Risk.{level}")
            except Exception as e:  # noqa: BLE001
                print(f"[warn] risk tag {fqn}: {e}")
        return {"tagged": len(RISK)}

    w = wait()
    w >> add_edges()
    w >> map_risk()


build_lineage()
