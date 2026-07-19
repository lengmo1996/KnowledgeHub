"""Read-only metrics and expansion gates for a controlled writing-material pilot."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from knowledgehub.core.atomic import atomic_write_json, atomic_write_text
from knowledgehub.core.hashing import sha256_json, sha256_text
from knowledgehub.writing_rag.extract import (
    EXTRACTION_DRY_RUN_SCHEMA_VERSION,
    FIXTURE_PROVIDER,
    PILOT_APPROVAL_SCHEMA_VERSION,
    PILOT_GATE_REPORT_SCHEMA_VERSION,
    WritingMaterialRuntimeConfig,
    validated_provider_origin,
)
from knowledgehub.writing_rag.review import (
    ACCEPTED_SCHEMA_VERSION,
    CANDIDATE_SCHEMA_VERSION,
    WritingMaterialReviewService,
)

PILOT_REPORT_SCHEMA_VERSION = "writing-material-pilot-report-v1"
DRY_RUN_REPORT_SCHEMA_VERSION = PILOT_GATE_REPORT_SCHEMA_VERSION
RETRIEVAL_CASE_SCHEMA_VERSION = "writing-material-retrieval-case-v1"
RETRIEVAL_REPORT_SCHEMA_VERSION = "writing-material-retrieval-evaluation-v1"
PROVIDER_PREFLIGHT_SCHEMA_VERSION = "writing-material-provider-preflight-v2"
QUALITY_AUDIT_SCHEMA_VERSION = "writing-material-quality-audit-v1"
QUALITY_REVIEW_PACKET_SCHEMA_VERSION = "writing-material-quality-review-packet-v1"

_QUALITY_TEXT_FIELDS: Mapping[str, tuple[str, ...]] = {
    "strategy": (
        "label",
        "description",
        "steps",
        "applicability",
        "claim_strength_guidance",
        "explanation_zh",
        "explanation_en",
    ),
    "template": ("template_text", "constraints", "claim_strength_guidance"),
    "phrase": ("text", "function", "position", "register", "constraints"),
}
_QUALITY_PRIMARY_TEXT_FIELD = {
    "strategy": "description",
    "template": "template_text",
    "phrase": "text",
}
_QUALITY_ID_FIELD = {
    "strategy": "strategy_id",
    "template": "template_id",
    "phrase": "phrase_id",
}


@dataclass(frozen=True, slots=True)
class PilotPolicy:
    min_documents: int = 30
    max_documents: int = 50
    minimum_provenance_pass_rate: float = 0.80
    maximum_document_failure_rate: float = 0.0
    maximum_exact_span_rejection_rate: float = 0.0
    maximum_provider_failure_rate: float = 0.0
    require_zero_provenance_failures: bool = True

    def validate(self) -> "PilotPolicy":
        if self.min_documents <= 0 or self.max_documents < self.min_documents:
            raise ValueError("pilot document bounds are invalid")
        for name, value in asdict(self).items():
            if name.endswith("_rate") and not 0 <= float(value) <= 1:
                raise ValueError(f"pilot policy {name} must be between zero and one")
        return self


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    minimum_queries: int = 5
    minimum_recall_at_k: float = 0.50
    minimum_source_join_rate: float = 1.0
    maximum_duplicate_material_ratio: float = 0.0

    def validate(self) -> "RetrievalPolicy":
        if self.minimum_queries <= 0:
            raise ValueError("retrieval policy minimum_queries must be positive")
        for name, value in asdict(self).items():
            if name.endswith("_rate") or name.endswith("_k") or name.endswith("_ratio"):
                if not 0 <= float(value) <= 1:
                    raise ValueError(f"retrieval policy {name} must be between zero and one")
        return self


@dataclass(frozen=True, slots=True)
class QualityAuditPolicy:
    minimum_quality_score: float = 0.75
    maximum_repeated_segment_occurrences: int = 2
    minimum_repeated_segment_characters: int = 12
    maximum_text_field_characters: int = 800
    maximum_near_duplicate_cluster_size: int = 1

    def validate(self) -> "QualityAuditPolicy":
        if not 0 <= self.minimum_quality_score <= 1:
            raise ValueError("quality audit minimum score must be between zero and one")
        for name in (
            "maximum_repeated_segment_occurrences",
            "minimum_repeated_segment_characters",
            "maximum_text_field_characters",
            "maximum_near_duplicate_cluster_size",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"quality audit policy {name} must be positive")
        return self


@dataclass(frozen=True, slots=True)
class PilotRetrievalOutcome:
    collection: str
    hits: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...] = ()


RetrievalQuery = Callable[[str, int], PilotRetrievalOutcome]


def provider_preflight(
    gate_report: Mapping[str, Any],
    config: WritingMaterialRuntimeConfig,
) -> dict[str, Any]:
    """Check provider readiness without resolving or emitting endpoint/secret values."""

    _validate_ready_gate(gate_report)
    if gate_report.get("version_bundle") != config.version_bundle:
        raise ValueError("provider preflight config differs from the ready gate version bundle")
    raw_base_url = os.environ.get(config.base_url_env, "").strip()
    base_url_configured = bool(raw_base_url)
    try:
        validated_provider_origin(raw_base_url)
        base_url_valid = True
    except ValueError:
        base_url_valid = False
    api_key_configured = bool(os.environ.get(config.api_key_env))
    fixture = config.provider == FIXTURE_PROVIDER
    ready = fixture or bool(config.effective_model and base_url_valid)
    report: dict[str, Any] = {
        "schema_version": PROVIDER_PREFLIGHT_SCHEMA_VERSION,
        "status": "ready" if ready else "stopped",
        "provider": config.provider,
        "model": config.effective_model,
        "version_bundle": config.version_bundle,
        "gate_artifact_fingerprint": gate_report["artifact_fingerprint"],
        "selection_sha256": gate_report["selection_sha256"],
        "environment": {
            "base_url_env": config.base_url_env,
            "base_url_configured": base_url_configured,
            "api_key_env": config.api_key_env,
            "api_key_configured": api_key_configured,
            "api_key_required_by_client": False,
        },
        "gates": {
            "ready_dry_run": True,
            "provider_supported": config.provider in {"openai_compatible", FIXTURE_PROVIDER},
            "model_configured": bool(config.effective_model),
            "base_url_valid_or_fixture": fixture or base_url_valid,
        },
        "recommendation": (
            "eligible_for_explicit_human_extraction_approval"
            if ready
            else f"configure_{config.base_url_env}_before_approval"
        ),
        "network_request_performed": False,
        "provider_client_created": False,
        "secret_values_emitted": False,
        "writes_performed": False,
    }
    report["artifact_fingerprint"] = sha256_json(report)
    return report


def create_pilot_approval(
    gate_report: Mapping[str, Any],
    *,
    output: Path,
    approver: str,
    reviewer: str,
    rights_basis: str,
    retention_policy: str,
    access_policy: str,
    provider: str,
    model: str,
    confirmed: bool,
) -> dict[str, Any]:
    """Materialize one immutable, explicit approval for a ready pilot gate."""

    if not confirmed:
        raise ValueError("pilot extraction approval requires explicit --yes confirmation")
    if output.exists():
        raise ValueError(f"refusing to overwrite an existing pilot approval: {output}")
    _validate_ready_gate(gate_report)
    text_fields = {
        "approver": approver,
        "reviewer": reviewer,
        "rights_basis": rights_basis,
        "retention_policy": retention_policy,
        "access_policy": access_policy,
        "provider": provider,
        "model": model,
    }
    for field, value in text_fields.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 1000:
            raise ValueError(f"pilot approval {field} is invalid")
        if any(ord(character) < 32 and character not in "\t" for character in value):
            raise ValueError(f"pilot approval {field} contains control characters")
    counts = gate_report["counts"]
    assert isinstance(counts, Mapping)
    report: dict[str, Any] = {
        "schema_version": PILOT_APPROVAL_SCHEMA_VERSION,
        "status": "approved_for_small_batch_extraction",
        "scope": "controlled_pilot_30_50",
        "approved_at": datetime.now(timezone.utc).isoformat(),
        **{key: value.strip() for key, value in text_fields.items()},
        "provider_execution_authorized": True,
        "secret_included": False,
        "production_index_authorized": False,
        "automatic_expansion_authorized": False,
        "selection_sha256": gate_report["selection_sha256"],
        "selected_documents": counts["selected"],
        "sections": list(gate_report["sections"]),
        "literature_checkpoint": gate_report.get("literature_checkpoint"),
        "version_bundle": gate_report["version_bundle"],
        "gate_artifact_fingerprint": gate_report["artifact_fingerprint"],
        "source_report_fingerprint": gate_report["source_report_fingerprint"],
    }
    report["artifact_fingerprint"] = sha256_json(report)
    atomic_write_json(output, report, mode=0o600)
    return report


def _validate_ready_gate(value: Mapping[str, Any]) -> None:
    fingerprinted = dict(value)
    fingerprint = fingerprinted.pop("artifact_fingerprint", None)
    if not isinstance(fingerprint, str) or fingerprint != sha256_json(fingerprinted):
        raise ValueError("pilot gate artifact fingerprint is invalid")
    if (
        value.get("schema_version") != PILOT_GATE_REPORT_SCHEMA_VERSION
        or value.get("status") != "ready"
        or value.get("recommendation") != "eligible_for_approved_small_batch_extraction"
        or value.get("real_llm_called") is not False
        or value.get("writes_performed") is not False
        or value.get("automatic_expansion_performed") is not False
    ):
        raise ValueError("only a ready dry-run gate may be approved")
    gates = value.get("gates")
    if (
        not isinstance(gates, Mapping)
        or set(gates)
        != {
            "selection_size",
            "provenance",
            "no_provenance_failures",
            "section_candidates",
        }
        or not all(item is True for item in gates.values())
    ):
        raise ValueError("pilot gate does not pass every planning gate")
    counts = value.get("counts")
    selected = counts.get("selected") if isinstance(counts, Mapping) else None
    policy = value.get("policy")
    minimum = policy.get("min_documents") if isinstance(policy, Mapping) else None
    maximum = policy.get("max_documents") if isinstance(policy, Mapping) else None
    if (
        not isinstance(selected, int)
        or isinstance(selected, bool)
        or not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or not minimum <= selected <= maximum
    ):
        raise ValueError("pilot gate selection size does not satisfy its recorded policy")
    if (
        not isinstance(policy, Mapping)
        or policy.get("maximum_document_failure_rate") != 0.0
        or policy.get("maximum_exact_span_rejection_rate") != 0.0
        or policy.get("maximum_provider_failure_rate") != 0.0
        or policy.get("require_zero_provenance_failures") is not True
    ):
        raise ValueError("pilot gate policy is incompatible with the partial-run index ban")
    for field in (
        "selection_sha256",
        "version_bundle",
        "source_report_fingerprint",
    ):
        item = value.get(field)
        if not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item):
            raise ValueError(f"pilot gate {field} is invalid")
    sections = value.get("sections")
    if (
        not isinstance(sections, list)
        or not sections
        or not all(isinstance(section, str) and section for section in sections)
        or sections != sorted(set(sections))
    ):
        raise ValueError("pilot gate sections are invalid")


class ControlledPilotEvaluator:
    """Evaluate immutable run artifacts without writing state, cache, or an index."""

    def __init__(
        self,
        review: WritingMaterialReviewService,
        policy: PilotPolicy | None = None,
        retrieval_policy: RetrievalPolicy | None = None,
    ) -> None:
        self.review = review
        self.policy = (policy or PilotPolicy()).validate()
        self.retrieval_policy = (retrieval_policy or RetrievalPolicy()).validate()

    def assess_dry_run(self, value: Mapping[str, Any]) -> dict[str, Any]:
        fingerprinted = dict(value)
        source_report_fingerprint = fingerprinted.pop("artifact_fingerprint", None)
        if (
            not isinstance(source_report_fingerprint, str)
            or source_report_fingerprint != sha256_json(fingerprinted)
        ):
            raise ValueError("extraction dry-run report artifact fingerprint is invalid")
        if value.get("schema_version") != EXTRACTION_DRY_RUN_SCHEMA_VERSION:
            raise ValueError("extraction dry-run report schema version is unsupported")
        selection_sha256 = value.get("selection_sha256")
        if not isinstance(selection_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", selection_sha256
        ):
            raise ValueError("dry-run selection fingerprint is invalid")
        raw_sections = value.get("sections")
        if (
            not isinstance(raw_sections, list)
            or not raw_sections
            or not all(isinstance(section, str) and section for section in raw_sections)
            or raw_sections != sorted(set(raw_sections))
        ):
            raise ValueError("dry-run section selection is invalid")
        version_bundle = value.get("version_bundle")
        version_manifest = value.get("version_manifest")
        if (
            not isinstance(version_bundle, str)
            or not re.fullmatch(r"[0-9a-f]{64}", version_bundle)
            or not isinstance(version_manifest, Mapping)
            or sha256_json(version_manifest) != version_bundle
        ):
            raise ValueError("dry-run version bundle is invalid")
        selected = _integer(value, "selected")
        planned = _integer(value, "planned")
        failed = _integer(value, "failed")
        candidates = _integer(value, "candidates")
        if value.get("dry_run") is not True or value.get("status") not in {"planned", "partial"}:
            raise ValueError("pilot input must be an extraction dry-run report")
        if selected <= 0 or planned < 0 or failed < 0 or planned + failed > selected:
            raise ValueError("dry-run document counts are inconsistent")
        gates = value.get("planning_gates")
        if not isinstance(gates, Mapping):
            raise ValueError("dry-run report lacks planning gate metrics")
        provenance_passed = _integer(gates, "provenance_passed")
        provenance_failed = _integer(gates, "provenance_failed")
        candidate_documents = _integer(gates, "section_candidate_documents")
        zero_candidate_documents = _integer(gates, "zero_candidate_documents")
        if (
            provenance_passed + provenance_failed != selected
            or provenance_failed != failed
            or candidate_documents + zero_candidate_documents != provenance_passed
        ):
            raise ValueError("dry-run planning gate counts are inconsistent")
        provenance_rate = _rate(provenance_passed, selected)
        selection_ok = self.policy.min_documents <= selected <= self.policy.max_documents
        provenance_ok = provenance_rate >= self.policy.minimum_provenance_pass_rate
        section_ok = candidate_documents > 0
        no_provenance_failures = (
            not self.policy.require_zero_provenance_failures or provenance_failed == 0
        )
        ready = selection_ok and provenance_ok and section_ok and no_provenance_failures
        report = {
            "schema_version": DRY_RUN_REPORT_SCHEMA_VERSION,
            "status": "ready" if ready else "stopped",
            "selection_sha256": selection_sha256,
            "sections": list(raw_sections),
            "literature_checkpoint": value.get("literature_checkpoint"),
            "version_bundle": version_bundle,
            "source_report_fingerprint": source_report_fingerprint,
            "counts": {
                "selected": selected,
                "planned": planned,
                "failed": failed,
                "candidate_paragraphs": candidates,
                "section_candidate_documents": candidate_documents,
                "zero_candidate_documents": zero_candidate_documents,
            },
            "rates": {
                "provenance_pass_rate": provenance_rate,
                "section_candidate_document_rate": _rate(
                    candidate_documents, provenance_passed
                ),
            },
            "gates": {
                "selection_size": selection_ok,
                "provenance": provenance_ok,
                "no_provenance_failures": no_provenance_failures,
                "section_candidates": section_ok,
            },
            "policy": asdict(self.policy),
            "recommendation": (
                "eligible_for_approved_small_batch_extraction"
                if ready
                else "fix_selection_or_provenance_before_extraction"
            ),
            "real_llm_called": False,
            "writes_performed": False,
            "automatic_expansion_performed": False,
        }
        report["artifact_fingerprint"] = sha256_json(report)
        return report

    def evaluate(
        self,
        run_id: str,
        *,
        candidate_report: Mapping[str, Any] | None = None,
        retrieval_report: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_dir = self.review.run_dir(run_id)
        manifest = _read_object(run_dir / "manifest.json")
        if manifest.get("run_id") != run_id:
            raise ValueError("pilot run ID differs from its manifest")
        selected = _integer(manifest, "selected")
        failed_documents = _integer(manifest, "failed")
        validation = self.review.validate(run_id, verify_source=True)
        failures = _read_jsonl(run_dir / "failures.jsonl")
        records = {
            "evidence": _read_jsonl(run_dir / "evidence.jsonl"),
            "strategy": _read_jsonl(run_dir / "strategies.jsonl"),
            "template": _read_jsonl(run_dir / "templates.jsonl"),
            "phrase": _read_jsonl(run_dir / "phrases.jsonl"),
        }
        error_codes = Counter(str(item.get("error_code") or "unknown") for item in failures)
        exact_rejected = error_codes["exact_span_rejected"]
        provider_failures = sum(
            count
            for code, count in error_codes.items()
            if code.startswith("provider_")
            or code in {"extraction_error", "invalid_json", "schema_validation_error"}
        )
        evidence_count = len(records["evidence"])
        document_failure_rate = _rate(failed_documents, selected)
        exact_rejection_rate = _rate(exact_rejected, exact_rejected + evidence_count)
        provider_failure_rate = _rate(provider_failures, selected)
        accepted_manifest_path = run_dir / "accepted" / "manifest.json"
        accepted = (
            _read_object(accepted_manifest_path) if accepted_manifest_path.is_file() else None
        )
        accepted_counts = (
            {str(key): int(count) for key, count in accepted.get("counts", {}).items()}
            if isinstance(accepted, Mapping) and isinstance(accepted.get("counts"), Mapping)
            else {}
        )
        complete_review = bool(
            isinstance(accepted, Mapping)
            and accepted.get("review_completeness") == "complete"
            and accepted.get("pending_count") == 0
            and validation.get("index_eligible")
        )
        candidate = _candidate_metrics(
            candidate_report,
            accepted_counts,
            run_id=run_id,
            accepted_manifest_sha256=(
                sha256_text(accepted_manifest_path.read_text(encoding="utf-8"))
                if accepted_manifest_path.is_file()
                else None
            ),
        )
        retrieval = _retrieval_metrics(
            retrieval_report,
            run_id=run_id,
            candidate_fingerprint=(
                candidate_report.get("artifact_fingerprint")
                if isinstance(candidate_report, Mapping)
                else None
            ),
            candidate_collection=candidate.get("collection"),
            expected_policy=self.retrieval_policy,
        )
        selection_ok = self.policy.min_documents <= selected <= self.policy.max_documents
        extraction_ok = bool(
            manifest.get("status") == "success"
            and document_failure_rate <= self.policy.maximum_document_failure_rate
            and validation.get("status") == "success"
        )
        source_ok = bool(validation.get("source_verified") and not validation.get("errors"))
        exact_ok = exact_rejection_rate <= self.policy.maximum_exact_span_rejection_rate
        provider_ok = provider_failure_rate <= self.policy.maximum_provider_failure_rate
        gates = {
            "selection_size": selection_ok,
            "extraction": extraction_ok,
            "source_join": source_ok,
            "exact_span": exact_ok,
            "provider_structure": provider_ok,
            "complete_review": complete_review,
            "isolated_candidate": candidate["passed"],
            "retrieval_quality": retrieval["passed"],
        }
        recommendation = _recommendation(gates, candidate_report, retrieval_report)
        all_passed = all(gates.values())
        assets = [item for values in records.values() for item in values]
        return {
            "schema_version": PILOT_REPORT_SCHEMA_VERSION,
            "status": "eligible_for_manual_expansion_decision" if all_passed else "incomplete",
            "run_id": run_id,
            "counts": {
                "selected_documents": selected,
                "processed_documents": _integer(manifest, "processed"),
                "failed_documents": failed_documents,
                "candidate_paragraphs": _integer(manifest, "candidates"),
                "assets": {key: len(values) for key, values in records.items()},
                "accepted_assets": accepted_counts,
            },
            "rates": {
                "document_failure_rate": document_failure_rate,
                "exact_span_rejection_rate": exact_rejection_rate,
                "provider_failure_rate": provider_failure_rate,
                "review_acceptance_rate": _rate(
                    sum(accepted_counts.values()),
                    int(accepted.get("target_count", 0)) if isinstance(accepted, Mapping) else 0,
                ),
            },
            "distributions": {
                "failure_code": dict(sorted(error_codes.items())),
                "category": _distribution(assets, "category"),
                "language": _distribution(assets, "language"),
                "quality_score": _quality_distribution(assets),
            },
            "review": {
                "validation_status": validation.get("status"),
                "errors": list(validation.get("errors") or ()),
                "review_counts": dict(validation.get("review_counts") or {}),
                "complete": complete_review,
            },
            "candidate": candidate,
            "retrieval": retrieval,
            "gates": gates,
            "policy": asdict(self.policy),
            "retrieval_policy": asdict(self.retrieval_policy),
            "recommendation": recommendation,
            "manual_expansion_decision_required": True,
            "automatic_expansion_performed": False,
        }


class CandidateRetrievalEvaluator:
    """Generate retrieval evidence from approved cases and a physical candidate."""

    def __init__(
        self,
        review: WritingMaterialReviewService,
        policy: RetrievalPolicy | None = None,
    ) -> None:
        self.review = review
        self.policy = (policy or RetrievalPolicy()).validate()

    def evaluate(
        self,
        run_id: str,
        *,
        candidate_report: Mapping[str, Any],
        cases: Sequence[Mapping[str, Any]],
        query: RetrievalQuery,
    ) -> dict[str, Any]:
        validation = self.review.validate(run_id, verify_source=True)
        if validation.get("status") != "success" or not validation.get("index_eligible"):
            raise ValueError("retrieval evaluation requires a source-verified complete review")
        run_dir = self.review.run_dir(run_id)
        accepted_dir = run_dir / "accepted"
        accepted_manifest = _read_object(accepted_dir / "manifest.json")
        counts = accepted_manifest.get("counts")
        if not isinstance(counts, Mapping):
            raise ValueError("accepted snapshot counts are invalid")
        candidate = _candidate_metrics(
            candidate_report,
            counts,
            run_id=run_id,
            accepted_manifest_sha256=sha256_text(
                (accepted_dir / "manifest.json").read_text(encoding="utf-8")
            ),
        )
        if not candidate["passed"]:
            raise ValueError("retrieval evaluation requires a verified isolated candidate report")
        evidences = {
            str(value["evidence_id"]): value
            for value in _read_jsonl(accepted_dir / "evidence.jsonl")
        }
        assets: dict[str, dict[str, Any]] = {}
        for filename, id_field in (
            ("strategies.jsonl", "strategy_id"),
            ("templates.jsonl", "template_id"),
            ("phrases.jsonl", "phrase_id"),
        ):
            for value in _read_jsonl(accepted_dir / filename):
                assets[str(value[id_field])] = value
        normalized_cases = _validate_retrieval_cases(cases, assets)
        case_results: list[dict[str, Any]] = []
        relevant_cases = joined_hits = material_hits = duplicate_hits = 0
        reciprocal = 0.0
        source_join_failures = 0
        all_warnings: set[str] = set()
        candidate_collection = str(candidate["collection"])
        for case in normalized_cases:
            outcome = query(str(case["query"]), int(case["top_k"]))
            if outcome.collection != candidate_collection:
                raise ValueError("retrieval response collection differs from the candidate")
            all_warnings.update(str(value) for value in outcome.warnings)
            selected_hits = list(outcome.hits[: int(case["top_k"])])
            material_payloads = [
                value for value in selected_hits if value.get("accepted_snapshot_only") is True
            ]
            identifiers = [str(value.get("document_id") or "") for value in material_payloads]
            duplicate_hits += len(identifiers) - len(set(identifiers))
            material_hits += len(material_payloads)
            expected = set(str(value) for value in case["expected_asset_ids"])
            first_rank = next(
                (
                    index
                    for index, value in enumerate(selected_hits, 1)
                    if value.get("accepted_snapshot_only") is True
                    and str(value.get("document_id") or "") in expected
                ),
                None,
            )
            if first_rank is not None:
                relevant_cases += 1
                reciprocal += 1 / first_rank
            join_errors: list[str] = []
            case_join_failure_hits = 0
            for payload in material_payloads:
                errors = _source_join_errors(payload, assets, evidences)
                if errors:
                    source_join_failures += 1
                    case_join_failure_hits += 1
                    join_errors.extend(errors)
                else:
                    joined_hits += 1
            case_results.append(
                {
                    "case_id": case["case_id"],
                    "expected_asset_ids": list(case["expected_asset_ids"]),
                    "retrieved_material_ids": identifiers,
                    "first_relevant_rank": first_rank,
                    "source_join_failure_hits": case_join_failure_hits,
                    "source_join_errors": sorted(set(join_errors)),
                    "warnings": sorted(str(value) for value in outcome.warnings),
                }
            )
        query_count = len(normalized_cases)
        metrics = {
            "recall_at_k": _rate(relevant_cases, query_count),
            "mrr": _rate(reciprocal, query_count),
            "source_join_rate": _rate(joined_hits, material_hits),
            "duplicate_material_ratio": _rate(duplicate_hits, material_hits),
        }
        passed = bool(
            query_count >= self.policy.minimum_queries
            and metrics["recall_at_k"] >= self.policy.minimum_recall_at_k
            and metrics["source_join_rate"] >= self.policy.minimum_source_join_rate
            and metrics["duplicate_material_ratio"]
            <= self.policy.maximum_duplicate_material_ratio
            and source_join_failures == 0
        )
        report: dict[str, Any] = {
            "schema_name": "writing_material_retrieval_evaluation",
            "schema_version": RETRIEVAL_REPORT_SCHEMA_VERSION,
            "status": "success" if passed else "failed",
            "passed": passed,
            "run_id": run_id,
            "candidate_collection": candidate_collection,
            "candidate_artifact_fingerprint": candidate_report.get("artifact_fingerprint"),
            "queries_sha256": sha256_json(normalized_cases),
            "queries": query_count,
            "source_join_failures": source_join_failures,
            "metrics": metrics,
            "cases": case_results,
            "warnings": sorted(all_warnings),
            "policy": asdict(self.policy),
            "writes_performed": False,
            "promotion_performed": False,
        }
        report["artifact_fingerprint"] = sha256_json(report)
        return report


class AcceptedCorpusQualityAuditor:
    """Audit one complete accepted snapshot without copying text or changing state."""

    def __init__(
        self,
        review: WritingMaterialReviewService,
        policy: QualityAuditPolicy | None = None,
    ) -> None:
        self.review = review
        self.policy = (policy or QualityAuditPolicy()).validate()

    def audit(self, run_id: str) -> dict[str, Any]:
        validation = self.review.validate(run_id, verify_source=True)
        if validation.get("status") != "success" or not validation.get("index_eligible"):
            raise ValueError("quality audit requires a source-verified complete review")
        accepted_dir = self.review.run_dir(run_id) / "accepted"
        manifest_path = accepted_dir / "manifest.json"
        manifest = _read_object(manifest_path)
        if (
            manifest.get("schema_version") != ACCEPTED_SCHEMA_VERSION
            or manifest.get("review_completeness") != "complete"
            or manifest.get("pending_count") != 0
        ):
            raise ValueError("quality audit requires a complete accepted-v2 snapshot")

        assets: dict[str, list[dict[str, Any]]] = {
            "strategy": _read_jsonl(accepted_dir / "strategies.jsonl"),
            "template": _read_jsonl(accepted_dir / "templates.jsonl"),
            "phrase": _read_jsonl(accepted_dir / "phrases.jsonl"),
        }
        counts = manifest.get("counts")
        if not isinstance(counts, Mapping) or any(
            counts.get(asset_type) != len(values) for asset_type, values in assets.items()
        ):
            raise ValueError("accepted snapshot counts differ from quality audit inputs")

        findings, clusters = _quality_findings(assets, self.policy)
        flagged_assets = sorted(
            {
                str(asset_id)
                for finding in findings
                for asset_id in finding.get("asset_ids", ())
            }
        )
        finding_counts = dict(sorted(Counter(str(item["code"]) for item in findings).items()))
        severity_counts = dict(
            sorted(Counter(str(item["severity"]) for item in findings).items())
        )
        asset_counts = {key: len(value) for key, value in assets.items()}
        total_assets = sum(asset_counts.values())
        low_quality_assets = sum(
            1
            for values in assets.values()
            for value in values
            if float(value["quality_score"]) < self.policy.minimum_quality_score
        )
        clustered_assets = sum(int(value["size"]) for value in clusters)
        gates = {
            "complete_source_verified_snapshot": True,
            "no_low_quality_scores": "low_quality_score" not in finding_counts,
            "no_repeated_segments": "repeated_text_segment" not in finding_counts,
            "no_oversized_text_fields": "oversized_text_field" not in finding_counts,
            "no_repeated_list_items": "repeated_list_item" not in finding_counts,
            "no_exact_duplicate_primary_text": "exact_duplicate_primary_text" not in finding_counts,
            "no_multi_member_lexical_clusters": "near_duplicate_cluster" not in finding_counts,
        }
        passed = all(gates.values())
        report: dict[str, Any] = {
            "schema_name": "writing_material_quality_audit",
            "schema_version": QUALITY_AUDIT_SCHEMA_VERSION,
            "status": "success",
            "passed": passed,
            "run_id": run_id,
            "accepted_manifest": str(manifest_path),
            "accepted_manifest_sha256": sha256_text(manifest_path.read_text(encoding="utf-8")),
            "policy": asdict(self.policy),
            "counts": {
                "assets": {**asset_counts, "total": total_assets},
                "findings": finding_counts,
                "findings_total": len(findings),
                "severities": severity_counts,
                "flagged_assets": len(flagged_assets),
                "multi_member_clusters": len(clusters),
            },
            "metrics": {
                "flagged_asset_rate": _rate(len(flagged_assets), total_assets),
                "low_quality_asset_rate": _rate(low_quality_assets, total_assets),
                "near_duplicate_clustered_asset_rate": _rate(clustered_assets, total_assets),
            },
            "gates": gates,
            "clusters": clusters,
            "findings": findings,
            "recommendation": (
                "quality_gate_passed" if passed else "manual_review_flagged_assets"
            ),
            "source_text_included": False,
            "review_decisions_modified": False,
            "accepted_snapshot_modified": False,
            "index_modified": False,
            "llm_called": False,
            "writes_performed": False,
        }
        report["artifact_fingerprint"] = sha256_json(report)
        return report


class AcceptedCorpusQualityReviewRenderer:
    """Render a non-importable local review packet for quality findings."""

    def __init__(self, review: WritingMaterialReviewService) -> None:
        self.review = review

    def render(
        self,
        run_id: str,
        *,
        quality_report: Mapping[str, Any],
        reviewer: str,
        output_dir: Path,
    ) -> dict[str, Any]:
        if not reviewer.strip() or len(reviewer.strip()) > 200:
            raise ValueError("quality review packet reviewer is invalid")
        if output_dir.exists():
            raise ValueError(f"refusing to overwrite quality review output: {output_dir}")
        validation = self.review.validate(run_id, verify_source=True)
        if validation.get("status") != "success" or not validation.get("index_eligible"):
            raise ValueError("quality review packet requires a source-verified complete review")
        run_dir = self.review.run_dir(run_id)
        accepted_dir = run_dir / "accepted"
        manifest_path = accepted_dir / "manifest.json"
        manifest_hash = sha256_text(manifest_path.read_text(encoding="utf-8"))
        policy = _validate_quality_audit_report(
            quality_report,
            run_id=run_id,
            accepted_manifest_sha256=manifest_hash,
        )

        filenames = {
            "strategy": "strategies.jsonl",
            "template": "templates.jsonl",
            "phrase": "phrases.jsonl",
        }
        accepted_assets: dict[str, tuple[str, dict[str, Any]]] = {}
        raw_assets: dict[str, dict[str, Any]] = {}
        for asset_type, filename in filenames.items():
            id_field = _QUALITY_ID_FIELD[asset_type]
            for value in _read_jsonl(accepted_dir / filename):
                accepted_assets[str(value[id_field])] = (asset_type, value)
            for value in _read_jsonl(run_dir / filename):
                raw_assets[str(value[id_field])] = value

        grouped: dict[str, list[dict[str, Any]]] = {}
        raw_findings = quality_report.get("findings")
        assert isinstance(raw_findings, list)
        for finding in raw_findings:
            assert isinstance(finding, Mapping)
            for asset_id in finding["asset_ids"]:
                grouped.setdefault(str(asset_id), []).append(dict(finding))
        unknown = sorted(set(grouped) - set(accepted_assets))
        if unknown:
            raise ValueError("quality audit references assets outside the accepted snapshot")

        items: list[dict[str, Any]] = []
        for asset_id in sorted(grouped):
            asset_type, accepted = accepted_assets[asset_id]
            raw = raw_assets.get(asset_id)
            if raw is None:
                raise ValueError("accepted quality finding lacks its immutable run asset")
            findings = sorted(
                grouped[asset_id],
                key=lambda value: (str(value["code"]), str(value.get("field") or "")),
            )
            proposed_edits = _quality_proposed_edits(accepted, findings, policy)
            recommendation = _quality_review_recommendation(findings, proposed_edits)
            current_fields = {
                "category": accepted["category"],
                "language": accepted["language"],
                "quality_score": accepted["quality_score"],
                **{
                    field: accepted[field]
                    for field in _QUALITY_TEXT_FIELDS[asset_type]
                    if field in accepted
                },
            }
            items.append(
                {
                    "asset_id": asset_id,
                    "asset_type": asset_type,
                    "based_on_hash": sha256_json(raw),
                    "findings": findings,
                    "recommended_action": recommendation,
                    "current_material_fields": current_fields,
                    "proposed_edits": proposed_edits,
                    "decision_draft": {
                        "asset_id": asset_id,
                        "decision": None,
                        "based_on_hash": sha256_json(raw),
                        "reviewer": reviewer.strip(),
                        "reason": None,
                        "edits": proposed_edits,
                    },
                }
            )

        markdown = _quality_review_markdown(
            run_id,
            reviewer.strip(),
            str(quality_report["artifact_fingerprint"]),
            items,
        )
        packet_path = output_dir / "quality-review-packet.json"
        markdown_path = output_dir / "quality-review.md"
        recommendation_counts = dict(
            sorted(Counter(str(item["recommended_action"]) for item in items).items())
        )
        packet: dict[str, Any] = {
            "schema_name": "writing_material_quality_review_packet",
            "schema_version": QUALITY_REVIEW_PACKET_SCHEMA_VERSION,
            "status": "success",
            "run_id": run_id,
            "reviewer": reviewer.strip(),
            "quality_audit_fingerprint": quality_report["artifact_fingerprint"],
            "accepted_manifest_sha256": manifest_hash,
            "counts": {
                "flagged_assets": len(items),
                "recommendations": recommendation_counts,
            },
            "items": items,
            "packet_path": str(packet_path.resolve()),
            "markdown_path": str(markdown_path.resolve()),
            "markdown_sha256": sha256_text(markdown),
            "decision_import_ready": False,
            "requires_explicit_reviewer_decision": True,
            "evidence_text_included": False,
            "provenance_excerpt_included": False,
            "derived_material_text_included": True,
            "review_decisions_modified": False,
            "accepted_snapshot_modified": False,
            "index_modified": False,
            "llm_called": False,
            "report_files_written": True,
        }
        packet["artifact_fingerprint"] = sha256_json(packet)
        output_dir.mkdir(parents=True, mode=0o700)
        atomic_write_text(markdown_path, markdown, mode=0o600)
        atomic_write_json(packet_path, packet, mode=0o600)
        return packet


def _candidate_metrics(
    value: Mapping[str, Any] | None,
    accepted_counts: Mapping[str, int],
    *,
    run_id: str,
    accepted_manifest_sha256: str | None,
) -> dict[str, Any]:
    if value is None:
        return {"provided": False, "passed": False}
    fingerprinted = dict(value)
    fingerprint = fingerprinted.pop("artifact_fingerprint", None)
    fingerprint_valid = fingerprint == sha256_json(fingerprinted)
    expected = sum(accepted_counts.get(key, 0) for key in ("strategy", "template", "phrase"))
    indexed = _integer(value, "indexed")
    selected = _integer(value, "selected")
    collection = value.get("candidate_collection")
    passed = bool(
        fingerprint_valid
        and value.get("schema_name") == "writing_material_candidate"
        and value.get("schema_version") == CANDIDATE_SCHEMA_VERSION
        and value.get("run_id") == run_id
        and value.get("status") == "success"
        and value.get("source_verified") is True
        and accepted_manifest_sha256 is not None
        and value.get("accepted_manifest_sha256") == accepted_manifest_sha256
        and isinstance(value.get("candidate_data_dir"), str)
        and bool(str(value.get("candidate_data_dir")))
        and value.get("accepted_only") is True
        and value.get("promotion_performed") is False
        and value.get("dry_run") is False
        and not value.get("failures")
        and expected > 0
        and selected == expected
        and indexed == expected
        and isinstance(collection, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", collection)
        and collection != "knowledgehub_writing_current"
    )
    return {
        "provided": True,
        "passed": passed,
        "collection": collection,
        "selected": selected,
        "indexed": indexed,
        "expected": expected,
        "promotion_performed": value.get("promotion_performed"),
        "fingerprint_valid": fingerprint_valid,
    }


def _retrieval_metrics(
    value: Mapping[str, Any] | None,
    *,
    run_id: str,
    candidate_fingerprint: Any,
    candidate_collection: Any,
    expected_policy: RetrievalPolicy,
) -> dict[str, Any]:
    if value is None:
        return {"provided": False, "passed": False}
    fingerprinted = dict(value)
    fingerprint = fingerprinted.pop("artifact_fingerprint", None)
    fingerprint_valid = fingerprint == sha256_json(fingerprinted)
    queries = _integer(value, "queries")
    source_join_failures = _integer(value, "source_join_failures")
    cases = value.get("cases")
    policy = value.get("policy")
    metrics = value.get("metrics")
    structure_valid = bool(
        isinstance(cases, list)
        and len(cases) == queries
        and all(isinstance(item, Mapping) for item in cases)
        and sum(
            int(item.get("source_join_failure_hits", -1))
            for item in cases
            if isinstance(item, Mapping)
        )
        == source_join_failures
        and isinstance(policy, Mapping)
        and isinstance(metrics, Mapping)
    )
    threshold_valid = False
    if structure_valid and isinstance(policy, Mapping) and isinstance(metrics, Mapping):
        try:
            retrieval_policy = RetrievalPolicy(
                minimum_queries=_integer(policy, "minimum_queries"),
                minimum_recall_at_k=_number(policy, "minimum_recall_at_k"),
                minimum_source_join_rate=_number(policy, "minimum_source_join_rate"),
                maximum_duplicate_material_ratio=_number(
                    policy, "maximum_duplicate_material_ratio"
                ),
            ).validate()
            recall = _number(metrics, "recall_at_k")
            source_join_rate = _number(metrics, "source_join_rate")
            duplicate_ratio = _number(metrics, "duplicate_material_ratio")
            _number(metrics, "mrr")
            threshold_valid = bool(
                queries >= retrieval_policy.minimum_queries
                and recall >= retrieval_policy.minimum_recall_at_k
                and source_join_rate >= retrieval_policy.minimum_source_join_rate
                and duplicate_ratio <= retrieval_policy.maximum_duplicate_material_ratio
                and source_join_failures == 0
            )
        except (TypeError, ValueError):
            threshold_valid = False
    passed = bool(
        fingerprint_valid
        and value.get("schema_name") == "writing_material_retrieval_evaluation"
        and value.get("schema_version") == RETRIEVAL_REPORT_SCHEMA_VERSION
        and value.get("run_id") == run_id
        and value.get("candidate_artifact_fingerprint") == candidate_fingerprint
        and value.get("candidate_collection") == candidate_collection
        and value.get("status") == "success"
        and value.get("passed") is True
        and value.get("writes_performed") is False
        and value.get("promotion_performed") is False
        and policy == asdict(expected_policy)
        and structure_valid
        and threshold_valid
    )
    return {
        "provided": True,
        "passed": passed,
        "queries": queries,
        "source_join_failures": source_join_failures,
        "fingerprint_valid": fingerprint_valid,
        "metrics": dict(metrics) if isinstance(metrics, Mapping) else {},
    }


def _recommendation(
    gates: Mapping[str, bool],
    candidate: Mapping[str, Any] | None,
    retrieval: Mapping[str, Any] | None,
) -> str:
    for name in ("selection_size", "extraction", "source_join", "exact_span", "provider_structure"):
        if not gates[name]:
            return "stop_and_fix_extraction_contract"
    if not gates["complete_review"]:
        return "complete_all_review_decisions"
    if candidate is None or not gates["isolated_candidate"]:
        return "build_or_fix_isolated_candidate"
    if retrieval is None or not gates["retrieval_quality"]:
        return "run_or_fix_candidate_retrieval_evaluation"
    return "eligible_for_manual_expansion_decision"


def _validate_retrieval_cases(
    values: Sequence[Mapping[str, Any]],
    assets: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not values:
        raise ValueError("retrieval evaluation cases are empty")
    expected_fields = {
        "schema_version",
        "case_id",
        "query",
        "expected_asset_ids",
        "top_k",
    }
    identifiers: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, value in enumerate(values, 1):
        if set(value) != expected_fields:
            raise ValueError(f"retrieval case {index} has an invalid closed-world schema")
        case_id = value.get("case_id")
        query = value.get("query")
        expected = value.get("expected_asset_ids")
        top_k = value.get("top_k")
        if value.get("schema_version") != RETRIEVAL_CASE_SCHEMA_VERSION:
            raise ValueError(f"retrieval case {index} has an invalid schema version")
        if (
            not isinstance(case_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", case_id)
            or case_id in identifiers
        ):
            raise ValueError(f"retrieval case {index} has an invalid or duplicate ID")
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError(f"retrieval case {index} has an invalid query")
        if (
            not isinstance(expected, list)
            or not expected
            or any(not isinstance(item, str) or not item for item in expected)
            or len(set(expected)) != len(expected)
            or any(item not in assets for item in expected)
        ):
            raise ValueError(f"retrieval case {index} has invalid expected asset IDs")
        if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 100:
            raise ValueError(f"retrieval case {index} has an invalid top_k")
        identifiers.add(case_id)
        normalized.append(
            {
                "schema_version": RETRIEVAL_CASE_SCHEMA_VERSION,
                "case_id": case_id,
                "query": query.strip(),
                "expected_asset_ids": list(expected),
                "top_k": top_k,
            }
        )
    return normalized


def _source_join_errors(
    payload: Mapping[str, Any],
    assets: Mapping[str, Mapping[str, Any]],
    evidences: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    asset_id = str(payload.get("document_id") or "")
    asset = assets.get(asset_id)
    if asset is None:
        return [f"{asset_id or 'missing'}:unknown_accepted_asset"]
    errors: list[str] = []
    expected_evidence_ids = [str(value) for value in asset.get("evidence_ids") or ()]
    expected_provenance: list[dict[str, Any]] = []
    for evidence_id in expected_evidence_ids:
        evidence = evidences.get(evidence_id)
        if evidence is None:
            errors.append(f"{asset_id}:accepted_evidence_missing")
            continue
        expected_provenance.append(
            {
                "evidence_id": evidence_id,
                "document_id": evidence["document_id"],
                "zotero_item_key": evidence["zotero_item_key"],
                "attachment_key": evidence["attachment_key"],
                "section": evidence["section_title"],
                "page_start": evidence["page_start"],
                "page_end": evidence["page_end"],
                "paragraph_id": evidence["paragraph_id"],
                "char_start": evidence["char_start"],
                "char_end": evidence["char_end"],
                "excerpt": str(evidence["original_text"])[:240],
            }
        )
    expected = {
        "asset_type": asset.get("asset_type"),
        "category": asset.get("category"),
        "language": asset.get("language"),
        "quality_score": asset.get("quality_score"),
        "evidence_ids": expected_evidence_ids,
        "provenance": expected_provenance,
        "accepted_snapshot_only": True,
    }
    for field, expected_value in expected.items():
        if payload.get(field) != expected_value:
            errors.append(f"{asset_id}:{field}_mismatch")
    return errors


def _quality_findings(
    assets: Mapping[str, Sequence[Mapping[str, Any]]],
    policy: QualityAuditPolicy,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    clusters: list[dict[str, Any]] = []
    for asset_type in ("strategy", "template", "phrase"):
        values = assets.get(asset_type, ())
        id_field = _QUALITY_ID_FIELD[asset_type]
        exact_groups: dict[tuple[str, str, str], list[str]] = {}
        cluster_groups: dict[str, list[str]] = {}
        for value in values:
            asset_id = str(value[id_field])
            score = float(value["quality_score"])
            if score < policy.minimum_quality_score:
                findings.append(
                    _quality_finding(
                        "low_quality_score",
                        "warning",
                        asset_type,
                        [asset_id],
                        observed=score,
                        threshold=policy.minimum_quality_score,
                    )
                )
            for field in _QUALITY_TEXT_FIELDS[asset_type]:
                raw = value.get(field)
                field_values = list(raw) if isinstance(raw, list) else [raw]
                normalized_items = [
                    _normalized_quality_text(item) for item in field_values if isinstance(item, str)
                ]
                if isinstance(raw, list) and len(set(normalized_items)) != len(normalized_items):
                    findings.append(
                        _quality_finding(
                            "repeated_list_item",
                            "error",
                            asset_type,
                            [asset_id],
                            field=field,
                            observed=len(normalized_items) - len(set(normalized_items)),
                            threshold=0,
                        )
                    )
                for item in field_values:
                    if not isinstance(item, str):
                        continue
                    if len(item) > policy.maximum_text_field_characters:
                        findings.append(
                            _quality_finding(
                                "oversized_text_field",
                                "warning",
                                asset_type,
                                [asset_id],
                                field=field,
                                observed=len(item),
                                threshold=policy.maximum_text_field_characters,
                            )
                        )
                    repeated = _maximum_repeated_segment(item, policy)
                    if repeated > policy.maximum_repeated_segment_occurrences:
                        findings.append(
                            _quality_finding(
                                "repeated_text_segment",
                                "error",
                                asset_type,
                                [asset_id],
                                field=field,
                                observed=repeated,
                                threshold=policy.maximum_repeated_segment_occurrences,
                            )
                        )
            primary = _normalized_quality_text(str(value[_QUALITY_PRIMARY_TEXT_FIELD[asset_type]]))
            exact_groups.setdefault(
                (str(value.get("category")), str(value.get("language")), primary), []
            ).append(asset_id)
            cluster_id = value.get("cluster_id")
            if isinstance(cluster_id, str) and cluster_id:
                cluster_groups.setdefault(cluster_id, []).append(asset_id)
        for asset_ids in exact_groups.values():
            if len(asset_ids) > 1:
                findings.append(
                    _quality_finding(
                        "exact_duplicate_primary_text",
                        "error",
                        asset_type,
                        sorted(asset_ids),
                        observed=len(asset_ids),
                        threshold=1,
                    )
                )
        for cluster_id, asset_ids in cluster_groups.items():
            if len(asset_ids) <= policy.maximum_near_duplicate_cluster_size:
                continue
            cluster = {
                "asset_type": asset_type,
                "cluster_id": cluster_id,
                "size": len(asset_ids),
                "asset_ids": sorted(asset_ids),
            }
            clusters.append(cluster)
            findings.append(
                _quality_finding(
                    "near_duplicate_cluster",
                    "warning",
                    asset_type,
                    sorted(asset_ids),
                    observed=len(asset_ids),
                    threshold=policy.maximum_near_duplicate_cluster_size,
                )
            )
    findings.sort(
        key=lambda value: (
            str(value["code"]),
            str(value["asset_type"]),
            str(value.get("field") or ""),
            tuple(value["asset_ids"]),
        )
    )
    clusters.sort(key=lambda value: (str(value["asset_type"]), str(value["cluster_id"])))
    return findings, clusters


def _validate_quality_audit_report(
    value: Mapping[str, Any],
    *,
    run_id: str,
    accepted_manifest_sha256: str,
) -> QualityAuditPolicy:
    fingerprinted = dict(value)
    fingerprint = fingerprinted.pop("artifact_fingerprint", None)
    if not isinstance(fingerprint, str) or fingerprint != sha256_json(fingerprinted):
        raise ValueError("quality audit artifact fingerprint is invalid")
    policy_raw = value.get("policy")
    if not isinstance(policy_raw, Mapping):
        raise ValueError("quality audit policy is invalid")
    try:
        policy = QualityAuditPolicy(
            minimum_quality_score=float(policy_raw["minimum_quality_score"]),
            maximum_repeated_segment_occurrences=int(
                policy_raw["maximum_repeated_segment_occurrences"]
            ),
            minimum_repeated_segment_characters=int(
                policy_raw["minimum_repeated_segment_characters"]
            ),
            maximum_text_field_characters=int(policy_raw["maximum_text_field_characters"]),
            maximum_near_duplicate_cluster_size=int(
                policy_raw["maximum_near_duplicate_cluster_size"]
            ),
        ).validate()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("quality audit policy is invalid") from exc
    findings = value.get("findings")
    counts = value.get("counts")
    counts_mapping = counts if isinstance(counts, Mapping) else {}
    finding_counts = counts_mapping.get("findings")
    valid_findings = bool(
        isinstance(findings, list)
        and findings
        and all(
            isinstance(item, Mapping)
            and isinstance(item.get("code"), str)
            and isinstance(item.get("severity"), str)
            and isinstance(item.get("asset_type"), str)
            and isinstance(item.get("asset_ids"), list)
            and bool(item.get("asset_ids"))
            and all(isinstance(asset_id, str) and asset_id for asset_id in item["asset_ids"])
            for item in findings
        )
        and isinstance(finding_counts, Mapping)
        and dict(finding_counts)
        == dict(sorted(Counter(str(item["code"]) for item in findings).items()))
        and counts_mapping.get("findings_total") == len(findings)
    )
    if (
        value.get("schema_name") != "writing_material_quality_audit"
        or value.get("schema_version") != QUALITY_AUDIT_SCHEMA_VERSION
        or value.get("status") != "success"
        or value.get("passed") is not False
        or value.get("run_id") != run_id
        or value.get("accepted_manifest_sha256") != accepted_manifest_sha256
        or value.get("recommendation") != "manual_review_flagged_assets"
        or value.get("source_text_included") is not False
        or value.get("review_decisions_modified") is not False
        or value.get("accepted_snapshot_modified") is not False
        or value.get("index_modified") is not False
        or value.get("llm_called") is not False
        or not valid_findings
    ):
        raise ValueError("quality review packet requires a valid failed quality audit")
    return policy


def _quality_proposed_edits(
    accepted: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
    policy: QualityAuditPolicy,
) -> dict[str, Any]:
    proposed: dict[str, Any] = {}
    repeated_fields = {
        str(value["field"])
        for value in findings
        if value.get("code") == "repeated_text_segment" and value.get("field")
    }
    repeated_list_fields = {
        str(value["field"])
        for value in findings
        if value.get("code") == "repeated_list_item" and value.get("field")
    }
    for field in sorted(repeated_fields):
        raw = accepted.get(field)
        cleaned_value: Any
        if isinstance(raw, str):
            cleaned_value = _deduplicate_text_segments(raw, policy)
        elif isinstance(raw, list):
            cleaned_value = [
                _deduplicate_text_segments(item, policy) if isinstance(item, str) else item
                for item in raw
            ]
        else:
            continue
        if cleaned_value != raw:
            proposed[field] = cleaned_value
    for field in sorted(repeated_list_fields):
        raw = accepted.get(field)
        if not isinstance(raw, list):
            continue
        seen: set[str] = set()
        cleaned_list: list[Any] = []
        for item in raw:
            identity = _normalized_quality_text(item) if isinstance(item, str) else repr(item)
            if identity not in seen:
                seen.add(identity)
                cleaned_list.append(item)
        if cleaned_list != raw:
            proposed[field] = cleaned_list
    return proposed


def _quality_review_recommendation(
    findings: Sequence[Mapping[str, Any]], proposed_edits: Mapping[str, Any]
) -> str:
    codes = {str(value["code"]) for value in findings}
    if proposed_edits:
        return "edit_repeated_content"
    if "exact_duplicate_primary_text" in codes:
        return "compare_then_keep_one_and_reject_redundant"
    if "near_duplicate_cluster" in codes:
        return "compare_cluster_then_keep_or_reject"
    if "low_quality_score" in codes:
        return "manual_keep_edit_or_reject"
    return "manual_keep_or_edit"


def _deduplicate_text_segments(value: str, policy: QualityAuditPolicy) -> str:
    parts = re.split(r"([.!?\u3002\uff01\uff1f;\uff1b\n]+)", value)
    result: list[str] = []
    seen: set[str] = set()
    for index in range(0, len(parts), 2):
        segment = parts[index]
        separator = parts[index + 1] if index + 1 < len(parts) else ""
        identity = _normalized_quality_text(segment)
        if len(identity) >= policy.minimum_repeated_segment_characters:
            if identity in seen:
                continue
            if not separator and any(previous.startswith(identity) for previous in seen):
                continue
            seen.add(identity)
        result.extend((segment, separator))
    return "".join(result).strip()


def _quality_review_markdown(
    run_id: str,
    reviewer: str,
    audit_fingerprint: str,
    items: Sequence[Mapping[str, Any]],
) -> str:
    lines = [
        "# Writing-material quality review packet",
        "",
        f"- Run: `{run_id}`",
        f"- Reviewer: `{reviewer}`",
        f"- Quality audit: `{audit_fingerprint}`",
        "- Evidence/source excerpts included: no",
        "- Decision import ready: no",
        "",
        "Fill an explicit decision and reason outside this packet; do not pass this draft "
        "directly to `review apply`.",
        "",
    ]
    for item in items:
        lines.extend(
            [
                f"## {item['asset_id']}",
                "",
                f"- Type: `{item['asset_type']}`",
                f"- Recommendation: `{item['recommended_action']}`",
                f"- Based-on hash: `{item['based_on_hash']}`",
                "- Findings: "
                + ", ".join(
                    f"`{value['code']}`"
                    + (f" (`{value['field']}`)" if value.get("field") else "")
                    for value in item["findings"]
                ),
                "",
                "Current derived material fields:",
                "",
                *[
                    f"    {line}"
                    for line in json.dumps(
                        item["current_material_fields"],
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    ).splitlines()
                ],
                "",
                "Proposed deterministic edits:",
                "",
                *[
                    f"    {line}"
                    for line in json.dumps(
                        item["proposed_edits"],
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    ).splitlines()
                ],
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _quality_finding(
    code: str,
    severity: str,
    asset_type: str,
    asset_ids: Sequence[str],
    *,
    observed: int | float,
    threshold: int | float,
    field: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "asset_type": asset_type,
        "asset_ids": list(asset_ids),
        "observed": observed,
        "threshold": threshold,
    }
    if field is not None:
        result["field"] = field
    return result


def _normalized_quality_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def _maximum_repeated_segment(value: str, policy: QualityAuditPolicy) -> int:
    segments = [
        _normalized_quality_text(item)
        for item in re.split(r"[.!?\u3002\uff01\uff1f;\uff1b\n]+", value)
        if len(_normalized_quality_text(item)) >= policy.minimum_repeated_segment_characters
    ]
    return max(Counter(segments).values(), default=0)


def _distribution(values: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(value.get(key) or "unknown") for value in values).items()))


def _quality_distribution(values: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    scores = [float(value["quality_score"]) for value in values if "quality_score" in value]
    if not scores:
        return {"count": 0, "minimum": None, "mean": None, "maximum": None}
    return {
        "count": len(scores),
        "minimum": min(scores),
        "mean": sum(scores) / len(scores),
        "maximum": max(scores),
    }


def _integer(value: Mapping[str, Any], key: str) -> int:
    raw = value.get(key)
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return raw


def _number(value: Mapping[str, Any], key: str) -> float:
    raw = value.get(key)
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or not 0 <= float(raw) <= 1:
        raise ValueError(f"{key} must be a number between zero and one")
    return float(raw)


def _rate(numerator: int | float, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name}:{line_number} must contain an object")
        values.append(value)
    return values
