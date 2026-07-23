# aif-community-demos

Community demo **blueprints** for [SUSE AI Factory](https://github.com/SUSE/aif), plus a
local **Blueprint Marketplace** to browse, import, and demo them.

Each blueprint is a SUSE AI Factory `Blueprint` custom resource
(`ai-factory.suse.com/v1alpha1`) composed from the **SUSE Application Collection**, with a
small local demo UI and a step-by-step guide.

## Blueprints

| Topic | Blueprint | Folder | Compute | What it is |
|-------|-----------|--------|---------|-----------|
| Data engineering | **Airflow GenAI RAG (Ollama)** | [`blueprints/airflow-genai-rag`](blueprints/airflow-genai-rag) | CPU | Apache Airflow + Ollama + Milvus RAG pipeline (a SUSE/Ollama derivation of Astronomer's gen-ai-fine-tune-rag use case). Airflow ingests a knowledge base and customizes an Ollama model; a local UI generates grounded posts via RAG. |
| Anomaly detection | **Fraud / AML Detection (Ollama)** | [`blueprints/fraud-detection-ollama`](blueprints/fraud-detection-ollama) | CPU | Financial-crime / money-laundering / anomaly detection. Airflow generates a synthetic fraud graph (SantanderAI/gen-fraud-graph), trains an XGBoost classifier, and indexes behavioural vectors in Milvus; a local LLM (Ollama, `qwen2.5:3b`) acts as an AML analyst explaining flagged accounts. |
| Anomaly detection | **Fraud / AML Detection (vLLM)** | [`blueprints/fraud-detection-vllm`](blueprints/fraud-detection-vllm) | **GPU** | Same fraud/AML pipeline as above, but the analyst LLM (`Qwen/Qwen2.5-3B-Instruct`) is served by vLLM on an NVIDIA GPU. Requires a real GPU node + GPU Operator. |
| Media | **SUSE VSC (Video Search & Summarization)** | [`blueprints/suse-vss`](blueprints/suse-vss) | CPU | All-SUSE, non-NVIDIA Video Search & Summarization. Ollama serves `moondream:1.8b` (multimodal) and Milvus stores each frame's CLIP embedding. Local SUSE-styled UI: ingest a video by URL / upload / webcam / YouTube / RTSP, run a prompt, and search past frames by text. |
| Safety | **VisionGPT (Ollama)** | [`blueprints/visiongpt-ollama`](blueprints/visiongpt-ollama) | CPU | LLM-assisted navigation hazard detection (a SUSE derivation of AIS-Clemson's VisionGPT). Ollama serves Qwen2.5-VL (`qwen2.5vl:3b`); a local UI samples frames from a walking video and returns a per-frame danger score + short reason at a selectable sensitivity. |
| Safety | **VisionGPT (vLLM)** | [`blueprints/visiongpt-vllm`](blueprints/visiongpt-vllm) | **GPU** | Same VisionGPT hazard detection as above, but `Qwen/Qwen2.5-VL-3B-Instruct` is served by vLLM on an NVIDIA GPU. Requires a real GPU node + GPU Operator. |
| Security | **LiteLLM Guardrails (Ollama + Open WebUI)** | [`blueprints/litellm-guardrails`](blueprints/litellm-guardrails) | CPU | A guarded LLM gateway: Open WebUI → a LiteLLM proxy that applies guardrails (Presidio PII masking/blocking, secret redaction, prompt-injection detection) → Ollama (`llama3.2:1b`). Pick the guardrails in a pre-import wizard; they're injected into the LiteLLM config at import time. |
| Healthcare | **Chest X-ray Copilot (Ollama)** | [`blueprints/xray-copilot-ollama`](blueprints/xray-copilot-ollama) | CPU | Analyse a chest X-ray with a medical vision LLM (MedGemma via Ollama) and search a Milvus index of X-rays by image (similarity) or text (semantic) using BiomedCLIP embeddings. Demo only — not for clinical use. |
| Healthcare | **Chest X-ray Copilot (vLLM)** | [`blueprints/xray-copilot-vllm`](blueprints/xray-copilot-vllm) | **GPU** | Same X-ray analysis + search, with **LLaVA-Med 7B** (baseline) and optional **MedGemma 1.5 4B** served by vLLM. The import wizard collects a HuggingFace token for the gated MedGemma model. Demo only — not for clinical use. |
| Customer support | **Insurance Support Copilot (Ollama)** | [`blueprints/insurance-support-ollama`](blueprints/insurance-support-ollama) | CPU | Insurance customer-support chatbot. Airflow generates a synthetic support dataset (customers/families/policies/claims/tickets) into Postgres + a Milvus semantic index; a chat UI (Ollama `qwen2.5vl`) uploads accident photos, opens/closes tickets, and suggests similar past cases — redacted (Presidio). Demo only, synthetic data. |
| Customer support | **Insurance Support Copilot (vLLM)** | [`blueprints/insurance-support-vllm`](blueprints/insurance-support-vllm) | **GPU** | Same insurance support copilot, with `Qwen2.5-VL-7B` served by vLLM on a GPU (embeddings via a small CPU Ollama). Demo only, synthetic data. |
| Compliance | **DORA Compliance (Ollama)** | [`blueprints/dora-compliance-ollama`](blueprints/dora-compliance-ollama) | CPU | DORA / BaFin ICT-incident compliance. Airflow simulates ICT incidents into Postgres, classifies them against BaFin Article 18 rules (severity + deadlines), builds report / vendor-risk marts, indexes incidents into Milvus, and flags SLA breaches; a local agent UI (Ollama) answers compliance questions and can run the whole pipeline. Demo only, synthetic data. |
| Compliance | **DORA Compliance (vLLM)** | [`blueprints/dora-compliance-vllm`](blueprints/dora-compliance-vllm) | **GPU** | Same DORA compliance pipeline, with the analyst LLM served by vLLM on an NVIDIA GPU. Demo only, synthetic data. |
| FinOps | **FinOps Multi-Model Gateway (Ollama)** | [`blueprints/finops-multimodel-ollama`](blueprints/finops-multimodel-ollama) | CPU | FinOps for a guarded, multi-model LLM gateway. A LiteLLM proxy serves three Ollama models at different per-token prices with guardrails; Grafana reconciles TOKEN spend (LiteLLM OSS spend logs → a Prometheus exporter) with INFRASTRUCTURE cost (OpenCost). Airflow creates per-team virtual keys + budgets and backfills synthetic history so the dashboards populate immediately; a local chat UI shows each reply's cost. |
| FinOps | **FinOps Multi-Model Gateway (vLLM)** | [`blueprints/finops-multimodel-vllm`](blueprints/finops-multimodel-vllm) | **GPU** | Same FinOps multi-model gateway, with three Qwen sizes served by vLLM on NVIDIA GPUs. |

Every blueprint folder contains:
- `*-<version>.yaml` — the Blueprint CR to `kubectl apply`;
- `ui/` — a local FastAPI + SUSE-styled demo UI;
- `marketplace.yaml` — catalog card, prerequisites, local-frontend definition, in-cluster component UIs, and guided-demo steps;
- `README.md` — install + demo runbook;
- (Airflow-based blueprints) `dags/` + `include/` — the Airflow DAGs (delivered via git-sync) and sample data.

## Blueprint Marketplace

[`marketplace/`](marketplace) is a single Go binary that pulls this repo, lists the
blueprints (**search + topic filter**), lets you pick a kube context (showing SUSE AI
Factory readiness), imports a blueprint via `kubectl` — with an optional **pre-import
wizard** (toggle options and enter secrets such as a HuggingFace token, injected into the
Blueprint CR before apply) — and walks you through the guided demo, starting each
blueprint's local frontend + `kubectl port-forward`s. For Airflow blueprints it can also
**run the demo's DAG pipeline in one click** — it triggers the DAGs in order and waits for
each to finish (you can still run them manually in the Airflow UI).

Prebuilt binaries are attached to each
[release](https://github.com/alessandro-festa/aif-community-demos/releases):

```bash
# pick the asset for your platform (darwin/linux, amd64/arm64):
curl -LO https://github.com/alessandro-festa/aif-community-demos/releases/download/v0.5.0/bpm-linux-amd64
curl -LO https://github.com/alessandro-festa/aif-community-demos/releases/download/v0.5.0/SHA256SUMS
shasum -a 256 -c SHA256SUMS --ignore-missing && chmod +x bpm-linux-amd64
./bpm-linux-amd64   # open http://127.0.0.1:8900
```

Or build from source:

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
- for the **GPU blueprints** (Fraud/vLLM, VisionGPT/vLLM, X-ray Copilot/vLLM): the NVIDIA **GPU Operator** and a node with a real NVIDIA GPU.

Import a blueprint, then create an **AIWorkload** from it in the SUSE AI Factory UI to deploy it.
