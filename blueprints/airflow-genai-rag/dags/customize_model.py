"""
DAG: customize_model

Model-customization pipeline — the CPU-friendly, no-GPU analogue of the original
use case's OpenAI hosted fine-tuning. Instead of training weights, it teaches the
base model a house *style* via an Ollama Modelfile:

    read include/examples/*.txt -> build (system persona + few-shot messages)
      -> POST /api/create on Ollama -> custom model tag `astra-custom`

Each example file: the first line is the post's topic/title, the rest is the
post body written in the target style. These become user/assistant message pairs
so the customized model mimics the tone. Trigger manually from the Airflow UI.
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import (
    BASE_MODEL,
    CUSTOM_MODEL,
    EXAMPLES_DIR,
    ollama_create_model,
    ollama_tags,
)

SYSTEM_PERSONA = (
    "You are Astra, a social-media copywriter for the Apache Airflow and SUSE AI "
    "community. Write short, punchy, technically accurate posts (2-4 sentences). "
    "Ground every post in the facts provided to you and never invent features. "
    "Match the upbeat, practical tone of the examples you were trained on."
)


@dag(
    dag_id="customize_model",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["genai-rag", "customize"],
)
def customize_model():
    @task
    def build_messages() -> list[dict]:
        """Turn each example post into a user/assistant few-shot message pair."""
        examples = sorted(EXAMPLES_DIR.glob("*.txt"))
        if not examples:
            raise FileNotFoundError(f"No example posts found in {EXAMPLES_DIR}")
        messages: list[dict] = []
        for ex in examples:
            lines = ex.read_text(encoding="utf-8").strip().splitlines()
            if not lines:
                continue
            title = lines[0].lstrip("# ").strip()
            body = "\n".join(lines[1:]).strip()
            if not title or not body:
                continue
            messages.append(
                {"role": "user", "content": f"Write a post about: {title}"}
            )
            messages.append({"role": "assistant", "content": body})
        if not messages:
            raise ValueError("No usable example posts (need a title line + body).")
        return messages

    @task
    def create_model(messages: list[dict]) -> str:
        """Create the customized Ollama model and verify it appears in /api/tags."""
        ollama_create_model(
            name=CUSTOM_MODEL,
            from_model=BASE_MODEL,
            system=SYSTEM_PERSONA,
            messages=messages,
        )
        tags = ollama_tags()
        if not any(t.split(":")[0] == CUSTOM_MODEL for t in tags):
            raise RuntimeError(
                f"Model {CUSTOM_MODEL!r} not found after create; tags={tags}"
            )
        return CUSTOM_MODEL

    create_model(build_messages())


customize_model()
