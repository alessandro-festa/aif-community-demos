"""
DAG: backfill_history

Seed the FinOps dashboards with instant HISTORY by writing BACKDATED synthetic
samples straight into Prometheus via its remote-write receiver. Without this you'd
have to wait days of live traffic before the trend panels showed anything.

For each of the past BACKFILL_DAYS days it synthesises a plausible daily spend per
team × model (weekday/weekend variation, team + model weights, real per-token
prices from common.MODELS) and writes it at that day's midnight-UTC timestamp:

  finops_daily_spend_usd{team_alias, model, use_case}   (USD spent that day)
  finops_daily_tokens_total{model}                      (tokens that day)

The Grafana "Historical spend (backfilled)" panels read these. Live panels read the
LiteLLM exporter metrics instead, so backfilled history and live spend never mix.

Requires the prometheus component's remote-write receiver + out-of-order window
(both set in the Blueprint CR: web.enable-remote-write-receiver + out_of_order_time_window: 30d).
"""
from __future__ import annotations

import random

import pendulum
from airflow.decorators import dag, task

from common import BACKFILL_DAYS, MODELS, TEAMS, remote_write

# Baseline chat requests per day across all teams (before weights/variation).
BASE_REQUESTS_PER_DAY = 140
# Rough tokens per request (prompt + completion) and the input/output split.
AVG_TOKENS_PER_REQUEST = 420
INPUT_FRACTION = 0.6


@dag(
    dag_id="backfill_history",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["finops", "backfill"],
)
def backfill_history():
    @task
    def write_history() -> dict:
        now = pendulum.now("UTC").start_of("day")
        samples: list[dict] = []
        grand_total = 0.0

        for day in range(BACKFILL_DAYS, 0, -1):
            d = now.subtract(days=day)
            ts_ms = int(d.timestamp() * 1000)
            # Weekends are quieter; weekdays busy, with a little noise.
            iso = d.isoweekday()     # 1=Mon … 7=Sun
            day_factor = (0.35 if iso >= 6 else 1.0) * random.uniform(0.8, 1.2)
            day_tokens: dict[str, float] = {}

            for team_alias, _key, _budget, use_case, tw in TEAMS:
                team_reqs = BASE_REQUESTS_PER_DAY * tw * day_factor
                for model_name, in_price_1k, out_price_1k, mw in MODELS:
                    reqs = team_reqs * mw
                    tokens = reqs * AVG_TOKENS_PER_REQUEST * random.uniform(0.85, 1.15)
                    in_tok = tokens * INPUT_FRACTION
                    out_tok = tokens * (1 - INPUT_FRACTION)
                    spend = (in_tok / 1000 * in_price_1k) + (out_tok / 1000 * out_price_1k)
                    grand_total += spend
                    day_tokens[model_name] = day_tokens.get(model_name, 0.0) + tokens
                    samples.append({
                        "metric": {
                            "__name__": "finops_daily_spend_usd",
                            "team_alias": team_alias,
                            "model": model_name,
                            "use_case": use_case,
                        },
                        "value": round(spend, 6),
                        "timestamp": ts_ms,
                    })

            for model_name, tokens in day_tokens.items():
                samples.append({
                    "metric": {"__name__": "finops_daily_tokens_total", "model": model_name},
                    "value": round(tokens, 1),
                    "timestamp": ts_ms,
                })

        remote_write(samples)
        return {
            "days": BACKFILL_DAYS,
            "samples_written": len(samples),
            "synthetic_total_spend_usd": round(grand_total, 2),
        }

    write_history()


backfill_history()
