"""Tests for settings parsing edge cases."""

from __future__ import annotations

import pytest

from owui_tei_proxy.config import Settings


def test_k8s_injected_port_env_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kubernetes injects PORT=tcp://<svc-ip>:<port> into every pod.

    That value is not a listen port (the Dockerfile CMD pins 8000), so
    it must not crash validation — the default 8000 wins.
    """
    monkeypatch.setenv("PORT", "tcp://10.105.244.2:80")
    cfg = Settings(tei_reranker_url="http://upstream.test")
    assert cfg.port == 8000


def test_tei_proxy_port_alias_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEI_PROXY_PORT", "9000")
    cfg = Settings(tei_reranker_url="http://upstream.test")
    assert cfg.port == 9000


def test_explicit_port_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "tcp://10.0.0.1:80")
    cfg = Settings(tei_reranker_url="http://upstream.test", port=8000)
    assert cfg.port == 8000
