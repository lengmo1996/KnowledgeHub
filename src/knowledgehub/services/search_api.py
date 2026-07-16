"""Authenticated loopback Search API."""

from __future__ import annotations

import argparse
import dataclasses
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from knowledgehub.embeddings.endpoint_pool import EndpointPool
from knowledgehub.indexing.qdrant import QdrantIndex
from knowledgehub.indexing.sparse import SparseEncoder
from knowledgehub.pipeline.config import (
    LIGHT_RERANKER_REVISION,
    QUALITY_RERANKER_REVISION,
    RagConfig,
)
from knowledgehub.retrieval.models import SearchRequest
from knowledgehub.retrieval.reranker import RerankerClient
from knowledgehub.retrieval.service import RetrievalService


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)
    knowledge_base: str = "literature"
    intent: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    return_mode: str = "pattern_first"
    mode: str = "hybrid"
    limit: int = Field(default=10, ge=1, le=100)
    prefetch_limit: int = Field(default=50, ge=1, le=500)
    collection_key: str | None = None
    tag: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    doi: str | None = None
    document_id: str | None = None
    attachment_key: str | None = None
    use_reranker: bool = False
    reranker_profile: str = "off"
    fallback_policy: str = "degrade"


class KnowledgeQueryBody(SearchBody):
    knowledge_base: Literal["literature", "code", "writing"] = "literature"
    max_tokens: int = Field(default=4000, ge=128, le=32000)
    allow_auto_import: bool = False
    allow_issues: bool = False


def build_retrieval(config: RagConfig) -> RetrievalService:
    pool = EndpointPool.create(
        config.embedding_endpoints,
        output_dim=config.embedding_dim,
        normalize=config.embedding_normalize,
        timeout_seconds=config.embedding_timeout_seconds,
        strategy=config.embedding_request_strategy,
        api_key=config.embedding_api_key.get_secret_value(),
    )
    reranker = None
    if config.reranker_profile != "off":
        reranker = RerankerClient(
            config.reranker_url,
            api_key=config.reranker_api_key.get_secret_value(),
        )
    return RetrievalService(
        config,
        endpoint_pool=pool,
        sparse_encoder=SparseEncoder(config),
        index=QdrantIndex(
            config.qdrant_url,
            config.qdrant_collection,
            dense_dim=config.embedding_dim,
        ),
        reranker=reranker,
    )


def create_app(
    config: RagConfig,
    *,
    service_factory: Callable[[RagConfig], RetrievalService] = build_retrieval,
) -> FastAPI:
    if not config.search_api_key:
        raise ValueError("KH_SEARCH_API_KEY is required for Search API")
    holder: dict[str, RetrievalService] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        holder["service"] = service_factory(config)
        try:
            yield
        finally:
            service = holder.get("service")
            if service:
                service.endpoint_pool.close()
                if service.reranker:
                    service.reranker.close()

    app = FastAPI(title="KnowledgeHub Search API", version="1", lifespan=lifespan)

    def authorize(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {config.search_api_key.get_secret_value()}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="invalid credentials")

    @app.get("/health")
    def health(_: None = Depends(authorize)) -> dict[str, Any]:
        service = holder["service"]
        return {
            "status": "ok",
            "collection": config.qdrant_collection,
            "embedding_model": config.embedding_model,
            "embedding_revision": config.embedding_revision,
            "embedding_endpoints": service.endpoint_pool.health(),
            "reranker_profile": config.reranker_profile,
            "reranker_revision": (
                LIGHT_RERANKER_REVISION
                if config.reranker_profile == "light"
                else QUALITY_RERANKER_REVISION
                if config.reranker_profile == "quality"
                else None
            ),
        }

    @app.post("/search")
    def search(body: SearchBody, _: None = Depends(authorize)) -> dict[str, Any]:
        if body.knowledge_base != "literature":
            from knowledgehub.hub.config import HubConfig
            from knowledgehub.hub.query import HubQueryRequest, HubQueryService

            response = HubQueryService(
                HubConfig.load(os.environ.get("KH_HUB_CONFIG", "configs/knowledgehub.yaml"))
            ).search(
                HubQueryRequest(
                    knowledge_base=body.knowledge_base,
                    query=body.query,
                    intent=body.intent,
                    filters=body.filters,
                    top_k=body.limit,
                    prefetch_limit=body.prefetch_limit,
                    mode=body.mode,
                    return_mode=body.return_mode,
                    reranker=body.reranker_profile if body.use_reranker else "off",
                )
            )
            return dataclasses.asdict(response)
        response = holder["service"].search(
            SearchRequest(**body.model_dump(exclude={"filters", "return_mode"}))
        )
        return {
            **dataclasses.asdict(response),
            "hits": [dataclasses.asdict(value) for value in response.hits],
        }

    @app.post("/knowledge/query")
    def knowledge_query(
        body: KnowledgeQueryBody, _: None = Depends(authorize)
    ) -> dict[str, Any]:
        from knowledgehub.hub.config import HubConfig
        from knowledgehub.hub.evidence import KnowledgeQueryService, QueryBudget
        from knowledgehub.hub.query import HubQueryRequest

        filters = dict(body.filters)
        for key, value in {
            "collection_key": body.collection_key,
            "tag": body.tag,
            "year_from": body.year_from,
            "year_to": body.year_to,
            "doi": body.doi,
            "document_id": body.document_id,
            "attachment_key": body.attachment_key,
        }.items():
            if value is not None:
                filters[key] = value
        config_value = HubConfig.load(
            os.environ.get("KH_HUB_CONFIG", "configs/knowledgehub.yaml")
        )
        return KnowledgeQueryService(config_value).query(
            HubQueryRequest(
                knowledge_base=body.knowledge_base,
                query=body.query,
                intent=body.intent,
                filters=filters,
                top_k=body.limit,
                prefetch_limit=body.prefetch_limit,
                mode=body.mode,
                return_mode=body.return_mode,
                reranker=body.reranker_profile if body.use_reranker else "off",
            ),
            QueryBudget(
                max_results=body.limit,
                max_tokens=body.max_tokens,
                allow_auto_import=body.allow_auto_import,
                allow_issues=body.allow_issues,
            ),
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("configs/rag/default.yaml"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    if (
        args.host not in {"127.0.0.1", "::1", "localhost"}
        and os.environ.get("KH_ALLOW_NON_LOOPBACK") != "true"
    ):
        raise SystemExit("refusing non-loopback bind without KH_ALLOW_NON_LOOPBACK=true")
    uvicorn.run(create_app(RagConfig.load(args.config)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
