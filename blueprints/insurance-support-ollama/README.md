# Insurance Support Copilot (Ollama, CPU)

A customer-support automation demo for an insurance company. A local chat UI lets a
customer upload an accident photo, describe their issue, **open/close support
tickets**, and get **similar past cases suggested — redacted** (PII removed) with
Presidio.

> ⚠️ **Demo only.** All data is **synthetic**. Presidio redaction is best-effort and
> is a guardrail, not a guarantee — pair with access control + encryption for real use.

## Flow

```
Airflow ─ generate_dataset → Postgres (customers/families/policies/claims/tickets)
        ├ index_cases      → embed tickets → Milvus support_cases (semantic search)
        └ customize_model  → Ollama persona model `support-agent`

Local chat UI ─ chat + accident photo → Ollama qwen2.5vl:3b (chat + vision)
   ├ semantic search : text  → nomic-embed → Milvus → redact (Presidio) → show
   ├ similarity      : photo → CLIP        → Milvus → redact → show
   ├ open/close ticket: model proposes → you confirm → Postgres write
   └ browse recent tickets
```

## Components

| Component | Chart (repo) | Notes |
|---|---|---|
| PostgreSQL | `postgresql` (application-collection) | `support-db:5432`; customers/policies/claims/tickets. |
| Milvus | `milvus` (application-collection) | Standalone + REST v2; semantic case index at `milvus:19530`. |
| Ollama | `ollama` (application-collection) | CPU; `qwen2.5vl:3b` (chat+vision) + `nomic-embed-text`. |
| Apache Airflow | `apache-airflow` (application-collection) | Custom image (Faker + psycopg2); DAGs via git-sync. |

CLIP (`clip-ViT-B-32`) and Presidio run **in the local UI on CPU** — no extra cluster
components.

## Airflow DAGs

1. **generate_dataset** — Faker synthetic insurance data → Postgres (size via `N_TICKETS`,
   set in the import wizard).
2. **index_cases** — embeds resolved/closed tickets → Milvus `support_cases`.
3. **customize_model** — builds a `support-agent` persona model via an Ollama Modelfile.

## Airflow image

Uses the **stock** SUSE Application Collection `apache-airflow` image — no custom image
to build. The DAGs use only Python stdlib + `psycopg2` (already present in the image for
Airflow's own Postgres metadata backend), and talk to the embedding endpoint + Milvus
over HTTP.

## Requirements

- SUSE AI Factory operator; `application-collection` ClusterRepo (+ credentials);
  a default StorageClass; cert-manager.
- Host tools for the local UI: `python3` (the marketplace builds a venv with CPU torch +
  sentence-transformers + Presidio + a small spaCy model).

For a GPU version (Qwen2.5-VL on vLLM) use **Insurance Support Copilot (vLLM, GPU)**.

## Redaction

Retrieved similar cases pass through Presidio (analyzer → anonymizer) in the UI before
display, with custom recognizers for `POLICY_NUMBER` (`POL-…`) and `CLAIM_ID` (`CLM-…`)
on top of the built-ins (names, emails, phones, cards, SSNs, locations). A regex fallback
applies if Presidio can't initialise.
