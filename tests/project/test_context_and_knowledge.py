from __future__ import annotations

from pathlib import Path

import pytest

from knowledgehub.project.context import ProjectContextBuilder
from knowledgehub.project.fixture import FixtureOrchestrator
from knowledgehub.project.knowledge import FixtureKnowledgeRouter, ProjectQueryService
from knowledgehub.project.models import ContextBudget
from knowledgehub.project.registry import ProjectRegistry
from knowledgehub.project.skills import ProjectSkillService


@pytest.fixture(scope="module")
def completed_registry(tmp_path_factory: pytest.TempPathFactory) -> ProjectRegistry:
    registry = ProjectRegistry(tmp_path_factory.mktemp("v3-state"))
    result = FixtureOrchestrator(Path.cwd(), registry).run_all()
    assert result["validation"]["valid"]
    return registry


def test_context_builder_selects_by_task(completed_registry: ProjectRegistry) -> None:
    builder = ProjectContextBuilder(completed_registry)
    debug = builder.build(
        "fixture-vision-project",
        "code_debugging",
        target_experiment_id="fixture-vision-exp-004",
    )
    assert set(debug["knowledge_scopes"]) == {"code"}
    assert debug["recent_experiments"][0]["status"] == "failed"
    assert debug["claims"] == []
    writing = builder.build("fixture-vision-project", "academic_writing")
    assert "path" not in writing["repositories"][0]
    assert writing["claims"]


def test_context_budget_truncates_records(completed_registry: ProjectRegistry) -> None:
    context = ProjectContextBuilder(completed_registry).build(
        "fixture-vision-project",
        "project_overview",
        budget=ContextBudget(max_records=2, max_characters=5000),
    )
    count = sum(
        len(context[key])
        for key in ("recent_experiments", "known_failures", "active_decisions", "claims")
    )
    assert count <= 2


def test_fixture_router_enforces_namespace_and_versions() -> None:
    router = FixtureKnowledgeRouter(Path("fixtures/v3/fixture_vision_project"))
    result = router.query(
        "code",
        "concatenation projection parameter count",
        namespace="fixture-code-v1",
    )
    assert result["sources"][0]["source_type"] == "source_code"
    assert result["versions"] == ["0.1.0"]
    with pytest.raises(PermissionError):
        router.query("code", "test", namespace="production-code")


def test_project_query_and_skills(completed_registry: ProjectRegistry) -> None:
    builder = ProjectContextBuilder(completed_registry)
    router = FixtureKnowledgeRouter(Path("fixtures/v3/fixture_vision_project"))
    query = ProjectQueryService(builder, router)
    result = query.query(
        "fixture-vision-project",
        "experiment_analysis",
        "Compare addition and concatenation fusion.",
        experiment_ids=("fixture-vision-exp-002", "fixture-vision-exp-003"),
    )
    assert set(result["knowledge_evidence"]) == {"code", "writing"}
    skill = ProjectSkillService(completed_registry, query)
    debug = skill.run(
        "code-debugging",
        "fixture-vision-project",
        experiment_ids=("fixture-vision-exp-004",),
    )
    assert debug["confidence"] == "high"
    analysis = skill.run(
        "research-result-analysis",
        "fixture-vision-project",
        experiment_ids=("fixture-vision-exp-002", "fixture-vision-exp-003"),
    )
    assert analysis["comparable_environment_and_commit"] is True
    writing = skill.run("writing-academic", "fixture-vision-project")
    assert "fixture_results_must_not_be_presented_as_real_research" in writing["warnings"]
