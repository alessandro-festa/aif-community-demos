"""
DAG: generate_traffic

Fire synthetic chat traffic at the LiteLLM gateway so the FinOps signals populate
with REAL token consumption. Each request:
  * uses a per-team virtual key (weighted by common.TEAMS) — spend is attributed to
    that team / key;
  * targets one of the three models (weighted by common.MODELS) — different prices;
  * carries a chargeback tag (team + use_case) via the x-litellm-tags header;
  * varies prompt length so token counts (and cost) vary.

LiteLLM writes each call to its spend logs; the litellm-exporter turns those into
Prometheus metrics, which Grafana visualises. Some requests may be blocked by the
guardrails (PII / prompt-injection) or rejected once a team's budget is exhausted —
both are expected and counted.

Run finops_setup first (it creates the virtual keys this DAG uses).
Set TRAFFIC_REQUESTS to change the volume; schedule the DAG to build an ongoing trend.
"""
from __future__ import annotations

import json
import random

import pendulum
import requests
from airflow.decorators import dag, task

from common import (
    LITELLM_BASE_URL,
    LITELLM_MASTER_KEY,
    MODELS,
    TEAMS,
    TRAFFIC_REQUESTS,
    HTTP_TIMEOUT,
    wait_for_litellm,
)

try:  # Airflow 3 task SDK, with a fallback for older imports.
    from airflow.sdk import Variable
except ImportError:  # pragma: no cover
    from airflow.models import Variable

TEAM_KEYS_VAR = "finops_team_keys"

# Prompt pools keyed by use_case, plus a few long prompts to create token variety.
PROMPTS = {
    "code-assist": [
        "Write a Python function that reverses a linked list.",
        "Explain the difference between a process and a thread.",
        "Refactor this loop into a list comprehension: for x in items: out.append(x*2)",
    ],
    "analysis": [
        "Summarise the key drivers of customer churn in two sentences.",
        "What statistical test compares means of two independent groups?",
        "Explain p-values to a non-technical stakeholder.",
    ],
    "copywriting": [
        "Write a punchy one-line tagline for a cloud cost tool.",
        "Draft a 2-sentence product announcement for a new AI gateway.",
        "Give me three subject lines for a webinar invite.",
    ],
    "helpdesk": [
        "How do I reset my password?",
        "The dashboard is slow to load, what should I check first?",
        "Explain what a 502 error means to an end user.",
    ],
}
LONG_SUFFIX = (
    " Please answer thoroughly, step by step, with examples, trade-offs, and a short "
    "conclusion summarising the recommendation for a production environment."
)


def _weighted(items, weight_index):
    r = random.random() * sum(i[weight_index] for i in items)
    upto = 0.0
    for it in items:
        upto += it[weight_index]
        if upto >= r:
            return it
    return items[-1]


@dag(
    dag_id="generate_traffic",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["finops", "traffic"],
)
def generate_traffic():
    @task
    def wait() -> None:
        wait_for_litellm()

    @task
    def fire() -> dict:
        # Per-team virtual keys created by finops_setup. If missing, fall back to the
        # master key (spend still tagged, but not attributed to a team) and warn.
        team_keys = json.loads(Variable.get(TEAM_KEYS_VAR, default="{}"))
        used_fallback = not team_keys

        sent, blocked, errors = 0, 0, 0
        total_cost, total_tokens = 0.0, 0
        by_model: dict[str, int] = {}
        by_team: dict[str, float] = {}

        for _ in range(TRAFFIC_REQUESTS):
            team_alias, _legacy_key, _budget, use_case, _tw = _weighted(TEAMS, 4)
            entry = team_keys.get(team_alias) or {}
            key = entry.get("key") or LITELLM_MASTER_KEY
            model_name, _in, _out, _mw = _weighted(MODELS, 3)
            prompt = random.choice(PROMPTS[use_case])
            if random.random() < 0.35:  # ~1/3 are long (more tokens → more cost)
                prompt += LONG_SUFFIX

            try:
                r = requests.post(
                    f"{LITELLM_BASE_URL}/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        "x-litellm-tags": f"team:{team_alias},use_case:{use_case}",
                    },
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 256,
                        "temperature": 0.7,
                    },
                    timeout=HTTP_TIMEOUT,
                )
            except Exception:  # noqa: BLE001
                errors += 1
                continue

            if r.status_code == 200:
                sent += 1
                cost = float(r.headers.get("x-litellm-response-cost", 0) or 0)
                total_cost += cost
                by_team[team_alias] = by_team.get(team_alias, 0.0) + cost
                by_model[model_name] = by_model.get(model_name, 0) + 1
                usage = (r.json() or {}).get("usage", {})
                total_tokens += int(usage.get("total_tokens", 0) or 0)
            elif r.status_code in (400, 403):
                # Guardrail block or budget/permission rejection — expected.
                blocked += 1
            else:
                errors += 1

        return {
            "requests": TRAFFIC_REQUESTS,
            "sent": sent,
            "blocked": blocked,
            "errors": errors,
            "total_cost_usd": round(total_cost, 6),
            "total_tokens": total_tokens,
            "by_model": by_model,
            "by_team": {k: round(v, 6) for k, v in by_team.items()},
            "warning": ("no finops_team_keys Variable — ran on the master key; "
                        "run finops_setup for per-team attribution") if used_fallback else None,
        }

    wait() >> fire()


generate_traffic()
