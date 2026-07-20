# VisionGPT (vLLM, GPU) — navigation hazard detection

A SUSE / vision-language derivation of
[AIS-Clemson/VisionGPT](https://github.com/AIS-Clemson/VisionGPT). It analyses a walking
video and, per sampled frame, returns a **danger score (0/1)** and a **short reason**, at a
selectable **sensitivity** (low / normal / high).

This is the **GPU / vLLM** variant: it serves **Qwen2.5-VL-3B-Instruct** with vLLM. For the
CPU version of the same model on Ollama (runs anywhere, slower), see
[`../visiongpt-ollama`](../visiongpt-ollama). The demo UI is identical between the two — only
the model endpoint differs.

Blueprint CR: [`visiongpt-vllm-1-0-0.yaml`](visiongpt-vllm-1-0-0.yaml)

## Components

| Component | Chart (App Collection) | Role |
|-----------|------------------------|------|
| **vLLM** | `vllm` `0.1.10` | serves `Qwen/Qwen2.5-VL-3B-Instruct` (serving engine + router), **GPU** |
| **VisionGPT UI** | — (local) | FastAPI + SUSE UI in [`ui/`](ui/); samples frames + calls the VLM. Runs locally. |

The vLLM chart is the SUSE repackage of the vLLM production-stack chart. The
OpenAI-compatible API is exposed on the **router** service (`vllm-router-service:80/v1`),
which the local UI reaches via port-forward.

## Requirements

- A node with a **real NVIDIA GPU** and the **GPU Operator** / device plugin.
- SUSE AI Factory operator, the `application-collection` ClusterRepo (+ credentials),
  a default StorageClass, cert-manager.

> On a simulated-GPU cluster (e.g. kwok) the pods schedule but do not actually serve —
> real inference needs a real GPU.

## Use it via the Blueprint Marketplace (recommended)

Pick **VisionGPT (vLLM, GPU)** and follow the guide: import → create the AIWorkload in AI
Factory (real GPU node; the model downloads on first start) → it starts the local UI +
port-forward to the vLLM router → analyse the bundled `walk.mp4`.

## Or run it manually

```bash
kubectl apply -f visiongpt-vllm-1-0-0.yaml             # import the Blueprint CR
# create an AIWorkload from it in the AI Factory UI (namespace <ns>); wait for vLLM Ready
kubectl -n <ns> port-forward svc/vllm-router-service 8000:80
cd ui
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
OPENAI_BASE_URL=http://localhost:8000/v1 VLM_MODEL=Qwen/Qwen2.5-VL-3B-Instruct \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

## Notes

- The `model` in each request must equal the served `modelURL`
  (`Qwen/Qwen2.5-VL-3B-Instruct`).
- Vision flags are set in the CR via `vllmConfig.extraArgs` (`--trust-remote-code`,
  `--limit-mm-per-prompt.image=2`). Verify the `containers/vllm-openai` image tag against the
  SUSE Application Collection for your environment.
- Sample video and UI configuration are the same as the Ollama variant — see
  [`../visiongpt-ollama/README.md`](../visiongpt-ollama/README.md).
```
