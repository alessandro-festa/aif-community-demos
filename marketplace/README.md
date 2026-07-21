# Blueprint Marketplace

A local, **single-binary**, SUSE-styled launcher for SUSE AI Factory blueprints (the UI is
branded **"SUSE AI Factory Community Blueprints"**). It:

- pulls blueprint content — **including each blueprint's local frontend** — from a git repo;
- lists the blueprints in a SUSE-styled **catalog**;
- lets you pick a **target cluster** from your kubeconfig contexts (showing AI Factory
  readiness);
- **imports** a selected blueprint into that cluster via `kubectl apply` (the Blueprint CR);
- walks you through a **step-by-step guided demo**, including **starting the blueprint's
  local frontend + `kubectl port-forward`s** and telling you what to open;
- gives you **on-demand access to in-cluster component UIs** (port-forward once the service is
  ready) and a **Running** view to manage everything you've started.

The web UI is embedded in the binary (`go:embed`); the blueprints (and their frontends) are
pulled from git at runtime, so new blueprints appear without rebuilding.

> The blueprints are community demos — **totally unsupported, for demo purposes only.**

## Requirements

- **Operating system:** Linux or macOS, `x86-64` (amd64) or `arm64`. **Windows is not supported**
  — the process supervisor relies on POSIX process-group signals.
- **Host tools on `PATH`** (the binary orchestrates these):
  - **kubectl** — all cluster ops (list contexts, AI Factory readiness + prerequisite checks,
    `apply`, `port-forward`). Uses your existing kubeconfig.
  - **git** — pull the blueprints repo (not needed with `--dir`).
  - **python3** — run a blueprint's FastAPI frontend in a per-blueprint virtualenv.
    `pip` and `uvicorn` are provisioned automatically inside that venv.
- **A working kubeconfig** with access to the cluster(s) you want to target.
- **To build from source:** **Go 1.23+** (the only module dependency is `gopkg.in/yaml.v3`).

## Install

Prebuilt binaries are published as a GitHub Release on
[`alessandro-festa/aif-community-demos`](https://github.com/alessandro-festa/aif-community-demos/releases).

```bash
# pick the asset for your platform:
#   bpm-linux-amd64   bpm-linux-arm64   bpm-darwin-arm64   bpm-darwin-amd64
curl -LO https://github.com/alessandro-festa/aif-community-demos/releases/download/v0.3.0/bpm-linux-amd64
curl -LO https://github.com/alessandro-festa/aif-community-demos/releases/download/v0.3.0/SHA256SUMS

# verify, then make it runnable:
shasum -a 256 -c SHA256SUMS --ignore-missing
chmod +x bpm-linux-amd64
mv bpm-linux-amd64 /usr/local/bin/bpm

bpm            # then open the printed URL (default http://127.0.0.1:8900)
```

By default `bpm` pulls blueprints from `https://github.com/alessandro-festa/aif-community-demos.git`
(`main`); change this any time in **Settings**.

## Build & run

```bash
cd marketplace
go build -o bpm .

# Dev: use the local blueprints/ folder (no git):
./bpm --dir ../blueprints

# Normal: pull blueprints from git (default repo is the aif-community-demos one above):
./bpm --repo https://github.com/<you>/<repo>.git --ref main

# then open the printed URL (default http://127.0.0.1:8900)
```

Flags (all optional; they override saved settings):

| Flag | Default | Meaning |
|------|---------|---------|
| `--addr` | `127.0.0.1:8900` | listen address |
| `--repo` | (from settings; falls back to the aif-community-demos repo) | blueprints git repo URL |
| `--ref` | `main` | git ref / branch |
| `--context` | host current-context | target kube context |
| `--dir` | — | use a local `blueprints/` dir instead of git (disables git in Settings) |

Settings (git repo, ref, target cluster) are also editable in the **Settings** page and
persisted to `~/.suse-bp-marketplace/config.yaml`. Per-blueprint virtualenvs are cached
under `~/.suse-bp-marketplace/venvs/`.

## Features

- **Catalog** — cards from each blueprint's `marketplace.yaml` (name, description, category, tags).
- **Cluster picker** — target-context dropdown from your kubeconfig; each context is flagged with
  its **AI Factory readiness** (presence of the `blueprints.ai-factory.suse.com` CRD).
- **Prerequisite checks** — per-blueprint checks run against the target context. Kinds:
  `crd`, `namespace`, `default-storageclass`.
- **Guided demo** — a stepper with rendered instructions and per-step actions (see below),
  streaming `kubectl apply` / frontend logs live over SSE.
- **Local frontends** — start/stop a blueprint's FastAPI frontend in a cached venv, with
  supervised `kubectl port-forward`s (auto-restart on drop) and a `uvicorn` server.
- **Component UIs** — open an **in-cluster component's UI on demand**: the button stays disabled
  until the component's service reports ready endpoints, then a port-forward is started and the
  URL opened.
- **Running** view — a single list of everything you've started (local frontends +
  component-UI port-forwards) with **Stop** / **Stop all** and a live "running" badge.
- **Import wizard** — a blueprint may declare an `importWizard`; the import step then
  shows a checklist whose selected options are deep-merged into a target component's
  Helm values in the Blueprint CR before `kubectl apply` (e.g. choosing LiteLLM
  guardrails). Blueprints without a wizard import unchanged.
- **Dark / light theme** toggle (persisted).

## How a blueprint is described

Each blueprint folder in the repo carries a `marketplace.yaml` with the catalog card,
prerequisite checks, the local-frontend definition, any in-cluster **component UIs**, and the
guided-demo steps. See `blueprints/airflow-genai-rag/marketplace.yaml` and
`blueprints/suse-vss/marketplace.yaml`.

Guided-demo `action` types: `import` (kubectl apply the CR), `namespace-input` (collect the
AIWorkload namespace), `start-frontend` / `stop-frontend` (venv + port-forwards + uvicorn),
`open-url`, or none (instructions only).

## HTTP API

The embedded UI talks to these endpoints (SSE where a live log stream is produced):

| Endpoint | Purpose |
|----------|---------|
| `GET /api/contexts` | kube contexts + AI Factory readiness |
| `GET /api/settings`, `PUT /api/settings` | read / update config (PUT resyncs git on repo/ref change) |
| `GET /api/catalog` | list blueprints |
| `GET /api/blueprints/{id}/prereqs` | run prerequisite checks |
| `POST /api/blueprints/{id}/import` *(SSE)* | `kubectl apply` the Blueprint CR |
| `POST /api/blueprints/{id}/frontend/start` *(SSE)* / `.../frontend/stop` | local frontend lifecycle |
| `POST /api/blueprints/{id}/service-status` | component service readiness |
| `POST /api/blueprints/{id}/component-ui/start` / `.../component-ui/stop` | in-cluster component-UI port-forward |
| `GET /api/processes` | unified list of running frontends + forwards |

## Layout

```
marketplace/
  main.go                 # flags, config, git sync (or --dir), wiring, graceful shutdown
  internal/
    config/               # ~/.suse-bp-marketplace/config.yaml (repo, ref, context)
    catalog/              # scan blueprints/<id>/marketplace.yaml
    gitrepo/              # clone/pull the blueprints repo
    kube/                 # kubectl: contexts, AI Factory readiness, prereqs, service readiness, apply
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
