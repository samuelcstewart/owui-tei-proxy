# owui-tei-proxy

A small HTTP service that bridges [Open WebUI](https://github.com/open-webui/open-webui)'s
external RAG reranker interface to a [Hugging Face Text Embeddings
Inference (TEI)](https://github.com/huggingface/text-embeddings-inference)
reranker. The proxy:

- Accepts `POST /rerank` requests shaped like the Open WebUI external
  reranker contract (`{"model", "query", "documents": [...], "top_n"}`).
- Internally forwards to TEI's **native** `POST /rerank`
  (`{"query", "texts": [...]}`) — TEI 1.8.x has no `/v1/rerank`.
- Returns `{"results": [{"index", "relevance_score"}, ...]}` — exactly
  one result per input document, sorted by `index`, because Open WebUI
  zips scores positionally against the documents.
- Exposes `GET /health` for liveness probes — checks that TEI is
  reachable.

## Why this exists

Open WebUI's `ExternalReranker` (see
`backend/open_webui/retrieval/models/external.py` in the OWUI repo —
the source of truth) POSTs to `RAG_EXTERNAL_RERANKER_URL` verbatim
with `{"model", "query", "documents", "top_n"}` and expects
`{"results": [{"index", "relevance_score"}, ...]}`. TEI 1.8.3 only
exposes its native `POST /rerank` with `{"query", "texts"}` →
`[{"index", "score"}, ...]`, and the Jina/Cohere-compatible
`/v1/rerank` routes are still unmerged upstream (HF PR #797). This
proxy is the shim between the two dialects.

Embeddings need no shim: TEI speaks OpenAI `/v1/embeddings` natively,
so Open WebUI can point at `http://embedding.tei.svc/v1` directly.

## Stack

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) for package management
- [ruff](https://docs.astral.sh/ruff/) for lint + format
- [ty](https://docs.astral.sh/ty/) for type checking
- FastAPI + uvicorn for the HTTP server
- httpx for upstream calls
- pydantic / pydantic-settings for config + request/response models

## Configuration

| Variable | Default | Description |
|---|---|---|
| `TEI_RERANKER_URL` | `http://localhost:8080` | Upstream TEI reranker base URL (trailing slash stripped) |
| `TEI_PROXY_PORT` / `PORT` | `8000` | Listen port hint for local runs |
| `RERANK_TIMEOUT_SECS` | `30` | Upstream request timeout (seconds) |
| `MAX_CLIENT_BATCH_SIZE` | `256` | Batches larger than this are rejected with 413. Matches the TEI deployment's `--max-client-batch-size 256` |

The proxy never validates any inbound `Authorization` header on
`/rerank`. OWUI's `ExternalReranker` sends one by default
(`Bearer $RAG_EXTERNAL_RERANKER_API_KEY`) and the proxy simply ignores
it. This proxy is designed to run on a trusted local network alongside
Open WebUI and TEI — access control is enforced by the cluster's
Cilium NetworkPolicy, not by the app. No outbound auth either: TEI on
the in-cluster network requires none.

## Local development

```bash
uv sync                              # install all deps
uv run pytest                        # run unit tests
uv run ruff check                    # lint
uv run ruff format                   # format
uv run ty check                      # type check
uv run uvicorn owui_tei_proxy.main:app --reload --port 8000
```

## Docker

```bash
docker compose up --build            # brings up TEI cpu-1.8.3 + this proxy
curl -X POST http://localhost:8000/rerank \
  -H 'Content-Type: application/json' \
  -d '{"model":"reranker","query":"capital of France","documents":["Paris is the capital of France.","Berlin is the capital of Germany."],"top_n":2}' | jq
```

For a standalone build of the proxy image only:

```bash
poe build                            # multi-arch (amd64+arm64), --load (Docker 29+)
```

Integration tests against the real TEI container:

```bash
poe integration-test                 # compose up → pytest -m integration → compose down
```

## API

### `POST /rerank`

Open WebUI's `ExternalReranker` sends `POST` to `RAG_EXTERNAL_RERANKER_URL`
with `{"model", "query", "documents": [str, ...], "top_n": len(documents)}`.
The proxy accepts that exact shape (dict entries with `text` /
`page_content` keys are also tolerated) and returns one result per
document — never truncated to `top_n`:

```json
// Request (OWUI dialect)
{
  "model": "reranker",
  "query": "capital of France",
  "documents": ["Paris is the capital of France.", "Berlin is in Germany."],
  "top_n": 2
}
// Response
{
  "results": [
    {"index": 0, "relevance_score": 0.98},
    {"index": 1, "relevance_score": 0.01}
  ]
}
```

Internally the proxy forwards to TEI as
`{"query": "...", "texts": ["...", "..."]}` (no `model`, no `top_n`)
and maps TEI's `[{"index", "score"}]` to OWUI's
`{"results": [{"index", "relevance_score"}]}` sorted by `index`.

Errors: invalid input → 400; batch over `MAX_CLIENT_BATCH_SIZE` → 413;
upstream failure → 502.

### `GET /health`

```json
{"status": "ok"}        // 200 if upstream TEI /health is reachable
{"status": "degraded"}  // 503 if upstream is unreachable
```

## Publishing

A GitHub Actions workflow builds and pushes a multi-arch image to
GHCR. The same workflow also runs on every PR (build + tests, no
push) as a Dockerfile regression gate:

```
ghcr.io/samuelcstewart/owui-tei-proxy
```

Tag scheme:

- main pushes → `:main`, `:<short-sha>`, `:latest`
- `v0.1.0` tag → `:v0.1.0`, `:v0.1`, `:v0`, `:<short-sha>`

PR builds run the same `buildx` invocation with `push: false`, so PRs
get a build verification (and the test suite) but never produce a
throwaway GHCR image.

Pullable directly:

```bash
docker pull ghcr.io/samuelcstewart/owui-tei-proxy:latest
```

## License

MIT.
