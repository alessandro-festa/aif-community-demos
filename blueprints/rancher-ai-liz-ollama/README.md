# Rancher AI Assistant (Liz, Ollama)

Installs **Rancher AI Assistant ("Liz")** end-to-end: the `rancher-ai-agent` backend, the
`rancher-ai-ui` extension (the chat panel inside the Rancher UI), and an **Ollama**
instance preloaded with a micro tool-calling model (`qwen2.5:1.5b`, CPU inference). Liz's
three built-in agents — **Rancher** (general), **Continuous Delivery** (Fleet/GitOps), and
**Cluster Provisioning** (CAPI/K3k) — ship enabled by default and answer using that local
model.

This is the **CPU/micro** variant, sized to run without a GPU or heavy RAM headroom. See
[`../rancher-ai-liz-vllm`](../rancher-ai-liz-vllm) for the **GPU** variant (vLLM +
Qwen2.5-7B-Instruct with tool-calling enabled) for more reliable agent tool use.

Blueprint CR: [`rancher-ai-liz-ollama-1-0-0.yaml`](rancher-ai-liz-ollama-1-0-0.yaml)

> **Rancher management (local) cluster only.** Rancher's AI Assistant backend and UI
> extension expect `rancher-ai-agent` in namespace `cattle-ai-agent-system` on the same
> cluster Rancher server itself runs on — not an arbitrary downstream/tenant cluster.

## Credit

Adapted from [Edu Minguez](https://github.com/e-minguez)'s example Blueprint for the SUSE
AI Factory operator:
[`e-minguez/aif` — `rancher-ai-assistant-with-ollama-1.0.0.yaml`](https://github.com/e-minguez/aif/blob/liz-blueprint/examples/rancher-ai-assistant-with-ollama-1.0.0.yaml).
The component shape and the community-mirror workaround below are his; the model was
swapped from his `gpt-oss:20b` down to a CPU-friendly micro model for this variant.

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
| **Ollama** | `ollama` `1.55.0` (application-collection) | `cattle-ai-agent-system` | local LLM backend, CPU, preloaded with `qwen2.5:1.5b` |
| **rancher-ai-agent** | `rancher-ai-agent` `v1.0.2` (`liz-charts-obs` mirror) | `cattle-ai-agent-system` | Liz's backend — supervisor + 3 built-in agents, wired to Ollama |
| **rancher-ai-ui** | `rancher-ai-ui` `1.0.1` (`rancher-ui-plugins-http`) | `cattle-ui-plugin-system` | registers the UIPlugin that surfaces Liz's chat panel in the Rancher UI |

## Use it via the Blueprint Marketplace (recommended)

Pick **Rancher AI Assistant (Liz, Ollama)** and follow the guide: import → create the
AIWorkload in AI Factory **targeting namespace `cattle-ai-agent-system`** → reload the
Rancher UI to pick up the extension → open the AI Assistant panel and chat with the
built-in agents.

## Notes

- **Namespace matters.** This one only works if the AIWorkload lands in
  `cattle-ai-agent-system` — that's where Rancher's own AI Assistant backend looks for
  it. Each component also pins its own `targetNamespace`, so Ollama and the Liz agent
  land there (and the UI extension in `cattle-ui-plugin-system`) regardless of what the
  AIWorkload wizard is given — matching the workload namespace just keeps everything
  visibly aligned.
- **Model choice affects agent quality.** Liz's agents rely on function/tool calling;
  `qwen2.5:1.5b` is fast on CPU but tool-calling reliability at this size is worth a
  spot-check once it's running. A GPU variant with a larger model (e.g. `gpt-oss:20b`)
  would be more reliable but needs real memory headroom (~13 GiB to load) and a much
  longer first-pull.
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
