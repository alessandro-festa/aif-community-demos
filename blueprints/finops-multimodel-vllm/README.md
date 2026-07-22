# FinOps Multi-Model Gateway (vLLM, GPU)

A **FinOps** demo for SUSE AI Factory built on top of the `litellm-guardrails`
gateway — the **GPU / vLLM** sibling of `finops-multimodel-ollama`. It turns a guarded
LLM proxy into a **multi-model, cost-instrumented** workload and reconciles the two
cost planes every AI platform has:

```
                           ┌─ per-model pricing (input/output $ per token)
custom chat UI (local) ─► LiteLLM ──guardrails──► vLLM router ─► Qwen 0.5B / 1.5B / 3B (GPU)
Airflow traffic DAG ───┘      │  └─ Presidio (PII), hide-secrets, prompt-injection
  (per-team virtual keys)     └─ spend logs → LiteLLM Postgres
                                        │
                              litellm-exporter (reads DB) ─►/metrics┐
OpenCost (infra $, incl GPU) ────────────────────────────►/metrics ┤►  Prometheus ─► Grafana
Airflow backfill DAG ─► remote-write (backdated samples) ──────────┘   (FinOps dashboard)
```

- **Token spend** — LiteLLM's OSS spend logs, turned into real $ by per-token prices
  on each model, surfaced to Prometheus by a small **DB-reading exporter**
  (`nicholascecere/exporter-litellm`). LiteLLM's *native* Prometheus spend metrics are
  enterprise-gated, so we don't use them.
- **Infra cost** — **OpenCost** allocates the $ of the pods (incl. **GPU** node cost,
  the dominant cost here) into Prometheus.
- Both planes are visualised in a **provisioned Grafana dashboard**.
- **Apache Airflow** creates per-team virtual keys + budgets, fires synthetic
  multi-model traffic (real consumption), and **backfills** synthetic history into
  Prometheus via the remote-write receiver so dashboards populate immediately.
- Chat happens in a **custom local UI** wired to LiteLLM (Open WebUI is not used).

> **⚠️ GPU.** The default serves **three models = three GPUs** (one per vLLM
> `modelSpec`). For fewer GPUs, drop `modelSpec` entries in the CR and the matching
> `model_list` (LiteLLM) + `MODELS` (`dags/common.py`, `ui/app/main.py`) entries.

> **⚠️ Container image / architecture.** As in `litellm-guardrails`, the LiteLLM
> component ships the **multi-arch upstream image** `ghcr.io/berriai/litellm-database`.
> On amd64, prefer `registry.suse.com/ai/containers/litellm-database:v1.81.13`.

## Components

| Component | Chart (repo) | Notes |
|-----------|--------------|-------|
| vLLM | `vllm` (`application-collection`) | production-stack router; 3 ungated Qwen sizes on GPU, one OpenAI endpoint (`vllm-router-service:80`) |
| LiteLLM | `litellm` (`suse-ai-registry`) | multi-model + per-token pricing, `store_model_in_db: true`, bundled Postgres |
| Presidio | LiteLLM `extraResources` (`mcr.microsoft.com`) | PII analyzer + anonymizer |
| LiteLLM exporter | LiteLLM `extraResources` (`docker.io/nicholascecere/exporter-litellm`) | spend logs → Prometheus metrics |
| Prometheus | `prometheus` (`application-collection`) | remote-write receiver + 30d out-of-order for backfill |
| Grafana | `grafana` (`application-collection`) | Prometheus datasource + FinOps dashboard (provisioned) |
| OpenCost | `opencost` (`application-collection`) | infra $ (incl. GPU) → Prometheus (queried via the in-namespace Prometheus) |
| Apache Airflow | `apache-airflow` (`application-collection`) | setup / traffic / backfill DAGs, custom image |

## The models & pricing

Three ungated Qwen models at deliberately different **per-token prices** so the FinOps
story has variety (same tokens cost ~12× more on the 3B than the 0.5B):

| Model name (LiteLLM) | vLLM model (HF id) | input $/1M | output $/1M |
|----------------------|--------------------|-----------:|------------:|
| `qwen-0.5b` | `Qwen/Qwen2.5-0.5B-Instruct` | 0.05 | 0.10 |
| `qwen-1.5b` | `Qwen/Qwen2.5-1.5B-Instruct` | 0.20 | 0.40 |
| `qwen-3b`   | `Qwen/Qwen2.5-3B-Instruct`   | 0.60 | 1.20 |

LiteLLM routes to each via `hosted_vllm/<HF id>` at `http://vllm-router-service:80/v1`.

## The guardrail wizard

Reused from `litellm-guardrails`: the marketplace **import** step shows a checklist
(PII masking/blocking, secret redaction, prompt-injection). Your selection is deep-
merged into the `litellm` component's `proxy_config` before `kubectl apply`. Guardrails
apply to **both** the chat UI and the Airflow traffic generator.

## Airflow DAGs

Run **in order** from the Airflow UI (admin / admin):

1. `finops_setup` — creates a **virtual key + monthly budget** per team
   (engineering, data-science, marketing, support). LiteLLM generates the key values;
   they're captured into the Airflow Variable `finops_team_keys` for `generate_traffic`.
   Requires `store_model_in_db: true`. **Run this before `generate_traffic`.**
2. `backfill_history` — writes ~`BACKFILL_DAYS` (14) days of synthetic daily spend
   into Prometheus (`finops_daily_spend_usd`, `finops_daily_tokens_total`) via
   remote-write, at backdated timestamps.
3. `generate_traffic` — fires `TRAFFIC_REQUESTS` (40) synthetic chat requests across
   models/teams using the virtual keys → real token consumption in the spend logs.
   Re-run or schedule it to grow the live trend.

Env knobs on the `apache-airflow` component: `LITELLM_BASE_URL`, `LITELLM_MASTER_KEY`,
`PROM_REMOTE_WRITE_URL`, `TRAFFIC_REQUESTS`, `BACKFILL_DAYS`.

## Grafana FinOps dashboard

`FinOps — Multi-Model Gateway` (uid `finops-overview`), auto-provisioned:

- **Token spend (live)** — total spend, requests, blended cost/1k tokens, spend by
  model / team (LiteLLM exporter metrics).
- **Chargeback & budgets** — per-team budget utilization, tokens by model.
- **Historical spend (backfilled)** — daily spend by team / model (`finops_daily_*`).
- **Infrastructure cost (OpenCost)** — cluster $/hr (incl. GPU), projected monthly.

The dashboard JSON is embedded in the Blueprint CR (grafana `dashboards` value) and
mirrored at `grafana/dashboards/finops-overview.json`.

## Prerequisites (target cluster)

- SUSE AI Factory operator;
- the `application-collection` ClusterRepo (+ credentials/pull secret) and the
  `suse-ai-registry` ClusterRepo. **OpenCost / Prometheus / Grafana require the
  Application Collection "Prime" entitlement;**
- a default StorageClass;
- the **NVIDIA GPU Operator** and GPU node(s) — the default needs **3 GPUs**;
- cert-manager.

## Run the demo

1. In the marketplace, open **FinOps Multi-Model Gateway (vLLM)**, choose guardrails,
   and **Import**.
2. In the SUSE AI Factory UI: **Create AIWorkload**, pick a namespace, deploy. Wait for
   all components to be Ready (vLLM downloads models on first start — several minutes).
3. Open **Airflow**, run `finops_setup` → `backfill_history` → `generate_traffic`.
4. Open **Grafana** → *FinOps — Multi-Model Gateway*. Explore token spend, chargeback,
   history and infra cost (GPU included). Use **OpenCost UI** for per-workload detail.
5. **Launch the chat UI**, pick a model + team, and chat — each reply shows its cost and
   tokens, and the spend shows up in Grafana.

## Notes & things to verify on your cluster

- **Demo only, unsupported.** Master key (`sk-guardrails-demo`), Postgres/Grafana
  passwords are demo placeholders — change them for anything real. Per-team virtual
  keys are generated by LiteLLM at setup time and stored in an Airflow Variable.
- **Chargeback attribution.** The `generate_traffic` DAG uses per-team virtual keys
  (so spend rolls up to teams + budgets). The chat UI uses the master key + a team
  **tag** (`x-litellm-tags`) — virtual key values are hashed and can't be re-read, so
  the UI attributes by tag instead.
- **GPU count.** Default = 3 GPUs (one per model). Trim `modelSpec` + the matching
  `model_list` / `MODELS` entries for fewer GPUs.
- **vLLM router service name.** LiteLLM points at `vllm-router-service`. If your Helm
  release name isn't `vllm`, adjust to `<release>-router-service`.
- **Bundled Postgres service name.** The exporter points at `litellm-postgresql`. If
  your release names it differently, update `LITELLM_DB_HOST` (`kubectl -n <ns> get svc`).
- **Grafana login is `admin` / `admin`** (set via `adminPassword` on the grafana
  component). `prom-operator` is the *rancher-monitoring* (kube-prometheus-stack)
  Grafana default — a different Grafana; this blueprint deploys its own. To confirm:
  `kubectl -n <ns> get secret grafana -o jsonpath='{.data.admin-password}' | base64 -d`.
- **OpenCost is configured against the cluster out of the box.** It queries this
  blueprint's in-namespace Prometheus (`prometheus.external.url:
  http://prometheus-server:80`, bare DNS so it works in any namespace), which scrapes
  cluster-wide **cadvisor** + **kube-state-metrics**. OpenCost emits node-cost metrics
  itself using its **default on-prem pricing** (realistic but synthetic; GPU nodes
  carry the dominant cost). Allocation appears a few minutes after the pods are Ready.
- **Already have cluster monitoring?** To reuse an existing Prometheus/Grafana instead
  of this blueprint's, point `opencost.prometheus.external.url` and the Grafana
  datasource at it (e.g. `http://rancher-monitoring-prometheus.cattle-monitoring-system:9090`)
  and set the `prometheus`/`grafana` components' replicas to 0 (or remove them).
- **No custom Airflow image.** Uses the stock `apache-airflow` image — the DAGs need
  only `requests`; the Prometheus remote-write client in `dags/common.py` is pure-stdlib.
- **Chart versions** pinned to the Application Collection versions available at
  authoring time (vllm 0.1.10, prometheus 29.13.1, grafana 12.7.2, opencost 2.5.27).
