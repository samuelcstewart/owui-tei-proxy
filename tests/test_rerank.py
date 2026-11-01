"""Tests for `POST /rerank`."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from tests.conftest import make_tei_rerank_response


def test_rerank_happy_path_maps_and_sorts(make_client):  # type: ignore[no-untyped-def]
    """Upstream `[{"index","score"}]` → `{"results":[{"index","relevance_score"}]}`, sorted by index.

    The upstream is allowed to return entries out of order; the proxy
    must sort by `index` so OWUI's positional zip stays aligned.
    """
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/rerank"
        captured["body"] = json.loads(request.content)
        # Deliberately out of order.
        return httpx.Response(200, json=[{"index": 1, "score": 0.2}, {"index": 0, "score": 0.9}])

    with make_client(handler) as client:
        r = client.post(
            "/rerank",
            json={
                "model": "reranker",
                "query": "capital of France",
                "documents": ["Paris is the capital of France.", "Berlin is in Germany."],
                "top_n": 2,
            },
        )

    assert r.status_code == 200
    assert r.json() == {
        "results": [
            {"index": 0, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.2},
        ]
    }
    # The upstream call must be TEI's native shape — query + texts only.
    # NO `model`, NO `top_n`, NO `documents`.
    assert captured["body"] == {
        "query": "capital of France",
        "texts": ["Paris is the capital of France.", "Berlin is in Germany."],
    }


def test_rerank_returns_one_result_per_document_even_if_top_n_smaller(make_client):  # type: ignore[no-untyped-def]
    """OWUI sends `top_n = len(documents)` but zips positionally — never truncate."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "top_n" not in body and "model" not in body
        return httpx.Response(200, json=make_tei_rerank_response(scores=[0.1, 0.9, 0.5]))

    with make_client(handler) as client:
        r = client.post(
            "/rerank",
            json={
                "model": "reranker",
                "query": "q",
                "documents": ["a", "b", "c"],
                "top_n": 1,  # adversarial: OWUI always sends len(documents), but be safe
            },
        )

    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 3
    assert [x["index"] for x in results] == [0, 1, 2]
    assert [x["relevance_score"] for x in results] == [0.1, 0.9, 0.5]


def test_rerank_tolerates_dict_documents(make_client):  # type: ignore[no-untyped-def]
    """Dict entries with `text` / `page_content` are unwrapped before forwarding."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"query": "q", "texts": ["hello", "world"]}
        return httpx.Response(200, json=make_tei_rerank_response(scores=[0.5, 0.7]))

    with make_client(handler) as client:
        r = client.post(
            "/rerank",
            json={
                "query": "q",
                "documents": [{"text": "hello"}, {"page_content": "world"}],
            },
        )

    assert r.status_code == 200
    assert len(r.json()["results"]) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing query + documents
        {"query": "q"},  # missing documents
        {"documents": ["a"]},  # missing query
        {"query": "", "documents": ["a"]},  # empty query
        {"query": "   ", "documents": ["a"]},  # whitespace-only query
        {"query": "q", "documents": []},  # empty documents
        {"query": "q", "documents": [{"foo": "bar"}]},  # dict without text/page_content
    ],
)
def test_rerank_rejects_invalid_requests_with_400(make_client, payload):  # type: ignore[no-untyped-def]
    """Missing/empty query or documents → 400 (not 422), upstream never called."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("upstream should not be called")

    with make_client(handler) as client:
        r = client.post("/rerank", json=payload)

    assert r.status_code == 400
    assert "detail" in r.json()


def test_rerank_logs_400_validation_errors(  # type: ignore[no-untyped-def]
    make_client, caplog: pytest.LogCaptureFixture
) -> None:
    """400s are logged so request-shape drift is visible in proxy logs."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("upstream should not be called")

    with (
        make_client(handler) as client,
        caplog.at_level(logging.WARNING, logger="owui_tei_proxy"),
    ):
        r = client.post("/rerank", json={"documents": ["a"]})

    assert r.status_code == 400
    assert any("rejected request to /rerank" in rec.message for rec in caplog.records)


def test_rerank_rejects_oversized_batch_with_413(make_client):  # type: ignore[no-untyped-def]
    """`len(documents) > MAX_CLIENT_BATCH_SIZE` → 413, upstream never called."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("upstream should not be called")

    with make_client(handler, max_client_batch_size=4) as client:
        r = client.post("/rerank", json={"query": "q", "documents": ["a", "b", "c", "d", "e"]})

    assert r.status_code == 413
    assert "MAX_CLIENT_BATCH_SIZE=4" in r.json()["detail"]


def test_rerank_accepts_batch_at_exactly_max(make_client):  # type: ignore[no-untyped-def]
    """A batch of exactly MAX_CLIENT_BATCH_SIZE is accepted (boundary)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_tei_rerank_response(scores=[0.1] * 4))

    with make_client(handler, max_client_batch_size=4) as client:
        r = client.post("/rerank", json={"query": "q", "documents": ["a", "b", "c", "d"]})

    assert r.status_code == 200
    assert len(r.json()["results"]) == 4


@pytest.mark.parametrize("upstream_status", [400, 401, 403, 422, 500, 502, 503, 504])
def test_rerank_maps_upstream_error_status_to_502(  # type: ignore[no-untyped-def]
    make_client, upstream_status: int
) -> None:
    """Any upstream non-2xx collapses to 502 with a clear body."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(upstream_status, json={"error": "internal"})

    with make_client(handler) as client:
        r = client.post("/rerank", json={"query": "q", "documents": ["a"]})

    assert r.status_code == 502
    assert f"upstream error (status {upstream_status})" == r.json()["detail"]


def test_rerank_maps_timeout_to_502(make_client):  # type: ignore[no-untyped-def]
    """httpx timeout → 502 `upstream unreachable`."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with make_client(handler) as client:
        r = client.post("/rerank", json={"query": "q", "documents": ["a"]})

    assert r.status_code == 502
    assert r.json() == {"detail": "upstream unreachable"}


def test_rerank_maps_connection_error_to_502(make_client):  # type: ignore[no-untyped-def]
    """httpx transport errors (connection refused, DNS) → 502."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with make_client(handler) as client:
        r = client.post("/rerank", json={"query": "q", "documents": ["a"]})

    assert r.status_code == 502
    assert r.json() == {"detail": "upstream unreachable"}


def test_rerank_handles_non_json_upstream_body(make_client):  # type: ignore[no-untyped-def]
    """Upstream returns an HTML error page with 200 → 502 (malformed)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>")

    with make_client(handler) as client:
        r = client.post("/rerank", json={"query": "q", "documents": ["a"]})

    assert r.status_code == 502
    assert r.json() == {"detail": "upstream returned malformed rerank response"}


def test_rerank_handles_malformed_entries(make_client):  # type: ignore[no-untyped-def]
    """A response entry missing `score` → 502."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"index": 0}])

    with make_client(handler) as client:
        r = client.post("/rerank", json={"query": "q", "documents": ["a"]})

    assert r.status_code == 502
    assert r.json() == {"detail": "upstream returned malformed rerank entries"}


def test_rerank_handles_mismatched_result_count(make_client):  # type: ignore[no-untyped-def]
    """Upstream returns fewer/more results than documents → 502 (would misalign scores)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_tei_rerank_response(scores=[0.9]))  # 1 result, 2 docs

    with make_client(handler) as client:
        r = client.post("/rerank", json={"query": "q", "documents": ["a", "b"]})

    assert r.status_code == 502
    assert r.json() == {"detail": "upstream returned mismatched result count"}


def test_rerank_handles_duplicate_index(make_client):  # type: ignore[no-untyped-def]
    """A duplicated index would misalign OWUI's positional zip → 502."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"index": 0, "score": 0.9}, {"index": 0, "score": 0.1}])

    with make_client(handler) as client:
        r = client.post("/rerank", json={"query": "q", "documents": ["a", "b"]})

    assert r.status_code == 502
    assert r.json() == {"detail": "upstream returned mismatched result count"}


# ---------------------------------------------------------------------------
# Auth posture: no client auth
# ---------------------------------------------------------------------------


def test_rerank_ignores_inbound_authorization_header(make_client):  # type: ignore[no-untyped-def]
    """Inbound `Authorization` is never validated; any value is accepted.

    OWUI's `ExternalReranker` always sends `Bearer $RAG_EXTERNAL_RERANKER_API_KEY`;
    the proxy ignores it. Access control is enforced by the cluster's
    Cilium NetworkPolicy, not by the app.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=make_tei_rerank_response(scores=[0.9]))

    with make_client(handler) as client:
        r = client.post(
            "/rerank",
            json={"query": "q", "documents": ["a"]},
            headers={"Authorization": "Bearer anything-goes"},
        )

    assert r.status_code == 200


def test_rerank_sends_no_authorization_upstream(make_client):  # type: ignore[no-untyped-def]
    """The upstream TEI call carries no Authorization header."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers, (
            f"upstream should not receive Authorization header; got {request.headers.get('Authorization')!r}"
        )
        return httpx.Response(200, json=make_tei_rerank_response(scores=[0.9]))

    with make_client(handler) as client:
        r = client.post("/rerank", json={"query": "q", "documents": ["a"]})

    assert r.status_code == 200
