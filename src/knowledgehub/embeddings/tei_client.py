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
        transport: httpx.BaseTransport | None = None,
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

    def close(self) -> None:
        self._client.close()

    def health(self) -> bool:
        try:
            response = self._client.get("/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def embed(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        if not texts or any(not isinstance(value, str) or not value for value in texts):
            raise ValueError("embedding batch must contain non-empty strings")
        started = time.monotonic()
        try:
            response = self._client.post("/embed", json={"inputs": list(texts), "truncate": True})
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingServiceError(f"TEI request failed for {self.endpoint}: {exc}") from exc
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
            if len(values) != raw_dimension or len(values) < self.output_dim:
                raise EmbeddingServiceError("TEI returned inconsistent or undersized vectors")
            values = values[: self.output_dim]
            if self.normalize:
                norm = math.sqrt(sum(value * value for value in values))
                if norm == 0:
                    raise EmbeddingServiceError("TEI returned a zero vector")
                values = [value / norm for value in values]
            vectors.append(tuple(values))
        return EmbeddingBatchResult(
            vectors=tuple(vectors),
            endpoint=self.endpoint,
            raw_dimension=raw_dimension,
            final_dimension=self.output_dim,
            text_count=len(texts),
            latency_seconds=round(time.monotonic() - started, 6),
        )
