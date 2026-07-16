from __future__ import annotations

from typing import Any, Mapping, Sequence


def evaluate_rankings(
    samples: Sequence[Mapping[str, Any]],
    results: Sequence[Sequence[Mapping[str, Any]]],
    *,
    k: int = 10,
) -> dict[str, float]:
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
    return {
        "recall_at_k": recall / count,
        "mrr": reciprocal / count,
        "correct_version_recall": version_hits / count,
        "correct_symbol_recall": symbol_hits / count,
    }


def evaluate_writing(
    samples: Sequence[Mapping[str, Any]],
    predictions: Sequence[Mapping[str, Any]],
) -> dict[str, float]:
    """Evaluate automated labels plus explicitly supplied human judgements."""

    if len(samples) != len(predictions):
        raise ValueError("sample and prediction counts differ")
    function_hits = section_hits = traceable = accepted = risk_hits = 0
    function_total = section_total = acceptance_total = risk_total = 0
    wrong_domains = retrieved_domains = 0
    transferability: list[float] = []
    material_ids: list[str] = []
    for sample, prediction in zip(samples, predictions, strict=True):
        if sample.get("expected_function") is not None:
            function_total += 1
            function_hits += prediction.get("writing_function") == sample.get("expected_function")
        if sample.get("expected_section") is not None:
            section_total += 1
            section_hits += prediction.get("section") == sample.get("expected_section")
        transfer = prediction.get("pattern_transferability_score")
        if isinstance(transfer, (int, float)):
            transferability.append(max(0.0, min(1.0, float(transfer))))
        if prediction.get("source_paper_id") and prediction.get("source_location"):
            traceable += 1
        if isinstance(prediction.get("accepted"), bool):
            acceptance_total += 1
            accepted += bool(prediction["accepted"])
        expected_domains = set(sample.get("expected_domains") or [])
        for domain in prediction.get("retrieved_domains") or []:
            retrieved_domains += 1
            wrong_domains += bool(expected_domains and domain not in expected_domains)
        if sample.get("expected_similarity_risk") is not None:
            risk_total += 1
            expected = str(sample["expected_similarity_risk"])
            predicted = str(prediction.get("similarity_risk") or "low")
            risk_hits += (expected == "low" and predicted == "low") or (
                expected != "low" and predicted != "low"
            )
        material_ids.extend(str(value) for value in prediction.get("material_ids") or [])
    duplicates = len(material_ids) - len(set(material_ids))
    count = len(samples)
    return {
        "writing_function_accuracy": _safe_rate(function_hits, function_total),
        "section_accuracy": _safe_rate(section_hits, section_total),
        "pattern_transferability": round(sum(transferability) / max(1, len(transferability)), 6),
        "source_traceability_rate": _safe_rate(traceable, count),
        "duplicate_material_ratio": _safe_rate(duplicates, len(material_ids)),
        "user_acceptance_rate": _safe_rate(accepted, acceptance_total),
        "wrong_domain_recall_rate": _safe_rate(wrong_domains, retrieved_domains),
        "similarity_risk_detection_rate": _safe_rate(risk_hits, risk_total),
    }


def _safe_rate(numerator: int | float, denominator: int) -> float:
    return round(float(numerator) / denominator, 6) if denominator else 0.0
