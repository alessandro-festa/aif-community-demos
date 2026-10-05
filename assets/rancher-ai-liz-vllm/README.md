# Rancher AI Assistant (Liz, vLLM GPU)

Installs **Rancher AI Assistant ("Liz")** end-to-end on GPU: the `rancher-ai-agent`
backend, the `rancher-ai-ui` extension (the chat panel inside the Rancher UI), and
**vLLM** serving `Qwen/Qwen2.5-7B-Instruct` with tool-calling enabled
(`--enable-auto-tool-choice --tool-call-parser=hermes`). Liz's three built-in agents —
**Rancher** (general), **Continuous Delivery** (Fleet/GitOps), and **Cluster
Provisioning** (CAPI/K3k) — ship enabled by default and answer using that GPU-served
model.

This is the **GPU** sibling of [`../rancher-ai-liz-ollama`](../rancher-ai-liz-ollama)
(CPU/micro), following this catalog's usual `-ollama`/`-vllm` pairing. Unlike most
`-vllm` variants here, this one doesn't swap a chart's own model for a bigger one — it
swaps the whole LLM-serving component from Ollama to vLLM, since better tool-calling
reliability (vLLM's native tool-call parser) is the actual point of the GPU version.

Blueprint CR: [`rancher-ai-liz-vllm-1-0-0.yaml`](rancher-ai-liz-vllm-1-0-0.yaml)

> **Rancher management (local) cluster only, with a GPU node.** Rancher's AI Assistant
> backend and UI extension expect `rancher-ai-agent` in namespace
> `cattle-ai-agent-system` on the same cluster Rancher server itself runs on — not an
> arbitrary downstream/tenant cluster.

## Credit

Component shape adapted from [Edu Minguez](https://github.com/e-minguez)'s example
Blueprint for the SUSE AI Factory operator:
[`e-minguez/aif` — `rancher-ai-assistant-with-ollama-1.0.0.yaml`](https://github.com/e-minguez/aif/blob/liz-blueprint/examples/rancher-ai-assistant-with-ollama-1.0.0.yaml)
(his example ran Ollama for both CPU and GPU sizing; here the GPU path uses this
catalog's standard vLLM component instead). The community-mirror workaround below is
also his.

## Why a community chart mirror

The official `rancher-ai-agent` chart lives at `oci://registry.suse.com/rancher/charts`,
tagged `109.0.1_up1.0.2` (Helm's OCI-safe encoding of `+` → `_` for the semver build
metadata in `109.0.1+up1.0.2`). **Fleet's HelmOp — what AI Factory uses to deploy
Blueprint components — cannot resolve that tag in any form** (`+`, `_`, or plain semver
all fail identically): confirmed upstream bug
[rancher/fleet#5410](https://github.com/rancher/fleet/issues/5410), not something fixable
from this blueprint. Rancher's own `ClusterRepo` indexer resolves the chart version fine
— only Fleet's own OCI tag matching is affected.

Until that's fixed, `rancher-ai-agent` is pulled from **`liz-charts-obs`**, an
**UNOFFICIAL, community-rebuilt mirror** (courtesy of Edu Minguez) that re-tags the same
chart content with a clean semver (`v1.0.2`) Fleet can parse. **This is not a
SUSE-supported artifact.** For production, install the Prime chart from
`oci://registry.suse.com/rancher/charts` out-of-band instead — or once the Fleet issue is
fixed, repoint the `liz-charts-obs` ClusterRepo at the official registry and restore the
real tag.

## Components

| Component | Chart | Namespace | Role |
|-----------|-------|-----------|------|
| **vLLM** | `vllm` `0.1.10` (application-collection) | `cattle-ai-agent-system` | GPU LLM backend, `Qwen2.5-7B-Instruct`, tool-calling enabled, OpenAI-compatible API via `vllm-router-service` |
| **rancher-ai-agent** | `rancher-ai-agent` `v1.0.2` (`liz-charts-obs` mirror) | `cattle-ai-agent-system` | Liz's backend — supervisor + 3 built-in agents, wired to vLLM via `activeLlm: openai` |
| **rancher-ai-ui** | `rancher-ai-ui` `1.0.1` (`rancher-ui-plugins-http`) | `cattle-ui-plugin-system` | registers the UIPlugin that surfaces Liz's chat panel in the Rancher UI |

## Use it via the Blueprint Marketplace (recommended)

Pick **Rancher AI Assistant (Liz, vLLM GPU)** and follow the guide: import → create the
AIWorkload in AI Factory **targeting namespace `cattle-ai-agent-system`** on a
GPU-capable node → reload the Rancher UI to pick up the extension → open the AI Assistant
panel and chat with the built-in agents.

## Notes

- **Requires a real GPU node** with the NVIDIA GPU Operator installed
  (`gpu-operator` namespace prerequisite) — `vllm`'s pod requests 1 GPU and won't
  schedule without one.
- **Namespace matters.** This only works if the AIWorkload lands in
  `cattle-ai-agent-system` — that's where Rancher's own AI Assistant backend looks for
  it. Each component also pins its own `targetNamespace`, so vLLM and the Liz agent land
  there (and the UI extension in `cattle-ui-plugin-system`) regardless of what the
  AIWorkload wizard is given.
- **Why vLLM instead of Ollama-with-GPU.** vLLM's `--enable-auto-tool-choice
  --tool-call-parser=hermes` flags give Qwen2.5-Instruct reliable, parseable tool calls —
  this catalog's already-proven pattern for agentic tool use (see `dora-compliance-vllm`)
  — rather than depending on Ollama's own (less consistent) tool-call support.
- **UI extension needs a reload.** The `rancher-ai-ui` component registers a `UIPlugin`;
  Rancher needs one browser reload to pick it up (a **Reload** banner usually appears at
  the top of the page).
- **Multi-agent scope.** Only the three built-in agents ship enabled. Adding the optional
  named agents (SUSE Application Collection, SUSE Observability, SUSE Security,
  CloudCasa) or fully custom agents is still a manual step today — see the "Multi-agent"
  section of the
  [Rancher AI admin how-to](https://documentation.suse.com/cloudnative/rancher-ai/latest/en/how-tos/how-to-admin.html#multi-agent).
- **Demo only** — unsupported community blueprint; the chart source itself is an
  unofficial mirror (see above).
