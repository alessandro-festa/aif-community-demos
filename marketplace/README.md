# Blueprint Marketplace

A local, **single-binary**, SUSE-styled launcher for SUSE AI Factory blueprints. It:

- pulls blueprint content — **including each blueprint's local frontend** — from a git repo;
- lists the blueprints in a SUSE-styled **catalog**;
- lets you pick a **target cluster** from your kubeconfig contexts (showing AI Factory
  readiness);
- **imports** a selected blueprint into that cluster via `kubectl apply` (the Blueprint CR);
- walks you through a **step-by-step guided demo**, including **starting the blueprint's
  local frontend + `kubectl port-forward`s** and telling you what to open.

The web UI is embedded in the binary (`go:embed`); the blueprints (and their frontends) are
pulled from git at runtime, so new blueprints appear without rebuilding.

## Host prerequisites

The binary orchestrates these host tools (must be on `PATH`):

- **kubectl** — cluster ops (list contexts, prereq checks, apply, port-forward). Uses your
  existing kubeconfig.
- **git** — pull the blueprints repo (not needed with `--dir`).
- **python3** — run a blueprint's FastAPI frontend in a per-blueprint virtualenv.

## Build & run

```bash
cd marketplace
go build -o bpm .

# Dev: use the local blueprints/ folder (no git):
./bpm --dir ../blueprints

# Normal: pull blueprints from git (configurable later in Settings):
./bpm --repo https://github.com/<you>/<repo>.git --ref main

# then open the printed URL (default http://127.0.0.1:8900)
```

Flags (all optional; they override saved settings):

| Flag | Default | Meaning |
|------|---------|---------|
| `--addr` | `127.0.0.1:8900` | listen address |
| `--repo` | (from settings) | blueprints git repo URL |
| `--ref` | `main` | git ref / branch |
| `--context` | host current-context | target kube context |
| `--dir` | — | use a local `blueprints/` dir instead of git (disables git in Settings) |

Settings (git repo, ref, target cluster) are also editable in the **Settings** page and
persisted to `~/.suse-bp-marketplace/config.yaml`. Per-blueprint virtualenvs are cached
under `~/.suse-bp-marketplace/venvs/`.

## How a blueprint is described

Each blueprint folder in the repo carries a `marketplace.yaml` with the catalog card,
prerequisite checks, the local-frontend definition, and the guided-demo steps. See
`blueprints/airflow-genai-rag/marketplace.yaml` and `blueprints/suse-vss/marketplace.yaml`.

Guided-demo `action` types: `import` (kubectl apply the CR), `namespace-input` (collect the
AIWorkload namespace), `start-frontend` / `stop-frontend` (venv + port-forwards + uvicorn),
`open-url`, or none (instructions only).

## Layout

```
marketplace/
  main.go                 # flags, config, git sync (or --dir), wiring, graceful shutdown
  internal/
    config/               # ~/.suse-bp-marketplace/config.yaml (repo, ref, context)
    catalog/              # scan blueprints/<id>/marketplace.yaml
    gitrepo/              # clone/pull the blueprints repo
    kube/                 # kubectl: contexts, AI Factory readiness, prereqs, apply
    proc/                 # subprocess registry: port-forwards + uvicorn + log hub
    server/               # REST + SSE handlers, serves embedded web/
  web/                    # embedded static UI (index.html, app.js, marketplace.css, font)
```

## Notes

- **Import only.** The marketplace applies the Blueprint CR; you then create an AIWorkload
  in the SUSE AI Factory UI (the guide points you there and collects the namespace you pick).
- Frontends run via the `python` runtime (venv + uvicorn on the pulled source). The
  `marketplace.yaml` schema anticipates a future `runtime: container` option.
- On shutdown (Ctrl-C) the binary reaps all child processes (port-forwards, uvicorn).
