"""Unified sparse/hybrid retrieval service."""

from __future__ import annotations

import time
from typing import Any

import httpx

from knowledgehub.pipeline.config import (
    LIGHT_RERANKER_REVISION,
    QUALITY_RERANKER_REVISION,
    RagConfig,
)
from knowledgehub.retrieval.models import SearchHit, SearchRequest, SearchResponse


class RetrievalService:
    def __init__(
        self,
        config: RagConfig,
        *,
        endpoint_pool: Any,
        sparse_encoder: Any,
        index: Any,
        reranker: Any | None = None,
    ) -> None:
        self.config = config
        self.endpoint_pool = endpoint_pool
        self.sparse_encoder = sparse_encoder
        self.index = index
        self.reranker = reranker

    def search(self, request: SearchRequest) -> SearchResponse:
        if not request.query.strip():
            raise ValueError("query cannot be empty")
        if request.mode not in {"hybrid", "sparse"}:
            raise ValueError("query mode must be hybrid or sparse")
        if not 1 <= request.limit <= 100 or request.prefetch_limit < request.limit:
            raise ValueError("invalid result or prefetch limit")
        started = time.monotonic()
        sparse_started = time.monotonic()
        sparse = self.sparse_encoder.encode([request.query])[0]
        sparse_seconds = time.monotonic() - sparse_started
        dense = None
        dense_seconds = 0.0
        if request.mode == "hybrid":
            dense_started = time.monotonic()
            prompt = f"Instruct: {self.config.embedding_query_instruction}\nQuery: {request.query}"
            dense = self.endpoint_pool.embed([prompt]).vectors[0]
            dense_seconds = time.monotonic() - dense_started
        qdrant_started = time.monotonic()
        points = self.index.query(
            dense=dense,
            sparse=sparse,
            mode=request.mode,
            limit=request.prefetch_limit if request.use_reranker else request.limit,
            prefetch_limit=request.prefetch_limit,
            query_filter=_build_filter(request),
        )
        qdrant_seconds = time.monotonic() - qdrant_started
        hits = [
            SearchHit(point_id=value.point_id, score=value.score, payload=value.payload)
            for value in points
        ]
        fallback: str | None = None
        reranker_seconds = 0.0
        profile = request.reranker_profile if request.use_reranker else "off"
        if request.use_reranker:
            if profile not in {"light", "quality"}:
                raise ValueError("reranker profile must be light or quality")
            if self.reranker is None:
                fallback = "reranker_unavailable"
            else:
                rerank_started = time.monotonic()
                try:
                    scores = self.reranker.rerank(
                        request.query,
                        [str(value.payload.get("text") or "") for value in hits],
                        profile=profile,
                    )
                except (httpx.HTTPError, RuntimeError, ValueError):
                    fallback = "reranker_failed"
                else:
                    hits = [
                        SearchHit(
                            point_id=value.point_id,
                            score=value.score,
                            payload=value.payload,
                            rerank_score=score,
                        )
                        for value, score in zip(hits, scores, strict=True)
                    ]
                    hits.sort(key=lambda value: (-(value.rerank_score or 0.0), value.point_id))
                reranker_seconds = time.monotonic() - rerank_started
        return SearchResponse(
            query=request.query,
            mode=request.mode,
            collection=self.config.qdrant_collection,
            embedding_model=self.config.embedding_model,
            embedding_revision=self.config.embedding_revision,
            embedding_dimension=self.config.embedding_dim,
            reranker_profile=profile,
            reranker_model=(
                f"Qwen/Qwen3-Reranker-{'0.6B' if profile == 'light' else '4B'}"
                if profile in {"light", "quality"}
                else None
            ),
            reranker_revision=(
                LIGHT_RERANKER_REVISION
                if profile == "light"
                else QUALITY_RERANKER_REVISION
                if profile == "quality"
                else None
            ),
            reranker_fallback=fallback,
            hits=tuple(hits[: request.limit]),
            timings={
                "dense_seconds": round(dense_seconds, 6),
                "qdrant_seconds": round(qdrant_seconds, 6),
                "reranker_seconds": round(reranker_seconds, 6),
                "sparse_seconds": round(sparse_seconds, 6),
                "total_seconds": round(time.monotonic() - started, 6),
            },
        )


def _build_filter(request: SearchRequest) -> Any:
    from qdrant_client import models

    must = []
    for key, value in (
        ("source", request.source),
        ("document_id", request.document_id),
        ("doi", request.doi),
        ("tags", request.tag),
        ("collection_keys", request.collection_key),
    ):
        if value:
            must.append(models.FieldCondition(key=key, match=models.MatchValue(value=value)))
    if request.year_from is not None or request.year_to is not None:
        must.append(
            models.FieldCondition(
                key="year", range=models.Range(gte=request.year_from, lte=request.year_to)
            )
        )
    return models.Filter(must=must) if must else None
