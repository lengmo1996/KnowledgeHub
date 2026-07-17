from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import anyio

from knowledgehub.mcp.config import MCPConfig
from knowledgehub.mcp.tools import ToolRegistry
from knowledgehub.project.models import Workspace
from knowledgehub.project.registry import ProjectRegistry


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


def test_project_query_and_skill_are_read_only_mcp_tools(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
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
