"""FastAPI app: bridges Open WebUI's external reranker to Hugging Face TEI.

Endpoints
---------
- `POST /rerank` — Open WebUI `ExternalReranker` contract.
                  Body: `{"model", "query", "documents": [...], "top_n"}`.
                  Response: `{"results": [{"index", "relevance_score"}, ...]}`,
                  exactly one entry per input document, sorted by `index`.
- `GET /health`  — kubelet probe target. Probes upstream TEI `/health`
                  and returns 200/503.

Auth posture
------------
This proxy is designed to run on a trusted local network alongside
Open WebUI and TEI. Neither direction is authenticated:

- Inbound (`/rerank`): the proxy never reads the inbound
  `Authorization` header. OWUI's `ExternalReranker` always sends one
  (`Bearer $RAG_EXTERNAL_RERANKER_API_KEY`); the proxy ignores it.
  Access control is the deployment's job (Cilium NetworkPolicy in the
  cluster), not the app's.
- Outbound (`/rerank` on TEI): no auth header is attached. TEI on the
  in-cluster network requires none.

Error mapping for `/rerank`
--------------------------
- pydantic validation failure (missing/empty query or documents) → 400
- `len(documents) > MAX_CLIENT_BATCH_SIZE`                     → 413
- upstream non-2xx / connection error / timeout                → 502
- upstream malformed or misaligned response                    → 502
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from owui_tei_proxy.config import Settings, load_settings
from owui_tei_proxy.models import (
    RerankRequest,
    RerankResponse,
    RerankResult,
    TeiRerankItem,
    TeiRerankRequest,
    document_text,
)

logger = logging.getLogger("owui_tei_proxy")

# Endpoints we call on the upstream TEI service.
_RERANK_PATH = "/rerank"
_HEALTH_PATH = "/health"

_router = APIRouter()


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def create_app(
    settings: Settings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    Parameters
    ----------
    settings:
        Optional pre-built `Settings`. If `None`, `load_settings()` is
        called from inside the lifespan. Tests pass a `Settings`
        constructed with explicit overrides so the env is irrelevant.
    http_client:
        Optional pre-built `httpx.AsyncClient`. If `None`, one is created
        inside the lifespan. Tests pass a `MockTransport`-backed client
        to short-circuit upstream calls.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Manage the upstream httpx client lifecycle."""
        if http_client is not None:
            # Tests pass a client they own; we must not close it.
            app.state.http_client = http_client
            app.state.owns_client = False
            app.state.settings = settings or load_settings()
            try:
                yield
            finally:
                app.state.http_client = None
            return

        cfg = settings or load_settings()
        app.state.settings = cfg
        # No auth headers — TEI on a trusted in-cluster network needs none.
        client = httpx.AsyncClient(
            base_url=cfg.tei_reranker_url,
            timeout=httpx.Timeout(cfg.rerank_timeout_secs),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        app.state.http_client = client
        app.state.owns_client = True
        try:
            yield
        finally:
            await client.aclose()
            app.state.http_client = None

    app = FastAPI(
        title="owui-tei-proxy",
        version="0.1.0",
        lifespan=lifespan,
        # Hide OpenAPI by default — callers pass a pre-built Settings
        # in tests, which means docs are off; otherwise expose /docs for
        # local exploration.
        docs_url="/docs" if (settings is None) else None,
        redoc_url=None,
    )

    # Log 400s so a future request-shape drift (e.g. an Open WebUI
    # upgrade changing the reranker contract) is visible in proxy logs
    # instead of only in the caller's logs.
    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # `exc.errors()` can carry non-JSON-serialisable objects (e.g.
        # the original `ValueError` instance under `ctx['error']`).
        # FastAPI's stock handler runs the same payload through
        # `jsonable_encoder`; mirror that so the response is
        # serialisable.
        logger.warning(
            "rejected request to %s: %s",
            request.url.path,
            exc.errors(),
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": jsonable_encoder(exc.errors())},
        )

    app.include_router(_router)
    return app


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_http_client(request: Request) -> httpx.AsyncClient:
    """FastAPI dependency that yields the lifespan-managed httpx client."""
    client = getattr(request.app.state, "http_client", None)
    if client is None:  # pragma: no cover — lifespan guarantees this
        raise RuntimeError("http_client not initialised; lifespan did not run")
    return client


def get_settings(request: Request) -> Settings:
    """FastAPI dependency that yields the lifespan-managed settings."""
    cfg = getattr(request.app.state, "settings", None)
    if cfg is None:  # pragma: no cover — lifespan guarantees this
        cfg = load_settings()
        request.app.state.settings = cfg
    return cfg


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@_router.post("/rerank", response_model=RerankResponse)
async def rerank(
    body: RerankRequest,
    client: httpx.AsyncClient = Depends(get_http_client),
    settings: Settings = Depends(get_settings),
) -> RerankResponse:
    """Open WebUI `ExternalReranker` contract, bridged to TEI's native `/rerank`.

    Accepts `{"model", "query", "documents", "top_n"}` and returns
    `{"results": [{"index", "relevance_score"}, ...]}` — exactly one
    result per input document, sorted by `index`. OWUI sorts by
    `index` itself and zips scores positionally against `documents`,
    so dropping or truncating results would silently misalign scores.
    """
    if len(body.documents) > settings.max_client_batch_size:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"documents batch too large: {len(body.documents)} > "
                f"MAX_CLIENT_BATCH_SIZE={settings.max_client_batch_size}"
            ),
        )

    texts = [document_text(d) for d in body.documents]
    upstream_body = TeiRerankRequest(query=body.query, texts=texts).model_dump(mode="json")
    try:
        resp = await client.post(_RERANK_PATH, json=upstream_body)
    except httpx.HTTPError as exc:
        logger.warning("upstream connection error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="upstream unreachable",
        ) from exc

    if resp.status_code >= 400:
        logger.warning("upstream /rerank returned status %s", resp.status_code)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"upstream error (status {resp.status_code})",
        )

    payload = _safe_json(resp)
    if not isinstance(payload, list):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="upstream returned malformed rerank response",
        )

    try:
        items = [TeiRerankItem.model_validate(item) for item in payload]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="upstream returned malformed rerank entries",
        ) from exc

    # OWUI zips results positionally against documents after sorting by
    # index — a missing or duplicated index would silently misalign
    # scores, so this is a hard 502, not a best-effort fill.
    expected = set(range(len(body.documents)))
    if {item.index for item in items} != expected:
        logger.warning(
            "upstream returned %d results for %d documents",
            len(items),
            len(body.documents),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="upstream returned mismatched result count",
        )

    items.sort(key=lambda item: item.index)
    return RerankResponse(
        results=[RerankResult(index=item.index, relevance_score=item.score) for item in items]
    )


@_router.get("/health")
async def health(
    client: httpx.AsyncClient = Depends(get_http_client),
) -> JSONResponse:
    """kubelet probe target.

    Returns 200 if upstream TEI `/health` is reachable, 503 otherwise.
    """
    try:
        resp = await client.get(_HEALTH_PATH)
    except httpx.HTTPError as exc:
        logger.warning("upstream health unreachable: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "degraded", "reason": "upstream unreachable"},
        )

    if 200 <= resp.status_code < 300:
        return JSONResponse(status_code=200, content={"status": "ok"})

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "degraded", "reason": f"upstream status {resp.status_code}"},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_json(resp: httpx.Response) -> Any:
    """Return the parsed JSON body or `None` on parse failure."""
    try:
        return resp.json()
    except ValueError:
        logger.warning("upstream returned non-JSON body (status=%s)", resp.status_code)
        return None


# Module-level app for `uvicorn owui_tei_proxy.main:app`. The
# lifespan will load settings from the environment on first request.
app = create_app()
