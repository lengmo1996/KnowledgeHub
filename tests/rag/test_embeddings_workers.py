from __future__ import annotations

import math

import httpx
import pytest

from knowledgehub.embeddings.endpoint_pool import EndpointPool
from knowledgehub.embeddings.models import EmbeddingBatchResult
from knowledgehub.embeddings.tei_client import EmbeddingServiceError, TEIClient
from knowledgehub.pipeline.workers import partition_documents, stable_partition


def test_tei_truncates_mrl_and_renormalizes() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=[[3.0, 4.0, 12.0]], request=request)
    )
    client = TEIClient("http://tei", output_dim=2, transport=transport)
    result = client.embed(["paper"])
    assert result.raw_dimension == 3
    assert result.final_dimension == 2
    assert result.vectors[0] == pytest.approx((0.6, 0.8))
    assert math.isclose(sum(value * value for value in result.vectors[0]), 1.0)
    client.close()


def test_tei_api_key_is_only_sent_as_bearer_header() -> None:
    observed: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("authorization", "")
        assert "secret-value" not in str(request.url)
        return httpx.Response(200, json=[[1.0, 0.0]], request=request)

    client = TEIClient(
        "http://tei", output_dim=2, api_key="secret-value", transport=httpx.MockTransport(handler)
    )
    client.embed(["paper"])
    client.close()
    assert observed == {"authorization": "Bearer secret-value"}


class _FakeEndpoint:
    def __init__(self, endpoint: str, *, fail: bool = False) -> None:
        self.endpoint = endpoint
        self.fail = fail

    def embed(self, texts: list[str]) -> EmbeddingBatchResult:
        if self.fail:
            raise EmbeddingServiceError("temporary")
        return EmbeddingBatchResult(
            vectors=((1.0, 0.0),),
            endpoint=self.endpoint,
            raw_dimension=2,
            final_dimension=2,
            text_count=len(texts),
            latency_seconds=0.01,
        )

    def health(self) -> bool:
        return not self.fail

    def close(self) -> None:
        return None


def test_endpoint_pool_fails_over_and_records_work() -> None:
    pool = EndpointPool([_FakeEndpoint("gpu0", fail=True), _FakeEndpoint("gpu1")])  # type: ignore[list-item]
    result = pool.embed(["x"])
    assert result.endpoint == "gpu1"
    assert pool.stats()["gpu0"]["failures"] == 1
    assert pool.stats()["gpu1"]["texts"] == 1


def test_least_outstanding_balances_sequential_batches() -> None:
    pool = EndpointPool(  # type: ignore[list-item]
        [_FakeEndpoint("gpu0"), _FakeEndpoint("gpu1")],
        strategy="least_outstanding",
    )
    assert [pool.embed([str(index)]).endpoint for index in range(4)] == [
        "gpu0",
        "gpu1",
        "gpu0",
        "gpu1",
    ]


def test_stable_partition_does_not_depend_on_input_order() -> None:
    class Document:
        def __init__(self, document_id: str) -> None:
            self.document_id = document_id

    values = [Document("c"), Document("a"), Document("b")]
    one = partition_documents(values, 2)  # type: ignore[arg-type]
    two = partition_documents(reversed(values), 2)  # type: ignore[arg-type]
    assert [[item.document_id for item in part] for part in one] == [
        [item.document_id for item in part] for part in two
    ]
    assert stable_partition("same", 2) == stable_partition("same", 2)
