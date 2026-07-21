# Chest X-ray Copilot (Ollama, CPU)

The CPU variant of the Chest X-ray Copilot demo. Upload a chest X-ray, get an
analysis from a medical vision-language model served by **Ollama**, and search a
Milvus index of X-rays by **image** (similarity) or **text** (semantic) using
**BiomedCLIP** embeddings.

> ⚠️ **Demo only — not a medical device and not for clinical decision-making.**

## Flow

```
Local UI (upload/pick X-ray)
   ├── analysis:  image → Ollama (MedGemma) over OpenAI /v1/chat/completions
   └── search:    image → BiomedCLIP embedding (CPU, in UI) → Milvus
                  text  → BiomedCLIP text embedding        → Milvus  (shared space)
```

## Components

| Component | Chart (repo) | Notes |
|---|---|---|
| Ollama | `ollama` (application-collection) | CPU; serves the model at `ollama:11434`. |
| Milvus | `milvus` (application-collection) | Standalone + REST v2 proxy at `milvus:19530`. |

BiomedCLIP (MIT, 512-dim) runs in the local UI on CPU — no embedding component in
the cluster.

## Model

- **MedGemma 1.5 4B, vision-capable** via the Ollama registry
  (`dcarrascosa/medgemma-1.5-4b-it:Q4_K_M`, ~3.3 GB) — accepts image input, **no
  HuggingFace token needed** (Ollama pulls it directly). Use `:Q8_0` (5 GB) or
  `:F16` (8.6 GB) for higher fidelity.
- **Vision caveat:** only vision-capable tags work. Text-only MedGemma tags (e.g.
  `alibayram/medgemma`) fail X-ray analysis with *"this model is missing data
  required for image input"*. The UI lists whatever Ollama has pulled, so you can
  pick another Text+Image model if you prefer.
- **No LLaVA-Med here** — it has no first-class Ollama/GGUF path. Use the **Chest
  X-ray Copilot (vLLM, GPU)** blueprint for LLaVA-Med.

CPU inference is slow; the model is downloaded on first start.

## Requirements

- SUSE AI Factory operator; `application-collection` ClusterRepo (+ credentials);
  a default StorageClass; cert-manager.
- Host tools for the local UI: `python3` (the marketplace builds a venv and installs
  a CPU-only torch + open_clip + pymilvus).

## Sample X-rays

Bundled under `ui/static/samples/` (from Wikimedia Commons, for demonstration):
`normal-chest.jpg`, `lobar-pneumonia.jpg` (CC0, Mikael Häggström, M.D.),
`congestive-heart-failure.jpg`. Check each file's Wikimedia Commons page for its
exact license before reuse.
