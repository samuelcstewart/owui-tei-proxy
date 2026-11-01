"""Settings for the proxy.

Env vars (no prefix, case-insensitive):

- `TEI_RERANKER_URL`      — upstream TEI base URL (default http://localhost:8080)
- `TEI_PROXY_PORT` / `PORT` — listen port hint for local runs (default 8000)
- `RERANK_TIMEOUT_SECS`   — httpx timeout for upstream `/rerank` calls (default 30)
- `MAX_CLIENT_BATCH_SIZE` — reject document batches larger than this with
  413 (default 256, matching the TEI deployment's
  `--max-client-batch-size 256`)

The proxy never validates any inbound `Authorization` header. It is
designed to run on a trusted local network next to Open WebUI and TEI;
neither side enforces auth on the other.
"""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from env vars.

    The proxy does no inbound auth and no outbound auth — TEI on a
    trusted in-cluster network needs neither. If the deployment ever
    crosses a trust boundary, that's the deployment's job to enforce
    (NetworkPolicy, sidecar, ingress) — not the proxy's.
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=None,  # never read from disk
        case_sensitive=False,
        extra="ignore",
    )

    tei_reranker_url: str = Field(
        default="http://localhost:8080",
        description="Upstream TEI base URL (reranker service).",
    )

    port: int = Field(
        default=8000,
        gt=0,
        le=65535,
        validation_alias=AliasChoices("TEI_PROXY_PORT", "PORT"),
        description="Listen port hint for local (non-container) runs.",
    )

    rerank_timeout_secs: float = Field(
        default=30.0,
        gt=0,
        description="httpx request timeout for upstream TEI calls (seconds).",
    )

    max_client_batch_size: int = Field(
        default=256,
        ge=1,
        description=(
            "Maximum number of documents accepted in one /rerank call. "
            "Larger batches are rejected with 413. Matches the TEI "
            "deployment's --max-client-batch-size 256."
        ),
    )

    @field_validator("tei_reranker_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


def load_settings(**overrides: Any) -> Settings:
    """Construct a `Settings` instance.

    `overrides` are passed as keyword arguments and take precedence
    over env vars. Used by tests to inject config without touching
    the real environment.
    """
    return Settings(**overrides)
