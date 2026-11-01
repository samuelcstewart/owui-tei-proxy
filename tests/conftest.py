"""Shared test fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from owui_tei_proxy.config import Settings
from owui_tei_proxy.main import create_app


@pytest.fixture
def settings() -> Settings:
    """Settings with a fixed test upstream so the env is irrelevant."""
    return Settings(
        tei_reranker_url="http://upstream.test",
        rerank_timeout_secs=5.0,
        max_client_batch_size=256,
    )


@pytest.fixture
def make_client():  # type: ignore[no-untyped-def]
    """Build a TestClient wired to a `MockTransport`-backed httpx client.

    Usage::

        def test_x(make_client):
            def handler(request):
                return httpx.Response(200, json=[{"index": 0, "score": 0.9}])
            with make_client(handler) as client:
                r = client.post("/rerank", json={"query": "q", "documents": ["d"]})
                assert r.status_code == 200

    Optional kwargs are forwarded to `Settings`.
    """
    upstreams: list[httpx.AsyncClient] = []

    def _factory(
        handler: Callable[[httpx.Request], httpx.Response],
        **settings_overrides: Any,
    ) -> TestClient:
        transport = httpx.MockTransport(handler)
        defaults: dict[str, Any] = {
            "tei_reranker_url": "http://upstream.test",
            "rerank_timeout_secs": 5.0,
            "max_client_batch_size": 256,
        }
        cfg = Settings(**(defaults | settings_overrides))
        upstream = httpx.AsyncClient(
            transport=transport,
            base_url="http://upstream.test",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        upstreams.append(upstream)
        app = create_app(settings=cfg, http_client=upstream)
        return TestClient(app)

    yield _factory

    # Cleanup: close any upstream clients we created. TestClient closes
    # the FastAPI lifespan, which leaves the client alone (owns_client=False).
    # httpx.AsyncClient only exposes async `aclose()`, so we drive it
    # via a fresh event loop in the fixture teardown. MockTransport
    # doesn't hold sockets, but we close anyway to silence resource
    # warnings.
    for u in upstreams:
        if not u.is_closed:
            asyncio.run(u.aclose())


def make_tei_rerank_response(
    *,
    scores: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Build a TEI native `/rerank` response body: `[{"index", "score"}, ...]`.

    `scores[i]` is the score for input document `i`.
    """
    if scores is None:
        scores = [0.9]
    return [{"index": i, "score": s} for i, s in enumerate(scores)]
