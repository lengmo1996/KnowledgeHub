"""HTTP client for Text Embeddings Inference with MRL projection."""

from __future__ import annotations

import math
import time
from typing import Any, Sequence

import httpx

from knowledgehub.embeddings.models import EmbeddingBatchResult


class TEIClient:
    def __init__(
        self,
        endpoint: str,
        *,
        output_dim: int,
        normalize: bool,
        timeout_seconds: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.output_dim = output_dim
        self.normalize = normalize
        self._client = httpx.Client(
            base_url=self.endpoint,
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def health(self) -> bool:
        try:
            return self._client.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def embed(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        if not texts or any(not value.strip() for value in texts):
            raise ValueError("embedding inputs must contain non-empty text")
        started = time.monotonic()
        response = self._client.post("/embed", json={"inputs": list(texts), "truncate": True})
        response.raise_for_status()
        payload: Any = response.json()
        if not isinstance(payload, list) or len(payload) != len(texts):
            raise RuntimeError("TEI returned an unexpected batch shape")
        vectors: list[tuple[float, ...]] = []
        raw_dimension = 0
        for row in payload:
            if not isinstance(row, list) or not row:
                raise RuntimeError("TEI returned an invalid vector")
            raw_dimension = len(row)
            if raw_dimension < self.output_dim:
                raise RuntimeError(
                    f"TEI vector dimension {raw_dimension} is smaller than {self.output_dim}"
                )
            projected = [float(value) for value in row[: self.output_dim]]
            if not all(math.isfinite(value) for value in projected):
                raise RuntimeError("TEI returned a non-finite vector")
            if self.normalize:
                norm = math.sqrt(sum(value * value for value in projected))
                if norm == 0:
                    raise RuntimeError("TEI returned a zero vector")
                projected = [value / norm for value in projected]
            vectors.append(tuple(projected))
        return EmbeddingBatchResult(
            vectors=tuple(vectors),
            raw_dimension=raw_dimension,
            final_dimension=self.output_dim,
            endpoint=self.endpoint,
            latency_seconds=round(time.monotonic() - started, 6),
            text_count=len(texts),
        )
