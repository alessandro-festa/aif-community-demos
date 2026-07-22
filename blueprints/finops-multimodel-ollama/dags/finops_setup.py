"""
DAG: finops_setup

Create the per-team **virtual keys** and **budgets** in LiteLLM that make the
chargeback / budget FinOps use cases work. Idempotent: re-running reuses an existing
team (matched by alias) and ignores keys that already exist.

Teams, fixed key values and budgets are defined in common.TEAMS. The fixed key
values mean the traffic DAG (and the local chat UI) can use them directly with no
state to pass around.

Requires the litellm component's `store_model_in_db: true` (set in the Blueprint CR).
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import (
    LITELLM_BASE_URL,
    TEAMS,
    litellm_post,
    wait_for_litellm,
    _admin_headers,  # noqa: PLC2701 - internal helper reuse within the DAG package
)


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

        created = {}
        for team_alias, key, budget, use_case, _weight in TEAMS:
            # Team (reuse if it already exists).
            team_id = team_id_by_alias(team_alias)
            if not team_id:
                resp = litellm_post(
                    "/team/new",
                    {"team_alias": team_alias, "max_budget": budget, "budget_duration": "30d"},
                    ok_conflict=True,
                )
                team_id = resp.get("team_id") or team_id_by_alias(team_alias)

            # Virtual key with a FIXED value, scoped to the team + a chargeback tag.
            litellm_post(
                "/key/generate",
                {
                    "key": key,
                    "key_alias": f"{team_alias}-key",
                    "team_id": team_id,
                    "max_budget": budget,
                    "budget_duration": "30d",
                    "metadata": {"team": team_alias, "use_case": use_case},
                    "tags": [f"team:{team_alias}", f"use_case:{use_case}"],
                },
                ok_conflict=True,
            )
            created[team_alias] = {"team_id": team_id, "budget": budget, "use_case": use_case}

        return created

    wait() >> create_teams_and_keys()  # wait for the proxy, then create teams/keys


finops_setup()
