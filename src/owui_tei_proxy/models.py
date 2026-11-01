"""Request/response models.

The public `/rerank` contract matches Open WebUI's `ExternalReranker`
(`backend/open_webui/retrieval/models/external.py` in the
open-webui/open-webui repo — the source of truth):

- Request:  `{"model": str, "query": str, "documents": [str, ...],
  "top_n": len(documents)}` — POSTed verbatim to
  `RAG_EXTERNAL_RERANKER_URL`.
- Response: `{"results": [{"index": int, "relevance_score": float}, ...]}`.
  OWUI sorts `results` by `index` and zips the scores positionally
  against the input documents, so the proxy MUST return exactly one
  result per input document — never drop, never truncate to `top_n`.

`documents` entries are plain strings in practice; dict entries with a
`text` or `page_content` key are tolerated for robustness.

The internal upstream payload is TEI's native rerank shape:

- `POST {TEI_RERANKER_URL}/rerank` with `{"query": str, "texts":
  [str, ...]}` → `[{"index": int, "score": float}, ...]`. TEI 1.8.x
  has no `/v1/rerank`; the Jina/Cohere-compatible routes are still
  unmerged upstream (HF PR #797).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def document_text(doc: str | dict[str, Any]) -> str:
    """Extract the text of one `documents` entry.

    Plain strings pass through. Dict entries must carry a non-empty
    `text` or `page_content` string (langchain `Document` shape).
    """
    if isinstance(doc, str):
        return doc
    for key in ("text", "page_content"):
        value = doc.get(key)
        if isinstance(value, str) and value:
            return value
    raise ValueError("document dict must contain a non-empty 'text' or 'page_content' key")


class RerankRequest(BaseModel):
    """Request body for `POST /rerank` — the Open WebUI external reranker dialect.

    `model` and `top_n` are accepted (OWUI always sends them) but
    deliberately NOT forwarded: TEI's native `/rerank` takes only
    `query` + `texts`, and OWUI needs one score per document back
    regardless of `top_n`.
    """

    model_config = ConfigDict(extra="ignore")

    model: str | None = Field(
        default=None, description="Ignored — accepted for OWUI compatibility."
    )
    query: str = Field(..., min_length=1, description="The search query.")
    documents: list[str | dict[str, Any]] = Field(
        ...,
        min_length=1,
        description="Documents to rerank. Strings, or dicts with text/page_content.",
    )
    top_n: int | None = Field(default=None, description="Ignored — always one result per document.")

    @field_validator("query")
    @classmethod
    def _query_non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must be a non-empty, non-whitespace string")
        return v

    @field_validator("documents")
    @classmethod
    def _check_documents(cls, v: list[str | dict[str, Any]]) -> list[str | dict[str, Any]]:
        if not v:
            raise ValueError("documents must be a non-empty list")
        for d in v:
            document_text(d)  # raises ValueError on an unusable entry
        return v


class RerankResult(BaseModel):
    """One scored document, as OWUI's `ExternalReranker` expects."""

    index: int = Field(..., description="Position of the document in the input `documents` list.")
    relevance_score: float = Field(..., description="Reranker score (TEI `score`).")


class RerankResponse(BaseModel):
    """Response body for `POST /rerank` — exactly one entry per input document."""

    results: list[RerankResult]


class TeiRerankRequest(BaseModel):
    """Internal request body for TEI's native `POST /rerank`."""

    query: str
    texts: list[str]


class TeiRerankItem(BaseModel):
    """One entry of TEI's native `/rerank` response list."""

    index: int
    score: float
