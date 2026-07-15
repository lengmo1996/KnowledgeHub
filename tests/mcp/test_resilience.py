from __future__ import annotations

from types import SimpleNamespace

import anyio

from knowledgehub.mcp.config import MCPConfig
from knowledgehub.mcp.resilience import CircuitBreaker, SlidingWindowLimiter
from knowledgehub.mcp.runtime import RequestPolicyMiddleware
from knowledgehub.mcp.tokens import request_context
from knowledgehub.mcp.tools import ToolRegistry
from knowledgehub.pipeline.config import RagConfig
from knowledgehub.retrieval.models import SearchRequest
from knowledgehub.retrieval.service import RetrievalService


class FailingAsyncIndex:
    async def aretrieve(self, point_ids):  # type: ignore[no-untyped-def]
        raise RuntimeError("qdrant down")


def test_qdrant_circuit_opens_after_bounded_failures() -> None:
    service = RetrievalService(
        RagConfig(embedding_dim=2),
        endpoint_pool=object(),
        sparse_encoder=object(),
        index=FailingAsyncIndex(),
    )
    breaker = CircuitBreaker(failure_threshold=3, max_attempts=1)
    service.circuit_breakers = {"qdrant": breaker}

    async def exercise() -> None:
        for _ in range(3):
            try:
                await service.aget_chunk("missing")
            except RuntimeError as exc:
                assert str(exc) == "qdrant down"
        assert breaker.state == "open"
        try:
            await service.aget_chunk("missing")
        except RuntimeError as exc:
            assert str(exc) == "circuit_open"

    anyio.run(exercise)


class CancelService:
    def __init__(self) -> None:
        self.config = SimpleNamespace(reranker_profile="off")
        self.circuit_breakers = {}
        self.fast = False

    async def aget_chunk(self, chunk_id: str):  # type: ignore[no-untyped-def]
        if not self.fast:
            await anyio.sleep(1)
        return {"chunk_id": chunk_id, "text": "ok"}


def test_deadline_releases_request_semaphore() -> None:
    service = CancelService()
    registry = ToolRegistry(
        service,
        MCPConfig(request_timeout_seconds=1, max_concurrent_requests=1),
    )

    async def exercise() -> None:
        first = await registry.call("rag_get_chunk", {"chunk_id": "slow"})
        assert first.structuredContent["error"]["code"] == "deadline_exceeded"
        service.fast = True
        second = await registry.call("rag_get_chunk", {"chunk_id": "fast"})
        assert second.structuredContent["ok"] is True

    anyio.run(exercise)


def test_final_response_limit_is_structured() -> None:
    service = CancelService()
    service.fast = True
    registry = ToolRegistry(service, MCPConfig(max_response_bytes=64))

    async def exercise() -> None:
        result = await registry.call("rag_get_chunk", {"chunk_id": "x"})
        assert result.structuredContent["error"]["code"] == "response_too_large"

    anyio.run(exercise)


class FailedEmbeddingPool:
    async def aembed(self, texts):  # type: ignore[no-untyped-def]
        raise RuntimeError("embedding down")


class SparseEncoder:
    def encode(self, texts):  # type: ignore[no-untyped-def]
        return [([1], [1.0])]


class CapturingIndex:
    def __init__(self) -> None:
        self.mode = ""

    async def aquery(self, **kwargs):  # type: ignore[no-untyped-def]
        self.mode = kwargs["mode"]
        return []


def test_hybrid_embedding_failure_degrades_or_fails_strictly() -> None:
    index = CapturingIndex()
    service = RetrievalService(
        RagConfig(embedding_dim=2),
        endpoint_pool=FailedEmbeddingPool(),
        sparse_encoder=SparseEncoder(),
        index=index,
    )

    async def exercise() -> None:
        degraded = await service.asearch(
            SearchRequest(query="q", mode="hybrid", fallback_policy="degrade")
        )
        assert degraded.mode == "sparse"
        assert degraded.degraded is True
        assert degraded.warnings == ("embedding_unavailable_degraded_to_sparse",)
        assert index.mode == "sparse"
        try:
            await service.asearch(SearchRequest(query="q", mode="hybrid", fallback_policy="strict"))
        except RuntimeError as exc:
            assert str(exc) == "embedding_unavailable"

    anyio.run(exercise)


def test_rate_limit_isolated_by_principal_and_ip_key() -> None:
    limiter = SlidingWindowLimiter(1)

    async def exercise() -> None:
        assert await limiter.allow("principal:a") is True
        assert await limiter.allow("principal:a") is False
        assert await limiter.allow("ip:192.0.2.1") is True

    anyio.run(exercise)


def test_forwarded_headers_are_ignored_on_lan_and_rightmost_on_trusted_proxy() -> None:
    observed: list[tuple[str, str]] = []

    async def app(scope, receive, send):  # type: ignore[no-untyped-def]
        observed.append(request_context.get())

    async def invoke(*, trusted: bool, peer: str, forwarded: bytes) -> None:
        middleware = RequestPolicyMiddleware(app, trusted_proxy=trusted)
        scope = {
            "type": "http",
            "client": (peer, 1234),
            "headers": [
                (b"x-forwarded-for", forwarded),
                (b"tailscale-user-login", b"user@example.com"),
            ],
            "root_path": "/mcp",
            "path": "/",
        }

        async def receive():  # type: ignore[no-untyped-def]
            return {"type": "http.disconnect"}

        async def send(message):  # type: ignore[no-untyped-def]
            return None

        await middleware(scope, receive, send)  # type: ignore[arg-type]
        if trusted:
            assert scope["kh_tailscale_identity"] == "user@example.com"
        else:
            assert "kh_tailscale_identity" not in scope

    async def exercise() -> None:
        await invoke(trusted=False, peer="10.249.43.193", forwarded=b"198.51.100.9")
        await invoke(
            trusted=True,
            peer="127.0.0.1",
            forwarded=b"198.51.100.8, 100.64.0.9",
        )

    anyio.run(exercise)
    assert observed == [
        ("10.249.43.193", "/mcp"),
        ("100.64.0.9", "/mcp"),
    ]
