"""Unified dense, sparse, and hybrid retrieval service."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

import httpx

from knowledgehub.pipeline.config import (
    LIGHT_RERANKER_REVISION,
    QUALITY_RERANKER_REVISION,
    RagConfig,
)
from knowledgehub.retrieval.models import SearchHit, SearchRequest, SearchResponse

T = TypeVar("T")


class RetrievalService:
    def __init__(
        self,
        config: RagConfig,
        *,
        endpoint_pool: Any,
        sparse_encoder: Any,
        index: Any,
        reranker: Any | None = None,
        catalog: Any | None = None,
    ) -> None:
        self.config = config
        self.endpoint_pool = endpoint_pool
        self.sparse_encoder = sparse_encoder
        self.index = index
        self.reranker = reranker
        self.catalog = catalog
        self.circuit_breakers: dict[str, Any] = {}

    async def asearch(self, request: SearchRequest) -> SearchResponse:
        """Cancellation-aware search using async TEI, Qdrant, and reranker clients."""

        self._validate(request)
        started = time.monotonic()
        sparse = None
        sparse_seconds = 0.0
        if request.mode in {"hybrid", "sparse"}:
            sparse_started = time.monotonic()
            sparse = self.sparse_encoder.encode([request.query])[0]
            sparse_seconds = time.monotonic() - sparse_started
        dense = None
        dense_seconds = 0.0
        effective_mode = request.mode
        warnings: list[str] = []
        if request.mode in {"dense", "hybrid"}:
            dense_started = time.monotonic()
            prompt = f"Instruct: {self.config.embedding_query_instruction}\nQuery: {request.query}"
            try:
                dense = (
                    await self._dependency_call(
                        "embedding", lambda: self.endpoint_pool.aembed([prompt])
                    )
                ).vectors[0]
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                if request.mode == "hybrid" and request.fallback_policy == "degrade":
                    effective_mode = "sparse"
                    warnings.append("embedding_unavailable_degraded_to_sparse")
                else:
                    raise RuntimeError("embedding_unavailable") from exc
            dense_seconds = time.monotonic() - dense_started
        qdrant_started = time.monotonic()
        points = await self._dependency_call(
            "qdrant",
            lambda: self.index.aquery(
                dense=dense,
                sparse=sparse,
                mode=effective_mode,
                limit=request.prefetch_limit if request.use_reranker else request.limit,
                prefetch_limit=request.prefetch_limit,
                query_filter=_build_filter(request),
            ),
        )
        qdrant_seconds = time.monotonic() - qdrant_started
        hits = [
            SearchHit(
                point_id=value.point_id,
                score=value.score,
                payload=value.payload,
                dense_score=value.score if effective_mode == "dense" else None,
                sparse_score=value.score if effective_mode == "sparse" else None,
            )
            for value in points
        ]
        fallback: str | None = None
        reranker_seconds = 0.0
        profile = request.reranker_profile if request.use_reranker else "off"
        if request.use_reranker:
            if profile not in {"light", "quality"}:
                raise ValueError("reranker profile must be light or quality")
            if self.reranker is None:
                if request.fallback_policy == "strict":
                    raise RuntimeError("reranker_unavailable")
                fallback = "reranker_unavailable"
            else:
                reranker = self.reranker
                rerank_started = time.monotonic()
                try:
                    scores = await self._dependency_call(
                        "reranker",
                        lambda: reranker.arerank(
                            request.query,
                            [str(value.payload.get("text") or "") for value in hits],
                            profile=profile,
                        ),
                    )
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    if request.fallback_policy == "strict":
                        raise RuntimeError("reranker_unavailable") from exc
                    fallback = "reranker_failed"
                else:
                    hits = [
                        SearchHit(
                            point_id=value.point_id,
                            score=value.score,
                            payload=value.payload,
                            rerank_score=score,
                            dense_score=value.dense_score,
                            sparse_score=value.sparse_score,
                        )
                        for value, score in zip(hits, scores, strict=True)
                    ]
                    hits.sort(key=lambda value: (-(value.rerank_score or 0.0), value.point_id))
                reranker_seconds = time.monotonic() - rerank_started
        return self._response(
            request,
            effective_mode=effective_mode,
            hits=hits,
            profile=profile,
            fallback=fallback,
            warnings=warnings,
            dense_seconds=dense_seconds,
            sparse_seconds=sparse_seconds,
            qdrant_seconds=qdrant_seconds,
            reranker_seconds=reranker_seconds,
            started=started,
        )

    def search(self, request: SearchRequest) -> SearchResponse:
        self._validate(request)
        started = time.monotonic()
        sparse = None
        sparse_seconds = 0.0
        if request.mode in {"hybrid", "sparse"}:
            sparse_started = time.monotonic()
            sparse = self.sparse_encoder.encode([request.query])[0]
            sparse_seconds = time.monotonic() - sparse_started
        dense = None
        dense_seconds = 0.0
        effective_mode = request.mode
        warnings: list[str] = []
        if request.mode in {"dense", "hybrid"}:
            dense_started = time.monotonic()
            prompt = f"Instruct: {self.config.embedding_query_instruction}\nQuery: {request.query}"
            try:
                dense = self.endpoint_pool.embed([prompt]).vectors[0]
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                if request.mode == "hybrid" and request.fallback_policy == "degrade":
                    effective_mode = "sparse"
                    warnings.append("embedding_unavailable_degraded_to_sparse")
                else:
                    raise RuntimeError("embedding_unavailable") from exc
            dense_seconds = time.monotonic() - dense_started
        qdrant_started = time.monotonic()
        points = self.index.query(
            dense=dense,
            sparse=sparse,
            mode=effective_mode,
            limit=request.prefetch_limit if request.use_reranker else request.limit,
            prefetch_limit=request.prefetch_limit,
            query_filter=_build_filter(request),
        )
        qdrant_seconds = time.monotonic() - qdrant_started
        hits = [
            SearchHit(
                point_id=value.point_id,
                score=value.score,
                payload=value.payload,
                dense_score=value.score if effective_mode == "dense" else None,
                sparse_score=value.score if effective_mode == "sparse" else None,
            )
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
            mode=effective_mode,
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
            requested_mode=request.mode,
            degraded=effective_mode != request.mode or fallback is not None,
            warnings=tuple(warnings),
            hits=tuple(hits[: request.limit]),
            timings={
                "dense_seconds": round(dense_seconds, 6),
                "qdrant_seconds": round(qdrant_seconds, 6),
                "reranker_seconds": round(reranker_seconds, 6),
                "sparse_seconds": round(sparse_seconds, 6),
                "total_seconds": round(time.monotonic() - started, 6),
            },
        )

    def get_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        points = self.index.retrieve([chunk_id])
        return dict(points[0].payload) if points else None

    async def aget_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        points = await self._dependency_call("qdrant", lambda: self.index.aretrieve([chunk_id]))
        return dict(points[0].payload) if points else None

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        if self.catalog is None:
            raise RuntimeError("catalog_unavailable")
        document = cast("dict[str, Any] | None", self.catalog.get_document(document_id))
        if document is None:
            return None
        document["chunks"] = [
            {
                key: value
                for key, value in point.payload.items()
                if key
                in {
                    "chunk_id",
                    "chunk_index",
                    "document_id",
                    "page_end",
                    "page_start",
                    "section_path",
                    "text_sha256",
                    "token_count",
                }
            }
            for point in self.index.document_points(document_id)
        ]
        return document

    async def aget_document(self, document_id: str) -> dict[str, Any] | None:
        if self.catalog is None:
            raise RuntimeError("catalog_unavailable")
        document = cast("dict[str, Any] | None", self.catalog.get_document(document_id))
        if document is None:
            return None
        document["chunks"] = [
            {
                key: value
                for key, value in point.payload.items()
                if key
                in {
                    "chunk_id",
                    "chunk_index",
                    "document_id",
                    "page_end",
                    "page_start",
                    "section_path",
                    "text_sha256",
                    "token_count",
                }
            }
            for point in await self._dependency_call(
                "qdrant", lambda: self.index.adocument_points(document_id)
            )
        ]
        return document

    def get_neighbors(self, chunk_id: str, *, before: int, after: int) -> list[dict[str, Any]]:
        points = self.index.retrieve([chunk_id])
        if not points:
            return []
        payload = points[0].payload
        document_id = str(payload.get("document_id") or "")
        chunk_index = int(payload.get("chunk_index", 0))
        if not document_id:
            return []
        return [
            dict(value.payload)
            for value in self.index.document_points(
                document_id,
                chunk_index_from=chunk_index - before,
                chunk_index_to=chunk_index + after,
                limit=before + after + 1,
            )
        ]

    async def aget_neighbors(
        self, chunk_id: str, *, before: int, after: int
    ) -> list[dict[str, Any]]:
        points = await self._dependency_call("qdrant", lambda: self.index.aretrieve([chunk_id]))
        if not points:
            return []
        payload = points[0].payload
        document_id = str(payload.get("document_id") or "")
        chunk_index = int(payload.get("chunk_index", 0))
        if not document_id:
            return []
        return [
            dict(value.payload)
            for value in await self._dependency_call(
                "qdrant",
                lambda: self.index.adocument_points(
                    document_id,
                    chunk_index_from=chunk_index - before,
                    chunk_index_to=chunk_index + after,
                    limit=before + after + 1,
                ),
            )
        ]

    def resolve_reference(self, **reference: str | None) -> list[dict[str, Any]]:
        if self.catalog is None:
            raise RuntimeError("catalog_unavailable")
        return cast("list[dict[str, Any]]", self.catalog.resolve_reference(**reference))

    def list_facets(self, facet: str, *, cursor: str | None, limit: int) -> dict[str, Any]:
        if self.catalog is None:
            raise RuntimeError("catalog_unavailable")
        return cast("dict[str, Any]", self.catalog.list_facets(facet, cursor=cursor, limit=limit))

    async def astatus(self) -> dict[str, Any]:
        if self.catalog is None:
            raise RuntimeError("catalog_unavailable")
        return {
            "catalog": self.catalog.status(),
            "collection": await self._dependency_call("qdrant", self.index.astatus),
        }

    async def areadiness(self) -> dict[str, Any]:
        status = await self.astatus()
        embedding = await self._dependency_call("embedding", self.endpoint_pool.ahealth)
        status["embedding"] = {
            "status": "ready" if embedding and all(embedding.values()) else "not_ready",
            "replicas": {str(index): value for index, value in enumerate(embedding.values())},
        }
        status["sparse"] = {"status": "ready" if self.sparse_encoder is not None else "not_ready"}
        if self.config.reranker_profile == "off":
            status["reranker"] = {"status": "not_required", "profile": "off"}
        else:
            ready = bool(self.reranker and await self.reranker.ahealth())
            status["reranker"] = {
                "status": "ready" if ready else "degraded",
                "profile": self.config.reranker_profile,
            }
        return status

    async def aclose(self) -> None:
        await self.endpoint_pool.aclose()
        await self.index.aclose()
        if self.reranker:
            await self.reranker.aclose()

    async def _dependency_call(self, name: str, operation: Callable[[], Awaitable[T]]) -> T:
        breaker = self.circuit_breakers.get(name)
        if breaker is None:
            return await operation()
        return cast(T, await breaker.call(operation))

    @staticmethod
    def _validate(request: SearchRequest) -> None:
        if not request.query.strip():
            raise ValueError("query cannot be empty")
        if request.mode not in {"dense", "hybrid", "sparse"}:
            raise ValueError("query mode must be dense, hybrid, or sparse")
        if request.fallback_policy not in {"strict", "degrade"}:
            raise ValueError("fallback policy must be strict or degrade")
        if not 1 <= request.limit <= 100 or request.prefetch_limit < request.limit:
            raise ValueError("invalid result or prefetch limit")

    def _response(
        self,
        request: SearchRequest,
        *,
        effective_mode: str,
        hits: list[SearchHit],
        profile: str,
        fallback: str | None,
        warnings: list[str],
        dense_seconds: float,
        sparse_seconds: float,
        qdrant_seconds: float,
        reranker_seconds: float,
        started: float,
    ) -> SearchResponse:
        return SearchResponse(
            query=request.query,
            mode=effective_mode,
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
            requested_mode=request.mode,
            degraded=effective_mode != request.mode or fallback is not None,
            warnings=tuple(warnings),
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
        ("attachment_key", request.attachment_key),
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
