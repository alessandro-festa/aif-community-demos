# Fraud / AML Detection (vLLM, GPU)

A financial-crime / money-laundering / anomaly-detection blueprint. **Apache Airflow**
generates and manipulates a synthetic fraud graph, **trains an XGBoost classifier** on the
labelled data, and indexes behavioural feature vectors in **Milvus** for anomaly detection; a
local SUSE-styled investigator UI uses an LLM served by **vLLM (`Qwen/Qwen2.5-3B-Instruct`)**
as an **AML analyst** to classify and explain flagged accounts.

> **Inspired by and with thanks to [SantanderAI/gen-fraud-graph](https://github.com/SantanderAI/gen-fraud-graph)**
> (Apache-2.0) — the synthetic fraud-graph generator this blueprint builds on — and
> [srinivas-gajulaa/genai-fraud-detection](https://github.com/srinivas-gajulaa/genai-fraud-detection)
> for the analyst-explanation pattern. See [`ATTRIBUTION.md`](ATTRIBUTION.md).

This is the **GPU / vLLM** variant. For the **CPU / Ollama** variant (same pipeline, LLM on
Ollama — runs anywhere), see [`../fraud-detection-ollama`](../fraud-detection-ollama). The DAGs
and UI are identical; only the LLM endpoint differs.

Blueprint CR: [`fraud-detection-vllm-1-0-0.yaml`](fraud-detection-vllm-1-0-0.yaml)

## Components (all from the SUSE Application Collection)

| Component | Chart | Role |
|-----------|-------|------|
| **Apache Airflow** | `apache-airflow` `1.22.0` | orchestrates generate → train → anomaly (DAGs via git-sync) |
| **PostgreSQL** | `postgresql` `0.6.0` (`fraud-db`) | accounts, transactions, labels, scores, flagged accounts |
| **Milvus** | `milvus` `5.0.22` | per-account behavioural feature vectors for anomaly detection |
| **vLLM** | `vllm` `0.1.10` | `Qwen/Qwen2.5-3B-Instruct` — the AML analyst LLM, **GPU** |
| **Investigator UI** | — (local) | FastAPI + SUSE dashboard in [`ui/`](ui/), runs locally |

## Requirements

- A node with a **real NVIDIA GPU** and the **GPU Operator** (on a simulated-GPU cluster the
  vLLM pods schedule but do not actually serve).
- SUSE AI Factory operator, `application-collection` ClusterRepo (+ credentials), a default
  StorageClass, cert-manager.

## Pipeline & usage

Identical to the Ollama variant — three Airflow DAGs (`generate_fraud_dataset` →
`engineer_and_train` → `flag_and_anomaly`) produce the data, model scores and flagged
accounts; the UI explains flagged cases with the vLLM-served analyst. See
[`../fraud-detection-ollama/README.md`](../fraud-detection-ollama/README.md) for the full
pipeline description, DAG list, and notes on `SCALE_FACTOR` and DAG dependencies.

## Use it via the Blueprint Marketplace (recommended)

Pick **Fraud / AML Detection (vLLM, GPU)** and follow the guide: import → create the
AIWorkload in AI Factory (real GPU node; model downloads on first start) → run the three DAGs
→ it starts the local UI + port-forwards (Postgres, Milvus, vLLM router) → investigate.

## Notes

- The `model` in each request equals the served `modelURL` (`Qwen/Qwen2.5-3B-Instruct`).
- Verify the `containers/vllm-openai` image tag against the SUSE Application Collection for
  your environment.
- **Airflow image (baked)**: like the Ollama variant, this uses a **baked custom image**
  `ghcr.io/alessandro-festa/fraud-airflow:1.0.0` (built FROM the SUSE App Collection Airflow
  base with the DAG deps pre-installed — see [`airflow-image/Dockerfile`](airflow-image/Dockerfile)).
  Installing deps at pod start via `_PIP_ADDITIONAL_REQUIREMENTS` didn't converge (too slow vs
  Fleet reconcile); baking fixes it. Image is arm64; rebuild for amd64 if needed.
