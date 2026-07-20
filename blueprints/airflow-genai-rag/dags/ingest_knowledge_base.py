"""
DAG: ingest_knowledge_base

RAG ingestion pipeline (the SUSE/Ollama analogue of the original use case's
Weaviate ingestion). For each markdown file in include/knowledge_base/:

    read -> chunk -> embed (Ollama nomic-embed-text) -> upsert into Milvus

The Milvus `kb` collection is dropped and recreated on every run so ingestion is
idempotent. Trigger manually from the Airflow UI.
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import (
    KB_COLLECTION,
    KB_DIR,
    chunk_text,
    milvus_create_collection,
    milvus_drop_collection,
    milvus_insert,
    ollama_embed,
)


@dag(
    dag_id="ingest_knowledge_base",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["genai-rag", "ingest"],
)
def ingest_knowledge_base():
    @task
    def read_and_chunk() -> list[dict]:
        """Read every KB markdown file and split it into overlapping chunks."""
        docs = sorted(KB_DIR.glob("*.md"))
        if not docs:
            raise FileNotFoundError(f"No knowledge-base files found in {KB_DIR}")
        records: list[dict] = []
        for doc in docs:
            raw = doc.read_text(encoding="utf-8").strip()
            # First markdown heading (if any) becomes the title.
            first = raw.splitlines()[0] if raw else doc.stem
            title = first.lstrip("# ").strip() or doc.stem
            for chunk in chunk_text(raw):
                records.append(
                    {"text": chunk, "title": title, "source": doc.name}
                )
        if not records:
            raise ValueError("Knowledge base produced no chunks.")
        return records

    @task
    def embed_and_upsert(records: list[dict]) -> int:
        """Embed each chunk with Ollama and upsert the vectors into Milvus."""
        rows: list[dict] = []
        dim = 0
        for idx, rec in enumerate(records):
            vector = ollama_embed(rec["text"])
            dim = len(vector)
            rows.append(
                {
                    "id": idx,
                    "vector": vector,
                    "text": rec["text"],
                    "title": rec["title"],
                    "source": rec["source"],
                }
            )

        # Recreate the collection with the right dimensionality, then insert.
        milvus_drop_collection(KB_COLLECTION)
        milvus_create_collection(dim, KB_COLLECTION)
        milvus_insert(rows, KB_COLLECTION)
        return len(rows)

    embed_and_upsert(read_and_chunk())


ingest_knowledge_base()
