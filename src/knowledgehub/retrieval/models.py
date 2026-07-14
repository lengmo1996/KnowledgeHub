"""Stable retrieval request and response models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchRequest:
    query: str
    mode: str = "hybrid"
    limit: int = 10
    prefetch_limit: int = 50
    collection_key: str | None = None
    tag: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    doi: str | None = None
    document_id: str | None = None
    source: str | None = "zotero"
    use_reranker: bool = False
    reranker_profile: str = "off"


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchHit:
    point_id: str
    score: float
    payload: Mapping[str, Any]
    rerank_score: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchResponse:
    query: str
    mode: str
    collection: str
    embedding_model: str
    embedding_revision: str
    embedding_dimension: int
    reranker_profile: str
    reranker_model: str | None
    reranker_revision: str | None
    reranker_fallback: str | None
    hits: tuple[SearchHit, ...]
    timings: Mapping[str, float]
