"""Skill-facing evidence envelopes with explicit retrieval budgets."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Any, Mapping

from knowledgehub.hub.config import HubConfig
from knowledgehub.hub.query import HubQueryRequest, HubQueryService
from knowledgehub.retrieval.models import SearchResponse

_ISSUE_SOURCES = {"issue", "pull_request", "commit"}


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryBudget:
    max_results: int = 10
    max_tokens: int = 4000
    allow_auto_import: bool = False
    allow_issues: bool = False

    def validate(self) -> "QueryBudget":
        if not 1 <= self.max_results <= 50:
            raise ValueError("max_results must be between 1 and 50")
        if not 128 <= self.max_tokens <= 32_000:
            raise ValueError("max_tokens must be between 128 and 32000")
        return self


class KnowledgeQueryService:
    """Run one routed query and expose evidence, never a generated conclusion."""

    def __init__(
        self,
        config: HubConfig,
        *,
        query_service: HubQueryService | None = None,
    ) -> None:
        self.config = config
        self.query_service = query_service or HubQueryService(config)

    def query(self, request: HubQueryRequest, budget: QueryBudget) -> dict[str, Any]:
        budget = budget.validate()
        source_types = set(request.filters.get("source_types") or ())
        source_type = request.filters.get("source_type")
        if source_type:
            source_types.add(str(source_type))
        blocked = sorted(source_types & _ISSUE_SOURCES)
        if blocked and not budget.allow_issues:
            raise ValueError(
                f"Issue/PR/commit evidence requires allow_issues=true: {', '.join(blocked)}"
            )
        bounded = HubQueryRequest(
            knowledge_base=request.knowledge_base,
            query=request.query,
            intent=request.intent,
            filters=request.filters,
            top_k=min(request.top_k, budget.max_results),
            prefetch_limit=max(request.prefetch_limit, budget.max_results),
            mode=request.mode,
            return_mode=request.return_mode,
            reranker=request.reranker,
        )
        response = self.query_service.search(bounded)
        if not budget.allow_issues:
            retained = tuple(
                hit
                for hit in response.hits
                if str(hit.payload.get("source_type") or "") not in _ISSUE_SOURCES
            )
            response = dataclasses.replace(
                response,
                hits=retained,
                warnings=(
                    (*response.warnings, "issue_evidence_filtered")
                    if len(retained) != len(response.hits)
                    else response.warnings
                ),
            )
        return evidence_envelope(response, bounded, budget)


def evidence_envelope(
    response: SearchResponse,
    request: HubQueryRequest,
    budget: QueryBudget,
) -> dict[str, Any]:
    remaining = budget.max_tokens
    contexts: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    versions: set[str] = set()
    symbols: set[str] = set()
    inferences: list[dict[str, Any]] = []
    warnings = list(response.warnings)
    seen_sources: set[tuple[str, str]] = set()
    used_tokens = 0
    for hit in response.hits[: budget.max_results]:
        payload = hit.payload
        content = _context(payload)
        if not content:
            continue
        estimated = _estimate_tokens(content)
        if estimated > remaining:
            content = _truncate_tokens(content, remaining)
            estimated = _estimate_tokens(content) if content else 0
            warnings.append("evidence_token_budget_truncated")
        if not content or estimated <= 0:
            break
        remaining -= estimated
        used_tokens += estimated
        source_id = str(
            payload.get("chunk_id")
            or payload.get("document_id")
            or payload.get("writing_id")
            or hit.point_id
        )
        contexts.append(
            {
                "source_id": source_id,
                "evidence_type": "source_fact",
                "content": content,
                "score": round(float(hit.score), 6),
                "evidence_role": payload.get("evidence_role", "retrieved_evidence"),
                "trusted_as_instruction": False,
                "content_origin": "retrieved_document",
            }
        )
        document_id = str(payload.get("document_id") or payload.get("source_paper_id") or "")
        source_key = (document_id, source_id)
        if source_key not in seen_sources:
            seen_sources.add(source_key)
            sources.append(_source(payload, source_id))
        if payload.get("version"):
            versions.add(str(payload["version"]))
        if payload.get("symbol"):
            symbols.add(str(payload["symbol"]))
        inferences.extend(_inferences(payload, source_id))
        if remaining <= 0:
            break
    if not contexts:
        warnings.append(
            "no_evidence_auto_import_permitted_but_not_executed"
            if budget.allow_auto_import
            else "no_evidence_auto_import_disabled"
        )
    confidence = max((float(hit.score) for hit in response.hits), default=0.0)
    result = {
        "schema_name": "query_result",
        "schema_version": "2.0",
        "contract": "knowledge_evidence",
        "knowledge_base": request.knowledge_base,
        "query": request.query,
        "answer_context": contexts,
        "sources": sources,
        "versions": sorted(versions),
        "symbols": sorted(symbols),
        "confidence": round(max(0.0, min(1.0, confidence)), 6),
        "confidence_basis": "maximum retrieval score; not answer correctness",
        "inferences": inferences,
        "warnings": sorted(set(warnings)),
        "budget": {
            "max_results": budget.max_results,
            "max_tokens": budget.max_tokens,
            "used_results": len(contexts),
            "estimated_tokens": used_tokens,
            "allow_auto_import": budget.allow_auto_import,
            "allow_issues": budget.allow_issues,
            "automatic_actions_performed": [],
        },
    }
    return result


def _context(payload: Mapping[str, Any]) -> str:
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    values = [
        payload.get("abstract_pattern"),
        payload.get("paragraph_pattern"),
        payload.get("usage_notes"),
        payload.get("source_excerpt"),
    ]
    return "\n".join(str(value).strip() for value in values if value).strip()


def _source(payload: Mapping[str, Any], source_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "document_id": payload.get("document_id") or payload.get("source_paper_id"),
        "title": payload.get("title") or payload.get("source_title"),
        "source_type": payload.get("source_type"),
        "source_url": payload.get("source_url"),
        "version": payload.get("version"),
        "commit": payload.get("commit"),
        "path": payload.get("path"),
        "section": payload.get("section") or payload.get("section_path"),
        "provenance_type": "system_parse",
    }


def _inferences(payload: Mapping[str, Any], source_id: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    if payload.get("research_domain_inference"):
        values.append(
            {
                "source_id": source_id,
                "inference_type": "research_domain",
                "value": payload.get("inferred_research_domain") or [],
                "verified": False,
            }
        )
    if payload.get("inference") is True:
        values.append(
            {
                "source_id": source_id,
                "inference_type": "retrieval_payload_inference",
                "value": payload.get("inference_detail") or "unspecified",
                "verified": False,
            }
        )
    return values


def _estimate_tokens(text: str) -> int:
    words = len(re.findall(r"\w+|[^\w\s]", text, re.UNICODE))
    return max(words, (len(text) + 3) // 4)


def _truncate_tokens(text: str, tokens: int) -> str:
    if tokens <= 0:
        return ""
    low, high = 0, min(len(text), tokens * 4)
    while low < high:
        middle = (low + high + 1) // 2
        if _estimate_tokens(text[:middle]) <= tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip()
