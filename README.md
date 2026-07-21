# aif-community-demos

Community demo **blueprints** for [SUSE AI Factory](https://github.com/SUSE/aif), plus a
local **Blueprint Marketplace** to browse, import, and demo them.

Each blueprint is a SUSE AI Factory `Blueprint` custom resource
(`ai-factory.suse.com/v1alpha1`) composed from the **SUSE Application Collection**, with a
small local demo UI and a step-by-step guide.

## Blueprints

| Blueprint | Folder | Compute | What it is |
|-----------|--------|---------|-----------|
| **Airflow GenAI RAG (Ollama)** | [`blueprints/airflow-genai-rag`](blueprints/airflow-genai-rag) | CPU | Apache Airflow + Ollama + Milvus RAG pipeline (a SUSE/Ollama derivation of Astronomer's gen-ai-fine-tune-rag use case). Airflow ingests a knowledge base and customizes an Ollama model; a local UI generates grounded posts via RAG. |
| **Fraud / AML Detection (Ollama)** | [`blueprints/fraud-detection-ollama`](blueprints/fraud-detection-ollama) | CPU | Financial-crime / money-laundering / anomaly detection. Airflow generates a synthetic fraud graph (SantanderAI/gen-fraud-graph), trains an XGBoost classifier, and indexes behavioural vectors in Milvus; a local LLM (Ollama, `qwen2.5:3b`) acts as an AML analyst explaining flagged accounts. |
| **Fraud / AML Detection (vLLM)** | [`blueprints/fraud-detection-vllm`](blueprints/fraud-detection-vllm) | **GPU** | Same fraud/AML pipeline as above, but the analyst LLM (`Qwen/Qwen2.5-3B-Instruct`) is served by vLLM on an NVIDIA GPU. Requires a real GPU node + GPU Operator. |
| **SUSE VSC (Video Search & Summarization)** | [`blueprints/suse-vss`](blueprints/suse-vss) | CPU | All-SUSE, non-NVIDIA Video Search & Summarization. Ollama serves `moondream:1.8b` (multimodal) and Milvus stores each frame's CLIP embedding. Local SUSE-styled UI: ingest a video by URL / upload / webcam / YouTube / RTSP, run a prompt, and search past frames by text. |
| **VisionGPT (Ollama)** | [`blueprints/visiongpt-ollama`](blueprints/visiongpt-ollama) | CPU | LLM-assisted navigation hazard detection (a SUSE derivation of AIS-Clemson's VisionGPT). Ollama serves Qwen2.5-VL (`qwen2.5vl:3b`); a local UI samples frames from a walking video and returns a per-frame danger score + short reason at a selectable sensitivity. |
| **VisionGPT (vLLM)** | [`blueprints/visiongpt-vllm`](blueprints/visiongpt-vllm) | **GPU** | Same VisionGPT hazard detection as above, but `Qwen/Qwen2.5-VL-3B-Instruct` is served by vLLM on an NVIDIA GPU. Requires a real GPU node + GPU Operator. |

Every blueprint folder contains:
- `*-<version>.yaml` — the Blueprint CR to `kubectl apply`;
- `ui/` — a local FastAPI + SUSE-styled demo UI;
- `marketplace.yaml` — catalog card, prerequisites, local-frontend definition, in-cluster component UIs, and guided-demo steps;
- `README.md` — install + demo runbook;
- (Airflow-based blueprints) `dags/` + `include/` — the Airflow DAGs (delivered via git-sync) and sample data.

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
- a default StorageClass and cert-manager;
- for the **GPU blueprints** (Fraud/vLLM, VisionGPT/vLLM): the NVIDIA **GPU Operator** and a node with a real NVIDIA GPU.

Import a blueprint, then create an **AIWorkload** from it in the SUSE AI Factory UI to deploy it.
