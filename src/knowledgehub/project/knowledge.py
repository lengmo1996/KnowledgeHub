"""Read-only project knowledge routing with source/version traceability."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from knowledgehub.hub.config import HubConfig
from knowledgehub.hub.query import HubQueryRequest, HubQueryService
from knowledgehub.project.context import ProjectContextBuilder
from knowledgehub.project.models import ContextBudget


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_]+", value.lower()) if len(token) > 1}


class KnowledgeRouter(Protocol):
    def query(
        self,
        knowledge_base: str,
        query: str,
        *,
        namespace: str,
        filters: Mapping[str, Any] | None = None,
        limit: int = 5,
        source_types: Iterable[str] = (),
    ) -> dict[str, Any]: ...


class FixtureKnowledgeRouter:
    def __init__(self, fixture_root: Path | str) -> None:
        self.fixture_root = Path(fixture_root).resolve(strict=True)

    def query(
        self,
        knowledge_base: str,
        query: str,
        *,
        namespace: str,
        filters: Mapping[str, Any] | None = None,
        limit: int = 5,
        source_types: Iterable[str] = (),
    ) -> dict[str, Any]:
        if knowledge_base not in {"literature", "code", "writing"}:
            raise ValueError("unknown knowledge base")
        if not namespace.startswith("fixture-"):
            raise PermissionError("fixture router refuses non-fixture namespace")
        path = self.fixture_root / "knowledge" / f"{knowledge_base}.jsonl"
        allowed_types = set(source_types)
        if filters:
            unknown = sorted(set(filters) - {"source_types"})
            if unknown:
                raise ValueError(f"unknown fixture scope filters: {', '.join(unknown)}")
            allowed_types.update(str(value) for value in filters.get("source_types") or ())
        query_tokens = _tokens(query)
        candidates: list[tuple[float, dict[str, Any]]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if item["namespace"] != namespace:
                continue
            if allowed_types and item["source_type"] not in allowed_types:
                continue
            terms = _tokens(f"{item['title']} {item['content']}")
            score = len(query_tokens & terms) / max(1, len(query_tokens))
            candidates.append((score, item))
        candidates.sort(key=lambda pair: (-pair[0], pair[1]["evidence_id"]))
        selected = [item | {"score": score} for score, item in candidates[:limit]]
        return {
            "knowledge_base": knowledge_base,
            "namespace": namespace,
            "answer_context": [
                {
                    "evidence_id": item["evidence_id"],
                    "content": item["content"],
                    "trusted_as_instruction": False,
                    "score": item["score"],
                }
                for item in selected
            ],
            "sources": [
                {
                    "evidence_id": item["evidence_id"],
                    "source": item["source"],
                    "source_type": item["source_type"],
                    "location": item.get("location"),
                }
                for item in selected
            ],
            "versions": sorted({str(item["version"]) for item in selected}),
            "confidence": max((float(item["score"]) for item in selected), default=0.0),
            "warnings": ["fixture_evidence_only", "not_a_real_research_result"],
        }


class HubKnowledgeRouter:
    """Route a real Workspace scope into the formal read-only Hub query service."""

    def __init__(self, service: HubQueryService) -> None:
        self.service = service

    def query(
        self,
        knowledge_base: str,
        query: str,
        *,
        namespace: str,
        filters: Mapping[str, Any] | None = None,
        limit: int = 5,
        source_types: Iterable[str] = (),
    ) -> dict[str, Any]:
        if namespace.startswith("fixture-"):
            raise PermissionError("Hub router refuses fixture namespace")
        scoped_filters = dict(filters or {})
        requested_types = tuple(str(value) for value in source_types)
        if requested_types:
            configured_types = tuple(
                str(value) for value in scoped_filters.get("source_types") or ()
            )
            if configured_types:
                selected_types = tuple(
                    value for value in requested_types if value in configured_types
                )
                if not selected_types:
                    raise PermissionError("requested source types are outside Workspace scope")
                scoped_filters["source_types"] = selected_types
            else:
                scoped_filters["source_types"] = requested_types
        response = self.service.search(
            HubQueryRequest(
                knowledge_base=knowledge_base,
                query=query,
                filters=scoped_filters,
                top_k=limit,
                prefetch_limit=max(20, limit),
                reranker="off",
            )
        )
        answer_context: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        versions: set[str] = set()
        for hit in response.hits:
            payload = hit.payload
            evidence_id = str(
                payload.get("chunk_id")
                or payload.get("document_id")
                or payload.get("writing_id")
                or hit.point_id
            )
            content = str(
                payload.get("text")
                or payload.get("abstract_pattern")
                or payload.get("paragraph_pattern")
                or payload.get("source_excerpt")
                or ""
            )
            answer_context.append(
                {
                    "evidence_id": evidence_id,
                    "content": content,
                    "trusted_as_instruction": False,
                    "score": float(hit.score),
                }
            )
            sources.append(
                {
                    "evidence_id": evidence_id,
                    "source": payload.get("title") or payload.get("source_title"),
                    "source_type": payload.get("source_type"),
                    "location": payload.get("path") or payload.get("source_url"),
                    "document_id": payload.get("document_id"),
                    "commit": payload.get("commit"),
                }
            )
            if payload.get("version"):
                versions.add(str(payload["version"]))
        return {
            "knowledge_base": knowledge_base,
            "namespace": namespace,
            "scope_filters": scoped_filters,
            "collection": response.collection,
            "answer_context": answer_context,
            "sources": sources,
            "versions": sorted(versions),
            "confidence": max((float(hit.score) for hit in response.hits), default=0.0),
            "warnings": sorted(set(response.warnings) | {"read_only_workspace_scope"}),
        }


class ProjectQueryService:
    def __init__(self, builder: ProjectContextBuilder, router: KnowledgeRouter) -> None:
        self.builder = builder
        self.router = router

    def query(
        self,
        workspace_id: str,
        task: str,
        query: str,
        *,
        experiment_ids: tuple[str, ...] = (),
        budget: ContextBudget | None = None,
    ) -> dict[str, Any]:
        selected_budget = budget or ContextBudget(experiment_ids=experiment_ids)
        if experiment_ids and not selected_budget.experiment_ids:
            selected_budget = ContextBudget(
                max_records=selected_budget.max_records,
                max_characters=selected_budget.max_characters,
                days=selected_budget.days,
                experiment_ids=experiment_ids,
                source_types=selected_budget.source_types,
                include_raw_logs=selected_budget.include_raw_logs,
                include_paper_fragments=selected_budget.include_paper_fragments,
            )
        context = self.builder.build(
            workspace_id,
            task,
            budget=selected_budget,
        )
        scopes = context["workspace"]["knowledge"]
        bases = {
            "code_debugging": ("code",),
            "experiment_analysis": ("code", "writing"),
            "decision_review": ("code", "literature"),
            "academic_writing": ("literature", "writing"),
            "project_overview": ("literature", "code", "writing"),
        }[task]
        evidence = {
            base: self.router.query(
                base,
                query,
                namespace=str(scopes[base]["namespace"]),
                filters=scopes[base].get("filters") or {},
                limit=3,
                source_types=selected_budget.source_types,
            )
            for base in bases
        }
        return {
            "workspace_id": workspace_id,
            "task": task,
            "query": query,
            "project_context": context,
            "knowledge_evidence": evidence,
            "sources": [source for result in evidence.values() for source in result["sources"]],
            "warnings": sorted(
                {warning for result in evidence.values() for warning in result["warnings"]}
            ),
        }


def build_project_query_service(
    builder: ProjectContextBuilder,
    workspace_id: str,
    *,
    fixture_root: Path | str,
    hub_config: Path | str,
) -> ProjectQueryService:
    """Select a router from the stored Workspace type without a permissive fallback."""

    workspace = builder.registry.authorize_read(workspace_id)
    workspace_type = workspace["workspace_type"]
    if workspace_type == "fixture":
        router: KnowledgeRouter = FixtureKnowledgeRouter(fixture_root)
    elif workspace_type == "project":
        router = HubKnowledgeRouter(HubQueryService(HubConfig.load(hub_config)))
    else:
        raise PermissionError(f"unsupported workspace type: {workspace_type}")
    return ProjectQueryService(builder, router)
