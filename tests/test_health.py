"""Tests for `GET /health`."""

from __future__ import annotations

import httpx


def test_health_ok_when_upstream_reachable(make_client):  # type: ignore[no-untyped-def]
    """Upstream 2xx on `/health` → 200 with `{"status": "ok"}`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok"})

    with make_client(handler) as client:
        r = client.get("/health")

    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_degraded_when_upstream_5xx(make_client):  # type: ignore[no-untyped-def]
    """Upstream 5xx on `/health` → 503 with `{"status": "degraded"}`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    with make_client(handler) as client:
        r = client.get("/health")

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert "upstream status 503" in body["reason"]


def test_health_degraded_when_upstream_unreachable(make_client):  # type: ignore[no-untyped-def]
    """Transport error on `/health` → 503 with `reason: upstream unreachable`."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with make_client(handler) as client:
        r = client.get("/health")

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["reason"] == "upstream unreachable"
