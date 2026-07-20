from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import anyio

from knowledgehub.mcp.config import MCPConfig
from knowledgehub.mcp.tools import ToolRegistry
from knowledgehub.project.models import Workspace
from knowledgehub.project.registry import ProjectRegistry
from knowledgehub.retrieval.models import SearchHit, SearchResponse


class Service:
    def __init__(self) -> None:
        self.config = SimpleNamespace(reranker_profile="off")


def _workspace() -> Workspace:
    return Workspace(
        workspace_id="fixture-mcp-project",
        name="Fixture MCP Project",
        description="MCP project tool test",
        research={"questions": ["test"], "hypotheses": ["test"]},
        repositories=(
            {
                "repository_id": "fixture-main",
                "role": "primary",
                "path": "fixtures/v3/fixture_vision_project",
            },
        ),
        environments={"development": "fixture-cpu"},
        knowledge={
            "literature": {"namespace": "fixture-literature-v1"},
            "code": {"namespace": "fixture-code-v1"},
            "writing": {"namespace": "fixture-writing-v1"},
        },
        created_at="2026-07-17T00:00:00+00:00",
        updated_at="2026-07-17T00:00:00+00:00",
    )


def _project_workspace() -> Workspace:
    return Workspace(
        workspace_id="my-private-project",
        name="Private MCP Project",
        description="read-only",
        research={"question": "test", "hypotheses": []},
        repositories=({"repository_id": "primary-repository", "path": "."},),
        environments={},
        knowledge={
            "literature": {"namespace": "production-literature", "filters": {}},
            "code": {"namespace": "production-code", "filters": {}},
            "writing": {"namespace": "production-writing", "filters": {}},
        },
        created_at="2026-07-20T00:00:00+00:00",
        updated_at="2026-07-20T00:00:00+00:00",
        workspace_type="project",
        data_scope="private",
    )


class FakeHubQueryService:
    def __init__(self, config) -> None:  # type: ignore[no-untyped-def]
        self.config = config

    def search(self, value) -> SearchResponse:  # type: ignore[no-untyped-def]
        return SearchResponse(
            query=value.query,
            mode="hybrid",
            collection=f"formal-{value.knowledge_base}",
            embedding_model="test",
            embedding_revision="test",
            embedding_dimension=1,
            reranker_profile="off",
            reranker_model=None,
            reranker_revision=None,
            reranker_fallback=None,
            hits=(
                SearchHit(
                    point_id="point-1",
                    score=0.75,
                    payload={"chunk_id": "chunk-1", "text": "read-only evidence"},
                ),
            ),
            timings={},
        )


def test_project_query_and_skill_are_read_only_mcp_tools(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    state_root = tmp_path / "state"
    ProjectRegistry(state_root).create(_workspace())
    fixture_root = Path("fixtures/v3/fixture_vision_project").resolve(strict=True)
    monkeypatch.setenv("KH_PROJECT_STATE_ROOT", str(state_root))
    monkeypatch.setenv("KH_PROJECT_FIXTURE_ROOT", str(fixture_root))
    tools = ToolRegistry(Service(), MCPConfig(max_response_bytes=500_000))

    async def exercise() -> None:
        query = await tools.call(
            "knowledge_project_query",
            {
                "workspace_id": "fixture-mcp-project",
                "task": "project_overview",
                "query": "feature fusion limitation",
                "max_characters": 8000,
            },
        )
        assert not query.isError
        result = query.structuredContent["result"]
        assert set(result["knowledge_evidence"]) == {"literature", "code", "writing"}
        assert "fixture_evidence_only" in result["warnings"]
        skill = await tools.call(
            "knowledge_project_skill",
            {
                "workspace_id": "fixture-mcp-project",
                "skill": "writing-academic",
            },
        )
        assert not skill.isError
        assert skill.structuredContent["result"]["skill"] == "writing-academic"

    anyio.run(exercise)
    assert list(state_root.glob("fixture-mcp-project/**/*.json")) == [
        state_root / "fixture-mcp-project" / "workspace.json"
    ]


def test_project_tool_rejects_unknown_fields(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    state_root = tmp_path / "state"
    ProjectRegistry(state_root).create(_workspace())
    monkeypatch.setenv("KH_PROJECT_STATE_ROOT", str(state_root))
    monkeypatch.setenv(
        "KH_PROJECT_FIXTURE_ROOT",
        str(Path("fixtures/v3/fixture_vision_project").resolve(strict=True)),
    )
    tools = ToolRegistry(Service(), MCPConfig())
    result = anyio.run(
        tools.call,
        "knowledge_project_query",
        {
            "workspace_id": "fixture-mcp-project",
            "task": "project_overview",
            "query": "test",
            "state_root": "/etc",
        },
    )
    assert result.isError
    assert result.structuredContent["error"]["code"] == "invalid_arguments"


def test_real_project_mcp_is_read_only_and_fail_closed(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    state_root = tmp_path / "project-state"
    state_root.mkdir(mode=0o700)
    ProjectRegistry(state_root).create(
        _project_workspace(),
        allow_real_project=True,
        repository_root=repository,
    )
    before = {
        path.relative_to(state_root): path.read_bytes()
        for path in state_root.rglob("*")
        if path.is_file()
    }
    monkeypatch.setenv("KH_PROJECT_STATE_ROOT", str(state_root))
    monkeypatch.setenv("KH_HUB_CONFIG", str(Path("configs/knowledgehub.yaml").resolve()))
    monkeypatch.setattr("knowledgehub.project.knowledge.HubQueryService", FakeHubQueryService)
    for path in state_root.rglob("*"):
        path.chmod(0o500 if path.is_dir() else 0o400)
    state_root.chmod(0o500)
    tools = ToolRegistry(Service(), MCPConfig(max_response_bytes=500_000))

    result = anyio.run(
        tools.call,
        "knowledge_project_query",
        {
            "workspace_id": "my-private-project",
            "task": "project_overview",
            "query": "bounded query",
        },
    )
    assert not result.isError
    assert "read_only_workspace_scope" in result.structuredContent["result"]["warnings"]
    after = {
        path.relative_to(state_root): path.read_bytes()
        for path in state_root.rglob("*")
        if path.is_file()
    }
    assert after == before

    unknown = anyio.run(
        tools.call,
        "knowledge_project_query",
        {
            "workspace_id": "unknown-project",
            "task": "project_overview",
            "query": "test",
        },
    )
    assert unknown.isError
    traversal = anyio.run(
        tools.call,
        "knowledge_project_query",
        {
            "workspace_id": "../my-private-project",
            "task": "project_overview",
            "query": "test",
        },
    )
    assert traversal.isError
    assert traversal.structuredContent["error"]["code"] == "invalid_arguments"
