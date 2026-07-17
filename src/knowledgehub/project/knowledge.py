"""Offline fixture-only knowledge routing with source/version traceability."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from knowledgehub.project.context import ProjectContextBuilder
from knowledgehub.project.models import ContextBudget


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_]+", value.lower()) if len(token) > 1}


class FixtureKnowledgeRouter:
    def __init__(self, fixture_root: Path | str) -> None:
        self.fixture_root = Path(fixture_root).resolve(strict=True)

    def query(
        self,
        knowledge_base: str,
        query: str,
        *,
        namespace: str,
        limit: int = 5,
        source_types: Iterable[str] = (),
    ) -> dict[str, Any]:
        if knowledge_base not in {"literature", "code", "writing"}:
            raise ValueError("unknown knowledge base")
        if not namespace.startswith("fixture-"):
            raise PermissionError("fixture router refuses non-fixture namespace")
        path = self.fixture_root / "knowledge" / f"{knowledge_base}.jsonl"
        allowed_types = set(source_types)
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


class ProjectQueryService:
    def __init__(self, builder: ProjectContextBuilder, router: FixtureKnowledgeRouter) -> None:
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
                limit=3,
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
