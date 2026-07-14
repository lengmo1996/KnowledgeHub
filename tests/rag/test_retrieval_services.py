from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from knowledgehub.embeddings.models import EmbeddingBatchResult
from knowledgehub.indexing.qdrant import SearchPoint
from knowledgehub.pipeline.config import RagConfig, SecretValue
from knowledgehub.retrieval.fusion import reciprocal_rank_fusion
from knowledgehub.retrieval.models import SearchRequest, SearchResponse
from knowledgehub.retrieval.service import RetrievalService
from knowledgehub.services.reranker_api import (
    QwenCausalLMReranker,
)
from knowledgehub.services.reranker_api import (
    create_app as create_reranker_app,
)
from knowledgehub.services.search_api import create_app


class Pool:
    def embed(self, texts):
        return EmbeddingBatchResult(
            vectors=((1.0, 0.0),),
            endpoint="gpu0",
            raw_dimension=2,
            final_dimension=2,
            text_count=1,
            latency_seconds=0.01,
        )

    def health(self):
        return {"gpu0": True}

    def close(self) -> None:
        return None


class Sparse:
    def encode(self, texts):
        return [([1], [1.0])]


class Index:
    def query(self, **kwargs):
        return [SearchPoint(point_id="p1", score=0.5, payload={"text": "answer"})]


def test_rrf_and_sparse_query_without_reranker(tmp_path) -> None:
    assert reciprocal_rank_fusion([["a", "b"], ["b", "a"]])[0][0] == "a"
    config = RagConfig(data_dir=tmp_path, gpu_mode="cpu", embedding_dim=2)
    service = RetrievalService(config, endpoint_pool=Pool(), sparse_encoder=Sparse(), index=Index())
    response = service.search(SearchRequest(query="what", mode="sparse"))
    assert response.hits[0].payload["text"] == "answer"
    assert response.embedding_revision == config.embedding_revision


def test_search_api_requires_bearer_key(tmp_path) -> None:
    config = RagConfig(
        data_dir=tmp_path,
        gpu_mode="cpu",
        embedding_dim=2,
        search_api_key=SecretValue("secret"),
    )
    response = SearchResponse(
        query="q",
        mode="sparse",
        collection="c",
        embedding_model="m",
        embedding_revision="r",
        embedding_dimension=2,
        reranker_profile="off",
        reranker_model=None,
        reranker_revision=None,
        reranker_fallback=None,
        hits=(),
        timings={},
    )
    fake = SimpleNamespace(endpoint_pool=Pool(), reranker=None, search=lambda request: response)
    app = create_app(config, service_factory=lambda _: fake)

    route = next(value for value in app.routes if getattr(value, "path", "") == "/health")
    authorize = route.dependant.dependencies[0].call
    with pytest.raises(HTTPException) as failure:
        authorize(None)
    assert failure.value.status_code == 401
    assert authorize("Bearer secret") is None


def test_reranker_reduces_oom_batch_to_one() -> None:
    class FakeReranker(QwenCausalLMReranker):
        def __init__(self) -> None:
            self.batch_size = 4
            self.attempts: list[int] = []

        def _predict(self, query: str, passages: list[str], batch_size: int) -> list[float]:
            self.attempts.append(batch_size)
            if batch_size > 1:
                raise RuntimeError("CUDA out of memory")
            return [0.75 for _ in passages]

    reranker = FakeReranker()
    scores, batch = reranker.rerank("query", ["a", "b", "c", "d"])
    assert batch == 1
    assert scores == [0.75, 0.75, 0.75, 0.75]
    assert reranker.attempts == [4, 2, 1]


def test_reranker_api_requires_its_own_key(tmp_path) -> None:
    model = SimpleNamespace(
        profile="quality",
        model_name="Qwen/Qwen3-Reranker-4B",
        revision="revision",
        device="cuda:0",
        max_length=2048,
        rerank=lambda query, passages: ([0.5 for _ in passages], 1),
    )
    config = RagConfig(
        data_dir=tmp_path,
        gpu_mode="cpu",
        reranker_profile="quality",
        reranker_api_key=SecretValue("rerank-secret"),
    )
    app = create_reranker_app(config, device="cuda:0", model=model)
    route = next(value for value in app.routes if getattr(value, "path", "") == "/health")
    authorize = route.dependant.dependencies[0].call
    with pytest.raises(HTTPException) as failure:
        authorize("Bearer wrong")
    assert failure.value.status_code == 401
    assert authorize("Bearer rerank-secret") is None
