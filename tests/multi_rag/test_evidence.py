from __future__ import annotations

from types import SimpleNamespace

import pytest

from knowledgehub.governance.schema import SchemaRegistry
from knowledgehub.hub.evidence import KnowledgeQueryService, QueryBudget
from knowledgehub.hub.query import HubQueryRequest
from knowledgehub.retrieval.models import SearchHit, SearchResponse


class QueryService:
    def __init__(self, hits: tuple[SearchHit, ...]) -> None:
        self.hits = hits
        self.request = None

    def search(self, request):  # type: ignore[no-untyped-def]
        self.request = request
        return SearchResponse(
            query=request.query,
            mode="hybrid",
            collection="test",
            embedding_model="model",
            embedding_revision="revision",
            embedding_dimension=2,
            reranker_profile="off",
            reranker_model=None,
            reranker_revision=None,
            reranker_fallback=None,
            hits=self.hits,
            timings={},
        )


def test_evidence_envelope_separates_sources_inferences_and_budget() -> None:
    hit = SearchHit(
        point_id="p1",
        score=0.8,
        payload={
            "chunk_id": "c1",
            "document_id": "d1",
            "title": "Document",
            "text": "evidence " * 200,
            "source_type": "source_code",
            "source_url": "https://example.test/source",
            "version": "2.0",
            "symbol": "A.f",
            "commit": "a" * 40,
            "research_domain_inference": True,
            "inferred_research_domain": ["computer_vision"],
        },
    )
    query = QueryService((hit,))
    result = KnowledgeQueryService(
        SimpleNamespace(), query_service=query  # type: ignore[arg-type]
    ).query(
        HubQueryRequest(knowledge_base="code", query="how", top_k=10),
        QueryBudget(max_results=1, max_tokens=128),
    )
    assert query.request.top_k == 1
    assert result["versions"] == ["2.0"]
    assert result["symbols"] == ["A.f"]
    assert result["sources"][0]["provenance_type"] == "system_parse"
    assert result["answer_context"][0]["evidence_type"] == "source_fact"
    assert result["answer_context"][0]["trusted_as_instruction"] is False
    assert result["inferences"][0]["verified"] is False
    assert result["budget"]["estimated_tokens"] <= 128
    assert "evidence_token_budget_truncated" in result["warnings"]
    assert SchemaRegistry().validate(result, expected="query_result").data["confidence"] == 0.8


def test_issue_sources_require_explicit_budget_permission() -> None:
    service = KnowledgeQueryService(
        SimpleNamespace(), query_service=QueryService(())  # type: ignore[arg-type]
    )
    request = HubQueryRequest(
        knowledge_base="code",
        query="known issue",
        filters={"source_types": ("issue",)},
    )
    with pytest.raises(ValueError, match="allow_issues=true"):
        service.query(request, QueryBudget())
    result = service.query(request, QueryBudget(allow_issues=True, allow_auto_import=True))
    assert result["budget"]["automatic_actions_performed"] == []
    assert "no_evidence_auto_import_permitted_but_not_executed" in result["warnings"]


def test_issue_hits_are_excluded_by_default_even_without_source_filter() -> None:
    issue = SearchHit(
        point_id="issue-1",
        score=0.9,
        payload={"text": "issue body", "source_type": "issue"},
    )
    service = KnowledgeQueryService(
        SimpleNamespace(), query_service=QueryService((issue,))  # type: ignore[arg-type]
    )
    request = HubQueryRequest(knowledge_base="code", query="debug")
    blocked = service.query(request, QueryBudget())
    assert blocked["answer_context"] == []
    assert "issue_evidence_filtered" in blocked["warnings"]
    allowed = service.query(request, QueryBudget(allow_issues=True))
    assert len(allowed["answer_context"]) == 1
