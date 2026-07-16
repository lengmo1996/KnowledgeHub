"""Unified query routing across isolated KnowledgeHub collections."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from knowledgehub.hub.config import HubConfig
from knowledgehub.retrieval.models import SearchHit, SearchRequest, SearchResponse

_INTENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("debug", re.compile(r"traceback|exception|error|报错|异常|崩溃", re.I)),
    ("compatibility", re.compile(r"compatib|version|版本|弃用|deprecated|breaking", re.I)),
    ("migration", re.compile(r"migrat|upgrade|迁移|升级", re.I)),
    ("implementation", re.compile(r"implement|源码|source|内部实现|调用链", re.I)),
    ("api_usage", re.compile(r"how to|example|参数|用法|api|示例", re.I)),
)

_SOURCE_PRIORITIES: Mapping[str, tuple[str, ...]] = {
    "api_usage": ("api_documentation", "tutorial", "example", "source_code"),
    "implementation": ("api_documentation", "example", "source_code", "tutorial"),
    "debug": ("source_code", "release_note", "issue", "pull_request"),
    "compatibility": ("migration_guide", "release_note", "api_documentation", "source_code"),
    "migration": ("migration_guide", "release_note", "api_documentation", "example"),
    "source_understanding": ("source_code", "api_documentation", "example"),
}


def classify_code_intent(query: str, explicit: str | None = None) -> str:
    if explicit:
        if explicit not in _SOURCE_PRIORITIES:
            raise ValueError(f"unsupported code intent: {explicit}")
        return explicit
    for name, pattern in _INTENT_RULES:
        if pattern.search(query):
            return name
    return "source_understanding"


def build_code_query_plan(
    query: str,
    *,
    environment: str = "current",
    library: str | None = None,
    symbol: str | None = None,
    allow_auto_import: bool = False,
    allow_issues: bool = False,
) -> dict[str, Any]:
    intent = classify_code_intent(query)
    steps = ["find_symbol_current"] if symbol else ["search_current_documentation"]
    if intent in {"compatibility", "migration"}:
        steps.extend(["find_symbol_target", "find_release_changes", "find_related_source_diff"])
    if intent == "debug":
        steps.extend(["find_current_source", "find_release_bugfixes"])
    if allow_issues:
        steps.append("find_known_issues")
    return {
        "intent": intent,
        "entities": {"library": library, "symbol": symbol},
        "environment": environment,
        "retrieval_steps": steps,
        "allow_auto_import": allow_auto_import,
        "allow_issues": allow_issues,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class HubQueryRequest:
    knowledge_base: str
    query: str
    intent: str | None = None
    filters: Mapping[str, Any] = field(default_factory=dict)
    top_k: int = 10
    prefetch_limit: int = 50
    mode: str = "hybrid"
    return_mode: str = "pattern_first"
    reranker: str = "off"


class HubQueryService:
    def __init__(
        self,
        config: HubConfig,
        *,
        service_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.config = config
        self.service_factory = service_factory

    def search(self, value: HubQueryRequest) -> SearchResponse:
        if value.knowledge_base not in {"literature", "code", "writing"}:
            raise ValueError("knowledge_base must be literature, code, or writing")
        if not value.query.strip():
            raise ValueError("query cannot be empty")
        rag_config = self.config.rag_config(value.knowledge_base).with_overrides(
            reranker_profile=value.reranker
        )
        if self.service_factory is None:
            from knowledgehub.services.search_api import build_retrieval

            service = build_retrieval(rag_config)
        else:
            service = self.service_factory(rag_config)
        filters = dict(value.filters)
        intent = (
            classify_code_intent(value.query, value.intent)
            if value.knowledge_base == "code"
            else value.intent
        )
        source = filters.pop("source", "zotero" if value.knowledge_base == "literature" else None)
        allowed = {field.name for field in dataclasses.fields(SearchRequest)}
        unknown = sorted(set(filters) - allowed)
        if unknown:
            raise ValueError(f"unknown query filters: {', '.join(unknown)}")
        request = SearchRequest(
            query=value.query,
            mode=value.mode,
            limit=value.top_k,
            prefetch_limit=max(value.prefetch_limit, value.top_k),
            source=source,
            use_reranker=value.reranker != "off",
            reranker_profile=value.reranker,
            intent=intent,
            **filters,
        )
        try:
            response = service.search(request)
        finally:
            if hasattr(service, "endpoint_pool") and hasattr(service.endpoint_pool, "close"):
                service.endpoint_pool.close()
            reranker = getattr(service, "reranker", None)
            if reranker is not None:
                reranker.close()
        hits = list(response.hits)
        if value.knowledge_base == "code":
            priorities = _SOURCE_PRIORITIES[str(intent)]
            rank = {name: index for index, name in enumerate(priorities)}
            hits = [self._code_evidence(hit, filters, str(intent)) for hit in hits]
            evidence_rank = {
                "target_version_evidence": 0,
                "current_version_evidence": 1,
                "change_evidence": 2,
                "retrieved_evidence": 3,
            }
            hits.sort(
                key=lambda hit: (
                    evidence_rank.get(str(hit.payload.get("evidence_role")), 99),
                    rank.get(str(hit.payload.get("source_type")), 99),
                    -hit.score,
                )
            )
        elif value.knowledge_base == "writing" and value.return_mode == "pattern_first":
            hits = [self._pattern_first(hit) for hit in hits]
        return dataclasses.replace(response, hits=tuple(hits[: value.top_k]))

    @staticmethod
    def _code_evidence(
        hit: SearchHit, filters: Mapping[str, Any], intent: str
    ) -> SearchHit:
        payload = dict(hit.payload)
        version = str(payload.get("version") or "")
        if version and version == str(filters.get("target_version") or ""):
            role = "target_version_evidence"
        elif version and version == str(filters.get("installed_version") or ""):
            role = "current_version_evidence"
        elif payload.get("source_type") in {"release_note", "migration_guide"}:
            role = "change_evidence"
        else:
            role = "retrieved_evidence"
        payload["evidence_role"] = role
        payload["inference"] = False
        payload["intent"] = intent
        return dataclasses.replace(hit, payload=payload)

    @staticmethod
    def _pattern_first(hit: SearchHit) -> SearchHit:
        payload = dict(hit.payload)
        payload.pop("original_text", None)
        excerpt = payload.get("source_excerpt")
        if isinstance(excerpt, str):
            payload["source_excerpt"] = excerpt[:320]
        payload["return_mode"] = "pattern_first"
        return dataclasses.replace(hit, payload=payload)
