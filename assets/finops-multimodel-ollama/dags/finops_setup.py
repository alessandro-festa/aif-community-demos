"""
DAG: finops_setup

Create the per-team **virtual keys** and **budgets** in LiteLLM that make the
chargeback / budget FinOps use cases work. For each team it creates (or reuses) a
team with a monthly budget, then generates a virtual key scoped to that team.

LiteLLM's /key/generate RETURNS a generated key value (you can't supply your own —
that returns 403). The key values are therefore captured here and stored in the
Airflow Variable `finops_team_keys` (JSON: {team_alias: {key, team_id, budget,
use_case}}) so the generate_traffic DAG can use them. The local chat UI doesn't need
them — it uses the master key plus a team tag for attribution.

Requires the litellm component's `store_model_in_db: true` (set in the Blueprint CR).
Run this before generate_traffic.
"""
from __future__ import annotations

import json

import pendulum
from airflow.decorators import dag, task

from common import (
    LITELLM_BASE_URL,
    TEAMS,
    litellm_post,
    wait_for_litellm,
    _admin_headers,  # noqa: PLC2701 - internal helper reuse within the DAG package
)

try:  # Airflow 3 task SDK, with a fallback for older imports.
    from airflow.sdk import Variable
except ImportError:  # pragma: no cover
    from airflow.models import Variable

TEAM_KEYS_VAR = "finops_team_keys"


@dag(
    dag_id="finops_setup",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["finops", "setup"],
)
def finops_setup():
    @task
    def wait() -> None:
        wait_for_litellm()

    @task
    def create_teams_and_keys() -> dict:
        import requests

        def team_id_by_alias(alias: str) -> str | None:
            r = requests.get(
                f"{LITELLM_BASE_URL}/team/list", headers=_admin_headers(), timeout=60
            )
            if not r.ok:
                return None
            for t in r.json() or []:
                info = t.get("team_info", t) if isinstance(t, dict) else {}
                if (t.get("team_alias") or info.get("team_alias")) == alias:
                    return t.get("team_id") or info.get("team_id")
            return None

        mapping: dict[str, dict] = {}
        # Unique-ish suffix so key_alias never collides on re-runs (a fresh key each run).
        stamp = pendulum.now("UTC").int_timestamp
        for team_alias, _legacy_key, budget, use_case, _weight in TEAMS:
            # Team (reuse if it already exists), with a monthly budget for chargeback.
            team_id = team_id_by_alias(team_alias)
            if not team_id:
                resp = litellm_post(
                    "/team/new",
                    {"team_alias": team_alias, "max_budget": budget, "budget_duration": "30d"},
                    ok_conflict=True,
                )
                team_id = resp.get("team_id") or team_id_by_alias(team_alias)

            # Virtual key scoped to the team. LiteLLM generates + returns the value.
            keyresp = litellm_post(
                "/key/generate",
                {
                    "key_alias": f"{team_alias}-key-{stamp}",
                    "team_id": team_id,
                    "max_budget": budget,
                    "budget_duration": "30d",
                    "metadata": {"team": team_alias, "use_case": use_case},
                },
            )
            mapping[team_alias] = {
                "key": keyresp.get("key"),
                "team_id": team_id,
                "budget": budget,
                "use_case": use_case,
            }

        # Persist for generate_traffic (contains secrets — Variable, not returned/logged).
        Variable.set(TEAM_KEYS_VAR, json.dumps(mapping))
        # Return a redacted summary for the task log.
        return {t: {"team_id": v["team_id"], "budget": v["budget"], "has_key": bool(v["key"])}
                for t, v in mapping.items()}

    wait() >> create_teams_and_keys()


finops_setup()
