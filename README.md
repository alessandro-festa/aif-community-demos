# aif-community-demos

Community demo **blueprints** for [SUSE AI Factory](https://github.com/SUSE/aif), plus a
local **Blueprint Marketplace** to browse, import, and demo them.

Each blueprint is a SUSE AI Factory `Blueprint` custom resource
(`ai-factory.suse.com/v1alpha1`) composed from the **SUSE Application Collection**, with a
small local demo UI and a step-by-step guide.

## Blueprints

| Blueprint | Folder | What it is |
|-----------|--------|-----------|
| **Airflow GenAI RAG (Ollama)** | [`blueprints/airflow-genai-rag`](blueprints/airflow-genai-rag) | Apache Airflow + Ollama + Milvus RAG pipeline (a SUSE/Ollama derivation of Astronomer's gen-ai-fine-tune-rag use case). Airflow ingests a knowledge base and customizes an Ollama model; a local UI generates grounded posts. |
| **SUSE VSC** | [`blueprints/suse-vss`](blueprints/suse-vss) | All-SUSE, non-NVIDIA Video Search & Summarization on CPU — Ollama (`moondream:1.8b`) + Milvus, with a local SUSE-styled UI. |

Every blueprint folder contains:
- `*-<version>.yaml` — the Blueprint CR to `kubectl apply`;
- `ui/` — a local FastAPI + SUSE-styled demo UI;
- `marketplace.yaml` — catalog card, prerequisites, local-frontend definition, and guided-demo steps;
- `README.md` — install + demo runbook;
- (airflow) `dags/` + `include/` — the Airflow DAGs (delivered via git-sync) and sample data.

## Blueprint Marketplace

[`marketplace/`](marketplace) is a single Go binary that pulls this repo, lists the
blueprints, lets you pick a kube context (showing SUSE AI Factory readiness), imports a
blueprint via `kubectl`, and walks you through the guided demo — starting each blueprint's
local frontend + `kubectl port-forward`s.

```bash
cd marketplace
go build -o bpm .
./bpm            # pulls this repo by default; open http://127.0.0.1:8900
# or, for local dev against a checkout:
./bpm --dir ../blueprints
```

Host prerequisites: `kubectl`, `git`, `python3`. See [`marketplace/README.md`](marketplace/README.md).

## Prerequisites (target cluster)

- SUSE AI Factory operator installed;
- the `application-collection` ClusterRepo + credentials secret;
- a default StorageClass and cert-manager.

Import a blueprint, then create an **AIWorkload** from it in the SUSE AI Factory UI to deploy it.
