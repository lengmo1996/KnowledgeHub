"""Bounded, validated client for Text Embeddings Inference."""

from __future__ import annotations

import math
import time
from typing import Any, Sequence

import httpx

from knowledgehub.embeddings.models import EmbeddingBatchResult


class EmbeddingServiceError(RuntimeError):
    pass


class TEIClient:
    def __init__(
        self,
        endpoint: str,
        *,
        output_dim: int,
        normalize: bool = True,
        timeout_seconds: float = 120.0,
        api_key: str = "",
        transport: Any = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.output_dim = output_dim
        self.normalize = normalize
        self._client = httpx.Client(
            base_url=self.endpoint,
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            transport=transport,
            follow_redirects=False,
        )
        self._async_client = httpx.AsyncClient(
            base_url=self.endpoint,
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    async def aclose(self) -> None:
        await self._async_client.aclose()

    def health(self) -> bool:
        try:
            response = self._client.get("/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def ahealth(self) -> bool:
        try:
            response = await self._async_client.get("/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def embed(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        _validate_texts(texts)
        started = time.monotonic()
        try:
            response = self._client.post("/embed", json={"inputs": list(texts), "truncate": True})
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingServiceError(f"TEI request failed for {self.endpoint}: {exc}") from exc
        return _embedding_result(
            payload,
            texts=texts,
            endpoint=self.endpoint,
            output_dim=self.output_dim,
            normalize=self.normalize,
            started=started,
        )

    async def aembed(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        _validate_texts(texts)
        started = time.monotonic()
        try:
            response = await self._async_client.post(
                "/embed", json={"inputs": list(texts), "truncate": True}
            )
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingServiceError(f"TEI request failed for {self.endpoint}: {exc}") from exc
        return _embedding_result(
            payload,
            texts=texts,
            endpoint=self.endpoint,
            output_dim=self.output_dim,
            normalize=self.normalize,
            started=started,
        )


def _validate_texts(texts: Sequence[str]) -> None:
    if not texts or any(not isinstance(value, str) or not value for value in texts):
        raise ValueError("embedding batch must contain non-empty strings")


def _embedding_result(
    payload: Any,
    *,
    texts: Sequence[str],
    endpoint: str,
    output_dim: int,
    normalize: bool,
    started: float,
) -> EmbeddingBatchResult:
    if not isinstance(payload, list) or len(payload) != len(texts):
        raise EmbeddingServiceError("TEI returned an unexpected batch shape")
    vectors: list[tuple[float, ...]] = []
    raw_dimension = 0
    for vector in payload:
        if not isinstance(vector, list) or not vector:
            raise EmbeddingServiceError("TEI returned an invalid vector")
        values = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in values):
            raise EmbeddingServiceError("TEI returned non-finite vector values")
        raw_dimension = len(values) if raw_dimension == 0 else raw_dimension
        if len(values) != raw_dimension or len(values) < output_dim:
            raise EmbeddingServiceError("TEI returned inconsistent or undersized vectors")
        values = values[:output_dim]
        if normalize:
            norm = math.sqrt(sum(value * value for value in values))
            if norm == 0:
                raise EmbeddingServiceError("TEI returned a zero vector")
            values = [value / norm for value in values]
        vectors.append(tuple(values))
    return EmbeddingBatchResult(
        vectors=tuple(vectors),
        endpoint=endpoint,
        raw_dimension=raw_dimension,
        final_dimension=output_dim,
        text_count=len(texts),
        latency_seconds=round(time.monotonic() - started, 6),
    )
