"""
DAG: customize_model  (Ollama variant only)

Teach the base model a support-agent *persona* via an Ollama Modelfile — the
CPU-friendly, no-GPU analogue of fine-tuning (same approach as airflow-genai-rag's
astra-custom). Builds a system persona + a few real few-shot examples drawn from
resolved tickets in Postgres, then POSTs /api/create to Ollama, tagging the model
`support-agent`. The chat UI selects this model when present.

The vLLM variant does not run this DAG; it applies the same persona as a system
prompt in the app instead.
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import BASE_MODEL, CUSTOM_MODEL, ollama_create_model, pg_query

SYSTEM_PERSONA = (
    "You are Ava, a calm, empathetic insurance customer-support agent. Help the "
    "customer understand their policy, open or close support tickets, and explain "
    "claim decisions in plain language. Be concise and factual, ground every answer "
    "in the customer's policy and ticket data provided to you, and never invent "
    "coverage. When you reference a similar past case, only use the redacted summary "
    "you are given — never reveal another customer's personal details. If a request "
    "is outside policy or needs a human, say so and offer to escalate."
)


@dag(
    dag_id="customize_model",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["insurance-support", "customize"],
)
def customize_model():
    @task
    def build_messages() -> list[dict]:
        """A handful of resolved tickets become user/assistant few-shot pairs."""
        rows = pg_query("""
            SELECT t.subject, t.body, t.resolution_notes
            FROM support_tickets t
            WHERE t.status IN ('resolved', 'closed')
              AND t.resolution_notes IS NOT NULL AND t.resolution_notes <> ''
            ORDER BY t.ticket_id LIMIT 6
        """)
        messages: list[dict] = []
        for subject, body, resolution in rows:
            messages.append({"role": "user", "content": f"{subject}\n\n{body}"})
            messages.append({"role": "assistant", "content": resolution})
        return messages

    @task
    def create(messages: list[dict]) -> dict:
        ollama_create_model(CUSTOM_MODEL, BASE_MODEL, SYSTEM_PERSONA, messages)
        return {"model": CUSTOM_MODEL, "from": BASE_MODEL, "examples": len(messages) // 2}

    create(build_messages())


customize_model()
