"""Deterministic reciprocal-rank fusion for adapters without server fusion."""

from __future__ import annotations

from typing import Hashable, Sequence, TypeVar

T = TypeVar("T", bound=Hashable)


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[T]], *, k: int = 60
) -> list[tuple[T, float]]:
    if k <= 0:
        raise ValueError("RRF k must be positive")
    scores: dict[T, float] = {}
    for ranking in rankings:
        seen: set[T] = set()
        for rank, item in enumerate(ranking, 1):
            if item in seen:
                continue
            seen.add(item)
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda value: (-value[1], str(value[0])))
