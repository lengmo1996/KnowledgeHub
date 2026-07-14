"""Embedding request/result models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmbeddingBatchResult:
    vectors: tuple[tuple[float, ...], ...]
    endpoint: str
    raw_dimension: int
    final_dimension: int
    text_count: int
    latency_seconds: float
