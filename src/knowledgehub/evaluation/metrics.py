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
    metrics = {
        "writing_function_accuracy": _safe_rate(function_hits, function_total),
        "section_accuracy": _safe_rate(section_hits, section_total),
        "source_traceability_rate": _safe_rate(traceable, count),
        "duplicate_material_ratio": _safe_rate(duplicates, len(material_ids)),
        "wrong_domain_recall_rate": _safe_rate(wrong_domains, retrieved_domains),
        "similarity_risk_detection_rate": _safe_rate(risk_hits, risk_total),
    }
    if transferability:
        metrics["pattern_transferability"] = round(
            sum(transferability) / len(transferability), 6
        )
    if acceptance_total:
        metrics["user_acceptance_rate"] = _safe_rate(accepted, acceptance_total)
    return metrics


def evaluate_code(
    samples: Sequence[Mapping[str, Any]],
    results: Sequence[Sequence[Mapping[str, Any]]],
    *,
    latencies: Sequence[float] | None = None,
    k: int = 10,
) -> dict[str, float]:
    """Score retrieval evidence without treating answer correctness as retrieval quality."""

    if len(samples) != len(results):
        raise ValueError("sample and result counts differ")
    if latencies is not None and len(latencies) != len(samples):
        raise ValueError("latency and sample counts differ")
    source_hits = reciprocal = top_source_hits = 0.0
    version_hits = symbol_hits = evidence_complete = unsupported = 0.0
    version_total = symbol_total = conclusion_hits = conclusion_total = 0
    for sample, hits in zip(samples, results, strict=True):
        expected_source = sample.get("expected_source")
        expected_version = sample.get("version")
        expected_symbol = sample.get("expected_symbol")
        matching_hit: Mapping[str, Any] | None = None
        for rank, hit in enumerate(hits[:k], 1):
            if expected_source and hit.get("source_type") == expected_source:
                if matching_hit is None:
                    matching_hit = hit
                    source_hits += 1
                    reciprocal += 1 / rank
                if rank == 1:
                    top_source_hits += 1
            if expected_version and hit.get("version") == expected_version:
                expected_version = None
                version_hits += 1
            if expected_symbol and hit.get("symbol") == expected_symbol:
                expected_symbol = None
                symbol_hits += 1
            if hit.get("inference") is True and not (
                hit.get("source_url") or hit.get("document_id")
            ):
                unsupported += 1
        if sample.get("version"):
            version_total += 1
        if sample.get("expected_symbol"):
            symbol_total += 1
        if matching_hit is not None and _traceable_code_hit(matching_hit):
            evidence_complete += 1
        supported = sample.get("conclusion_supported")
        if isinstance(supported, bool):
            conclusion_total += 1
            conclusion_hits += supported
    count = len(samples)
    metrics = {
        "recall_at_k": _safe_rate(source_hits, count),
        "mrr": _safe_rate(reciprocal, count),
        "source_type_accuracy": _safe_rate(top_source_hits, count),
        "correct_version_recall": _safe_rate(version_hits, version_total),
        "correct_symbol_recall": _safe_rate(symbol_hits, symbol_total),
        "evidence_completeness_rate": _safe_rate(evidence_complete, count),
        "unsupported_inference_rate": _safe_rate(
            unsupported, sum(len(hits[:k]) for hits in results)
        ),
    }
    if conclusion_total:
        metrics["compatibility_conclusion_accuracy"] = _safe_rate(
            conclusion_hits, conclusion_total
        )
    if latencies:
        ordered = sorted(float(value) for value in latencies)
        index = max(0, min(len(ordered) - 1, int(0.95 * len(ordered) + 0.999999) - 1))
        metrics["mean_latency_seconds"] = round(sum(ordered) / len(ordered), 6)
        metrics["p95_latency_seconds"] = round(ordered[index], 6)
    return metrics


def _traceable_code_hit(hit: Mapping[str, Any]) -> bool:
    if not hit.get("source_type"):
        return False
    if hit.get("source_type") == "repository_profile":
        return bool(hit.get("repository") or hit.get("document_id"))
    return bool(hit.get("version") and (hit.get("source_url") or hit.get("commit")))


def _safe_rate(numerator: int | float, denominator: int) -> float:
    return round(float(numerator) / denominator, 6) if denominator else 0.0
