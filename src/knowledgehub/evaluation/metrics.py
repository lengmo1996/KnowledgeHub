from __future__ import annotations

from typing import Any, Mapping, Sequence


def evaluate_rankings(samples: Sequence[Mapping[str, Any]], results: Sequence[Sequence[Mapping[str, Any]]], *, k: int = 10) -> dict[str, float]:
    if len(samples) != len(results):
        raise ValueError("sample and result counts differ")
    recall = reciprocal = version_hits = symbol_hits = 0.0
    for sample, hits in zip(samples, results, strict=True):
        expected_source = sample.get("expected_source")
        expected_version = sample.get("version")
        expected_symbol = sample.get("expected_symbol")
        rank = None
        for index, hit in enumerate(hits[:k], 1):
            if expected_source and hit.get("source_type") == expected_source and rank is None:
                rank = index
            if expected_version and hit.get("version") == expected_version:
                version_hits += 1
                expected_version = None
            if expected_symbol and hit.get("symbol") == expected_symbol:
                symbol_hits += 1
                expected_symbol = None
        if rank:
            recall += 1
            reciprocal += 1 / rank
    count = max(1, len(samples))
    return {"recall_at_k": recall / count, "mrr": reciprocal / count, "correct_version_recall": version_hits / count, "correct_symbol_recall": symbol_hits / count}
