"""Stable retrieval request and response models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchRequest:
    query: str
    knowledge_base: str = "literature"
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
    attachment_key: str | None = None
    use_reranker: bool = False
    reranker_profile: str = "off"
    fallback_policy: str = "degrade"
    library: str | None = None
    package: str | None = None
    version: str | None = None
    source_type: str | None = None
    source_types: tuple[str, ...] = ()
    repository: str | None = None
    path: str | None = None
    symbol: str | None = None
    section: str | None = None
    writing_function: str | None = None
    research_domain: str | None = None
    intent: str | None = None
    installed_version: str | None = None
    target_version: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchHit:
    point_id: str
    score: float
    payload: Mapping[str, Any]
    rerank_score: float | None = None
    dense_score: float | None = None
    sparse_score: float | None = None


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
    requested_mode: str = "hybrid"
    degraded: bool = False
    warnings: tuple[str, ...] = ()
