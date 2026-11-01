"""Integration tests against a real TEI reranker container.

These tests are NOT run by `poe test` (the default test task
excludes the `integration` marker). They're invoked by
`poe integration-test`, which:

1. `docker compose up -d --build` — starts TEI cpu-1.8.3
   (cross-encoder/ms-marco-MiniLM-L-6-v2) + this proxy on a shared
   network.
2. Waits for `GET $PROXY_HEALTH_URL` to return 2xx (TEI model load
   on CPU takes 1-3 minutes on first pull).
3. Runs `pytest -m integration -v --tb=short`.
4. `docker compose down` — tears the stack down (via a shell `trap`).

The proxy is reachable at http://localhost:8000 (mapped from the
compose port forward). TEI is reachable inside the compose network
on port 80; the proxy talks to it via that internal DNS.

Why a separate test file
------------------------
The unit tests in `test_rerank.py` / `test_health.py` use
`httpx.MockTransport` to inject canned responses. That's fast and
deterministic, but it doesn't catch:

- A shape drift between what TEI returns and what the proxy expects.
- A real network error / connection-refused path.
- That ms-marco-MiniLM-L-6-v2 actually scores a relevant document
  above filler (the whole point of the reranker).

Marking convention
------------------
Every test in this file is decorated with
`@pytest.mark.integration` so the default `poe test` run skips it.
The matching `[tool.pytest.ini_options].markers` entry in
`pyproject.toml` registers the marker so `--strict-markers` is
satisfied.

URL under test
--------------
The proxy is published to localhost:8000 by docker compose. The
`PROXY_URL` env var exists so CI / dockerized runners can override
the target (e.g. `PROXY_URL=http://proxy:8000`).
"""

from __future__ import annotations

import os

import httpx
import pytest

# Reuse the proxy's expected port forward (set by docker compose).
PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8000")

QUERY = "What is the capital of France?"
DOCUMENTS = [
    "Paris is the capital and most populous city of France.",
    "Berlin is the capital of Germany.",
    "The Eiffel Tower is a wrought-iron lattice tower in Paris.",
    "France is a country in Western Europe.",
]


def _client() -> httpx.Client:
    """Plain sync httpx client — no need for async, the proxy is HTTP/1.1."""
    return httpx.Client(base_url=PROXY_URL, timeout=httpx.Timeout(120.0))


@pytest.mark.integration
def test_health_endpoint_reports_ok_against_real_tei() -> None:
    """`/health` returns 200 + `{"status": "ok"}` once TEI is up."""
    with _client() as c:
        r = c.get("/health")

    assert r.status_code == 200, f"unexpected status: {r.status_code} body={r.text!r}"
    assert r.json() == {"status": "ok"}


@pytest.mark.integration
def test_rerank_relevant_document_outscores_filler() -> None:
    """ms-marco-MiniLM-L-6-v2 must score the on-topic document highest.

    Exercises the full path: POST /rerank (OWUI dialect) → proxy →
    TEI native /rerank → score mapping → OWUI-shaped response.
    """
    with _client() as c:
        r = c.post(
            "/rerank",
            json={
                "model": "reranker",
                "query": QUERY,
                "documents": DOCUMENTS,
                "top_n": len(DOCUMENTS),
            },
        )

    assert r.status_code == 200, f"unexpected status: {r.status_code} body={r.text!r}"
    results = r.json()["results"]

    # Exactly one result per input document — OWUI zips positionally,
    # so dropping/truncating would silently misalign scores.
    assert len(results) == len(DOCUMENTS)
    assert sorted(x["index"] for x in results) == list(range(len(DOCUMENTS)))

    by_index = {x["index"]: x["relevance_score"] for x in results}
    # Document 0 is the direct answer; it must outscore every filler.
    for i in (1, 2, 3):
        assert by_index[0] > by_index[i], (
            f"expected doc 0 ({by_index[0]}) to outscore doc {i} ({by_index[i]})"
        )


@pytest.mark.integration
def test_rerank_rejects_empty_documents_against_real_proxy() -> None:
    """Smoke test that the running proxy enforces validation (400)."""
    with _client() as c:
        r = c.post("/rerank", json={"query": QUERY, "documents": []})

    assert r.status_code == 400
