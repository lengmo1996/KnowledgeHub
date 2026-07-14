"""Embedding transport models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmbeddingBatchResult:
    vectors: tuple[tuple[float, ...], ...]
    raw_dimension: int
    final_dimension: int
    endpoint: str
    latency_seconds: float
    text_count: int
