"""Unified query routing across isolated KnowledgeHub collections."""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from knowledgehub.hub.config import HubConfig
from knowledgehub.retrieval.models import SearchHit, SearchRequest, SearchResponse
from knowledgehub.writing_rag.sections import section_family

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
    "debug": ("source_code", "version_diff", "release_note", "issue", "pull_request"),
    "compatibility": (
        "version_diff",
        "migration_guide",
        "release_note",
        "api_documentation",
        "source_code",
    ),
    "migration": (
        "migration_guide",
        "version_diff",
        "release_note",
        "api_documentation",
        "example",
    ),
    "source_understanding": ("source_code", "api_documentation", "example"),
}


def _section_family(value: str) -> str:
    return section_family(value)


_BIBLIOGRAPHY_QUERY = re.compile(
    r"\b(?:bibliograph(?:y|ies)|citations?|references?|works cited)\b|参考文献|引用列表",
    re.I,
)
_BIBLIOGRAPHY_HEADINGS = {
    "bibliography",
    "literature cited",
    "reference",
    "references",
    "works cited",
    "参考文献",
    "参考资料",
}


def _is_bibliography(payload: Mapping[str, Any]) -> bool:
    values: list[str] = []
    for key in ("section", "section_path"):
        raw = payload.get(key)
        if isinstance(raw, (list, tuple)):
            values.extend(str(item) for item in raw)
        elif raw:
            values.append(str(raw))
    for value in values:
        leaf = re.split(r"\s*(?:>|/|::)\s*", value)[-1]
        normalized = re.sub(r"[^a-z\u3400-\u4dbf\u4e00-\u9fff]+", " ", leaf.lower()).strip()
        if normalized in _BIBLIOGRAPHY_HEADINGS:
            return True
    return False


def _normalized_domain(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _infer_writing_domains(payload: Mapping[str, Any]) -> set[str]:
    text = " ".join(
        str(payload.get(key) or "") for key in ("source_title", "title", "text", "source_excerpt")
    ).lower()
    inferred: set[str] = set()
    if re.search(r"\b(?:image|visual|vision|object detection|diffusion model)\b", text):
        inferred.add("computer_vision")
    if re.search(r"\b(?:graph|gnn|node|edge)\b", text):
        inferred.add("graph_learning")
    if re.search(r"\b(?:language model|llm|lmm|multimodal)\b", text):
        inferred.add("language_or_multimodal_models")
    if re.search(r"\b(?:ordinary differential|neural ode|time-series)\b", text):
        inferred.add("differential_equations")
    return inferred


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
    intent: str | None = None,
    environment: str = "current",
    library: str | None = None,
    symbol: str | None = None,
    allow_auto_import: bool = False,
    allow_issues: bool = False,
) -> dict[str, Any]:
    intent = classify_code_intent(query, intent)
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
        if value.knowledge_base == "writing" and value.return_mode not in {
            "pattern_first",
            "paragraph_structure",
            "include_original",
        }:
            raise ValueError("unsupported Writing return mode")
        rag_config = self.config.rag_config(value.knowledge_base).with_overrides(
            reranker_profile=value.reranker
        )
        if self.service_factory is None:
            from knowledgehub.services.search_api import build_retrieval

            service = build_retrieval(rag_config)
        else:
            service = self.service_factory(rag_config)
        filters = dict(value.filters)
        writing_post_filters: dict[str, Any] = {}
        if value.knowledge_base == "writing":
            for key in ("section", "venue", "research_domain"):
                if filters.get(key) is not None:
                    writing_post_filters[key] = filters.pop(key)
            if filters.get("writing_asset_type") is None:
                from knowledgehub.writing_rag.materials import infer_writing_asset_type

                inferred_asset_type = infer_writing_asset_type(value.query)
                if inferred_asset_type is not None:
                    filters["writing_asset_type"] = inferred_asset_type
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
            knowledge_base=value.knowledge_base,
            mode=value.mode,
            limit=(
                max(value.prefetch_limit, value.top_k)
                if writing_post_filters
                or (
                    value.knowledge_base == "literature"
                    and not _BIBLIOGRAPHY_QUERY.search(value.query)
                )
                else value.top_k
            ),
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
        if value.knowledge_base == "literature" and not _BIBLIOGRAPHY_QUERY.search(value.query):
            demoted = [hit for hit in hits if _is_bibliography(hit.payload)]
            if demoted:
                hits = [hit for hit in hits if not _is_bibliography(hit.payload)] + demoted
                response = dataclasses.replace(
                    response,
                    warnings=(*response.warnings, "bibliography_sections_demoted"),
                )
        elif value.knowledge_base == "code":
            exact_symbol = self._exact_symbol_hit(filters)
            if exact_symbol is not None:
                hits.insert(0, exact_symbol)
            priorities = _SOURCE_PRIORITIES[str(intent)]
            rank = {name: index for index, name in enumerate(priorities)}
            hits = [self._code_evidence(hit, filters, str(intent)) for hit in hits]
            evidence_rank = {
                "exact_symbol_source": 0,
                "target_version_evidence": 1,
                "current_version_evidence": 2,
                "change_evidence": 3,
                "retrieved_evidence": 4,
            }
            hits.sort(
                key=lambda hit: (
                    evidence_rank.get(str(hit.payload.get("evidence_role")), 99),
                    rank.get(str(hit.payload.get("source_type")), 99),
                    -hit.score,
                )
            )
        elif value.knowledge_base == "writing":
            hits = [self._writing_metadata(hit) for hit in hits]
            if writing_post_filters:
                hits = [
                    hit
                    for hit in hits
                    if self._writing_filter_match(hit.payload, writing_post_filters)
                ]
            hits = self._apply_writing_feedback(hits)
            if value.return_mode == "pattern_first":
                hits = [self._pattern_first(hit) for hit in hits]
            elif value.return_mode == "paragraph_structure":
                hits = [self._paragraph_structure(hit) for hit in hits]
        return dataclasses.replace(response, hits=tuple(hits[: value.top_k]))

    def _exact_symbol_hit(self, filters: Mapping[str, Any]) -> SearchHit | None:
        library = str(filters.get("library") or "")
        version = str(
            filters.get("version")
            or filters.get("target_version")
            or filters.get("installed_version")
            or ""
        )
        symbol = str(filters.get("symbol") or "")
        if not library or not version or not symbol:
            return None
        from knowledgehub.code_rag.symbols import SymbolIndex

        data_root = self.config.code.data_root
        catalog_path = data_root / "state" / "symbols.sqlite3"
        if not catalog_path.is_file():
            return None
        exact = SymbolIndex(catalog_path, read_only=True).inspect(library, version, symbol)
        if exact is None:
            return None
        marker_path = data_root / "sources" / "repositories" / library / version / "current.json"
        marker = (
            json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.is_file() else {}
        )
        repository = str(marker.get("repository") or "")
        commit = str(marker.get("commit") or "")
        path = str(exact.get("path") or "")
        source_url = (
            f"https://github.com/{repository}/blob/{commit}/{path}#L{exact.get('start_line')}"
            if repository and commit and path and exact.get("start_line")
            else ""
        )
        qualified = str(exact.get("qualified_name") or symbol)
        signature = str(exact.get("signature") or qualified)
        payload = dict(exact) | {
            "chunk_id": f"symbol:{exact['symbol_id']}",
            "document_id": str(exact["symbol_id"]),
            "knowledge_base": "code",
            "library": library,
            "version": version,
            "symbol": symbol,
            "qualified_symbol": qualified,
            "source_type": "source_code",
            "source_url": source_url,
            "repository": repository,
            "commit": commit,
            "title": f"{library} {version}: {qualified}",
            "text": f"Signature: {signature}\n\nDefined at {path}:{exact.get('start_line')}",
            "section": qualified,
            "evidence_role": "exact_symbol_source",
            "inference": False,
        }
        return SearchHit(
            point_id=f"symbol:{exact['symbol_id']}",
            score=1.0,
            payload=payload,
        )

    @staticmethod
    def _writing_metadata(hit: SearchHit) -> SearchHit:
        payload = dict(hit.payload)
        if not payload.get("writing_id") and payload.get("document_id"):
            # Frozen rules-v1 points use document_id as the canonical Writing
            # identity. Normalize only the response; never rewrite the index.
            payload["writing_id"] = payload["document_id"]
        inferred = _infer_writing_domains(payload)
        if inferred:
            payload["inferred_research_domain"] = sorted(inferred)
            payload["research_domain_inference"] = True
        return dataclasses.replace(hit, payload=payload)

    @staticmethod
    def _writing_filter_match(payload: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
        section = filters.get("section")
        if section is not None:
            expected = _section_family(str(section))
            actual = _section_family(str(payload.get("section") or ""))
            if expected != actual:
                return False
        venue = filters.get("venue")
        if venue is not None:
            actual_venue = str(payload.get("venue") or "").lower()
            domains = {str(value).lower() for value in payload.get("research_domain") or []}
            expected_venue = str(venue).lower()
            aliases = {expected_venue}
            if expected_venue == "neurips":
                aliases.add("nips")
            if actual_venue not in aliases and not aliases.intersection(domains):
                return False
        domain = filters.get("research_domain")
        if domain is not None:
            expected_domain = _normalized_domain(str(domain))
            declared = {
                _normalized_domain(str(value)) for value in payload.get("research_domain") or []
            }
            inferred = _infer_writing_domains(payload)
            if expected_domain not in declared | inferred:
                return False
        return True

    def _apply_writing_feedback(self, hits: list[SearchHit]) -> list[SearchHit]:
        writing = getattr(self.config, "writing", None)
        if writing is None:
            return hits
        path = writing.data_root / "state" / "feedback.sqlite3"
        if not path.is_file():
            return hits
        from knowledgehub.writing_rag.v2 import WritingFeedbackStore

        identifiers = [
            str(hit.payload.get("writing_id") or hit.payload.get("document_id") or "")
            for hit in hits
        ]
        adjustments = WritingFeedbackStore(path, read_only=True).adjustments(identifiers)
        ranked: list[SearchHit] = []
        for hit, writing_id in zip(hits, identifiers, strict=True):
            adjustment = adjustments.get(writing_id, 0.0)
            payload = dict(hit.payload)
            payload["feedback_adjustment"] = adjustment
            quality = payload.get("quality_score")
            if isinstance(quality, (int, float)):
                payload["adjusted_quality_score"] = max(0.0, min(1.0, float(quality) + adjustment))
            ranked.append(
                dataclasses.replace(
                    hit,
                    score=hit.score + adjustment,
                    payload=payload,
                )
            )
        ranked.sort(key=lambda hit: -hit.score)
        return ranked

    @staticmethod
    def _code_evidence(hit: SearchHit, filters: Mapping[str, Any], intent: str) -> SearchHit:
        payload = dict(hit.payload)
        version = str(payload.get("version") or "")
        if payload.get("evidence_role") == "exact_symbol_source":
            role = "exact_symbol_source"
        elif version and version == str(filters.get("target_version") or ""):
            role = "target_version_evidence"
        elif version and version == str(filters.get("installed_version") or ""):
            role = "current_version_evidence"
        elif payload.get("source_type") in {
            "release_note",
            "migration_guide",
            "version_diff",
        }:
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

    @staticmethod
    def _paragraph_structure(hit: SearchHit) -> SearchHit:
        payload = dict(hit.payload)
        payload.pop("original_text", None)
        payload.pop("source_excerpt", None)
        payload["return_mode"] = "paragraph_structure"
        return dataclasses.replace(hit, payload=payload)
