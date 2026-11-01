# Contributing / agent guidance

Guidance for AI agents and contributors working in this repo.
(Shipped as `CONTRIBUTING.md` — same content the project template keeps
in `AGENTS.md`.)

## Scope

This repo is the **source of truth for the `owui-tei-proxy` container
image**. The image is consumed by anything that wants to feed Open WebUI
an external RAG reranker backed by Hugging Face TEI. Changes here should
be small, reviewable, and focused on the proxy's HTTP contract with Open
WebUI and TEI. Do not redesign the service or refactor unrelated code.

## Conventions

- **Language**: Python 3.14+, type-checked with `ty`.
- **Linting**: `ruff check` + `ruff format`. No warnings allowed.
- **Tests**: `pytest`. New endpoints or error paths need tests.
- **Commits**: conventional commits (`feat:`, `fix:`, `build:`, etc.).
  52/72 rule. Rebase only — no merge commits.
- **Dependency updates**: edit `pyproject.toml`. The Dockerfile runs
  `uv sync --frozen` against the committed `uv.lock`, so adding a
  runtime dep requires `uv lock` to regenerate the lockfile.

## Open WebUI contract

The proxy implements the Open WebUI external RAG reranker contract.
That contract is set by OWUI's `ExternalReranker.predict` (see
`backend/open_webui/retrieval/models/external.py` in the OWUI
repo) — when in doubt, that source is the source of truth, not this
repo's README.

`POST /rerank` (OWUI POSTs to `RAG_EXTERNAL_RERANKER_URL` verbatim):

- Request: `{"model": str, "query": str, "documents": [str, ...],
  "top_n": len(documents)}`.
- Response: `{"results": [{"index": int, "relevance_score": float}, ...]}`.
- OWUI sorts `results` by `index` and **zips the scores positionally
  against `documents`** — so the proxy MUST return exactly one result
  per input document (never drop, never truncate to `top_n`).
- The proxy does not validate any inbound `Authorization` header.
  OWUI's `ExternalReranker` sends one (its
  `RAG_EXTERNAL_RERANKER_API_KEY`); the proxy ignores it.

Upstream contract (TEI 1.8.x, native route — there is no `/v1/rerank`):

- `POST {TEI_RERANKER_URL}/rerank` with `{"query": str,
  "texts": [str, ...]}` → `[{"index": int, "score": float}, ...]`.
- The proxy forwards `query` + `texts` only — never `model`, never
  `top_n`, never `documents`.

## Auth posture

The proxy intentionally does no auth validation:

- Inbound: no dependency reads `Authorization`. Any header (or no
  header) is accepted on `/rerank` and `/health`.
- Outbound: the lifespan attaches no `Authorization` header to
  upstream TEI calls.

This is a deliberate trust posture for "next to OWUI on a local
network", not an oversight. In the homelab cluster, access to the
proxy is enforced by Cilium NetworkPolicy, not by the app. If the
deployment ever crosses a trust boundary, that's the deployment's job
to enforce (NetworkPolicy, sidecar, ingress) — not the proxy's.

Changes to this contract are breaking for the OWUI side. Bump the
minor version in `pyproject.toml` and call out the change in the PR
description.

## Build

The image is built with buildx. The bundled `docker-compose.yml` does
this for local dev:

```
docker compose build
```

For a multi-arch (`linux/amd64` + `linux/arm64`) image loaded into
the local Docker daemon:

```
poe build
```

Requires Docker 29+ with the containerd image store (snapshotter)
enabled. That's what makes `buildx build --load` accept a
multi-platform build; on classic single-platform storage you'll get
an error.

## Publish

A GitHub Actions workflow at `.github/workflows/ci.yml` runs on every
push to `main`, every `v*.*.*` tag, and every pull request against
`main`, and on manual dispatch. Two jobs:

- `test` — `uv sync` + `uv run poe check` (ruff + format-check + ty +
  pytest). Runs first; the build job depends on it.
- `build` — multi-arch (linux/amd64 + linux/arm64) `buildx build`.
  On `push` events, pushes the result to GHCR. On `pull_request`
  events, builds for verification only (`push: false`).

The published image lives at:

```
ghcr.io/samuelcstewart/owui-tei-proxy
```

Tag scheme on push events:

- main push → `:main`, `:<short-sha>`, `:latest`
- `v0.1.0` tag → `:v0.1.0`, `:v0.1`, `:v0`, `:<short-sha>`

Visibility inherits from the repo (public). The `GITHUB_TOKEN` in the
runner has `packages: write` via the workflow's `permissions` block.

## What this repo is NOT

- Not a deployment repo. Bring the image up however you like
  (`docker run`, compose, Kubernetes, etc.) and point
  `TEI_RERANKER_URL` at your TEI reranker service.
- Not a TEI config repo. The proxy talks to whatever
  `TEI_RERANKER_URL` points at; pinning the TEI version/model is
  downstream of this project.
- Not an embedding proxy. TEI speaks OpenAI `/v1/embeddings`
  natively — point Open WebUI's embedding URL at TEI directly. This
  repo deliberately has no embedding routes.
- Not a test target for Open WebUI itself. The proxy just satisfies
  Open WebUI's external reranker contract.
