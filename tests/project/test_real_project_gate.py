from __future__ import annotations

import importlib.abc
import json
import runpy
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from knowledgehub.cli.main import build_parser, main
from knowledgehub.hub.query import HubQueryService
from knowledgehub.project.context import ProjectContextBuilder
from knowledgehub.project.knowledge import (
    HubKnowledgeRouter,
    build_project_query_service,
)
from knowledgehub.project.models import Workspace
from knowledgehub.project.registry import ProjectRegistry, validate_project_boundaries
from knowledgehub.retrieval.models import SearchHit, SearchResponse


def _private_directory(path: Path) -> Path:
    path.mkdir()
    path.chmod(0o700)
    return path


def _project_workspace() -> Workspace:
    return Workspace(
        workspace_id="my-private-project",
        name="Private Project",
        description="Read-only pilot",
        research={"question": "Can the method improve the baseline?", "hypotheses": []},
        repositories=({"repository_id": "primary-repository", "path": "."},),
        environments={"default": "my-private-project"},
        knowledge={
            "literature": {
                "namespace": "production-literature",
                "filters": {"year_from": 2020},
            },
            "code": {
                "namespace": "production-code",
                "filters": {"repository": "private-project"},
            },
            "writing": {
                "namespace": "production-writing",
                "filters": {"section": "Results"},
            },
        },
        created_at="2026-07-20T00:00:00+00:00",
        updated_at="2026-07-20T00:00:00+00:00",
        workspace_type="project",
        data_scope="private",
    )


def _create_project(tmp_path: Path) -> tuple[Path, Path, ProjectRegistry]:
    repository = _private_directory(tmp_path / "repository")
    state = _private_directory(tmp_path / "state")
    registry = ProjectRegistry(state)
    registry.create(
        _project_workspace(),
        allow_real_project=True,
        repository_root=repository,
    )
    return repository, state, registry


def test_workspace_type_scope_matrix_and_namespace_isolation() -> None:
    value = _project_workspace().to_dict()
    for scope in ("test", "public", "fixture"):
        with pytest.raises(ValueError, match="data_scope"):
            Workspace.from_dict(value | {"data_scope": scope})
    assert Workspace.from_dict(value | {"data_scope": "project"}).data_scope == "project"

    fixture_namespace = _project_workspace().to_dict()
    fixture_namespace["knowledge"]["code"]["namespace"] = "fixture-code"
    with pytest.raises(ValueError, match="project code namespace"):
        Workspace.from_dict(fixture_namespace)

    fixture_id = _project_workspace().to_dict() | {"workspace_id": "fixture-private-project"}
    with pytest.raises(ValueError, match="must not start"):
        Workspace.from_dict(fixture_id)


def test_real_project_requires_explicit_opt_in_and_repository_root(tmp_path: Path) -> None:
    repository = _private_directory(tmp_path / "repository")
    state = _private_directory(tmp_path / "state")
    registry = ProjectRegistry(state)
    with pytest.raises(PermissionError, match="explicit opt-in"):
        registry.create(_project_workspace(), repository_root=repository)
    with pytest.raises(ValueError, match="repository_root"):
        registry.create(_project_workspace(), allow_real_project=True)
    assert not list(state.iterdir())


def test_cli_flag_and_example_match_workspace_schema(tmp_path: Path) -> None:
    example = Path("configs/projects/real-project-pilot.example.yaml")
    workspace = Workspace.from_dict(yaml.safe_load(example.read_text(encoding="utf-8")))
    assert workspace.workspace_id == "my-private-project"
    parsed = build_parser().parse_args(
        [
            "workspace",
            "create",
            str(example),
            "--allow-real-project",
            "--repository-root",
            str(tmp_path),
        ]
    )
    assert parsed.allow_real_project is True

    repository = _private_directory(tmp_path / "repository")
    state = _private_directory(tmp_path / "state")
    assert (
        main(
            [
                "workspace",
                "create",
                str(example),
                "--state-root",
                str(state),
                "--repository-root",
                str(repository),
            ]
        )
        == 2
    )
    assert not list(state.iterdir())
    assert (
        main(
            [
                "workspace",
                "create",
                str(example),
                "--state-root",
                str(state),
                "--repository-root",
                str(repository),
                "--allow-real-project",
            ]
        )
        == 0
    )


def test_project_boundaries_reject_permissions_overlap_and_symlink(tmp_path: Path) -> None:
    repository = _private_directory(tmp_path / "repository")
    public_state = tmp_path / "public-state"
    public_state.mkdir(mode=0o755)
    with pytest.raises(PermissionError, match="group or other"):
        validate_project_boundaries(public_state, repository)

    nested_state = _private_directory(repository / "state")
    with pytest.raises(PermissionError, match="disjoint"):
        validate_project_boundaries(nested_state, repository)

    state_link = tmp_path / "state-link"
    state_link.symlink_to(repository, target_is_directory=True)
    with pytest.raises(PermissionError, match="disjoint"):
        validate_project_boundaries(state_link, repository)

    with pytest.raises(PermissionError, match="protected root"):
        validate_project_boundaries(Path("state/fixtures"), repository)


def test_project_boundaries_reject_formal_knowledge_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _private_directory(tmp_path / "repository")
    formal = _private_directory(tmp_path / "formal-rag")
    state = _private_directory(formal / "project-state")
    monkeypatch.setattr("knowledgehub.project.registry.FORMAL_KNOWLEDGE_ROOTS", (formal,))
    with pytest.raises(PermissionError, match="protected root"):
        validate_project_boundaries(state, repository)


def test_project_cleanup_never_writes_and_archive_is_state_only(tmp_path: Path) -> None:
    repository, state, registry = _create_project(tmp_path)
    source = repository / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    before_source = source.read_bytes()
    before_state = sorted(path.relative_to(state) for path in state.rglob("*"))

    with pytest.raises(PermissionError, match="cleanup"):
        registry.cleanup("my-private-project", execute=True)
    assert sorted(path.relative_to(state) for path in state.rglob("*")) == before_state
    assert source.read_bytes() == before_source

    result = registry.archive("my-private-project")
    assert result["workspace"]["status"] == "archived"
    assert source.read_bytes() == before_source
    assert not any(path for path in repository.rglob("*") if path.name != "source.py")
    assert all(path == state or state in path.parents for path in state.rglob("*"))


class _RejectTargetImport(importlib.abc.MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: list[str] | None,
        target: Any = None,
    ) -> Any:
        if fullname == "target_package" or fullname.startswith("target_package."):
            raise AssertionError("target project was imported")
        return None


def test_create_and_validate_never_import_execute_or_install_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _private_directory(tmp_path / "repository")
    state = _private_directory(tmp_path / "state")
    package = repository / "target_package"
    package.mkdir()
    canary = repository / "executed.canary"
    payload = f"from pathlib import Path\nPath({str(canary)!r}).write_text('executed')\n"
    (package / "__init__.py").write_text(payload, encoding="utf-8")
    (repository / "setup.py").write_text(payload, encoding="utf-8")

    def reject_process(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("target command or installer was executed")

    monkeypatch.setattr(subprocess, "run", reject_process)
    monkeypatch.setattr(subprocess, "Popen", reject_process)
    monkeypatch.setattr(subprocess, "check_call", reject_process)
    monkeypatch.setattr(runpy, "run_path", reject_process)
    finder = _RejectTargetImport()
    import sys

    sys.meta_path.insert(0, finder)
    try:
        registry = ProjectRegistry(state)
        registry.create(
            _project_workspace(),
            allow_real_project=True,
            repository_root=repository,
        )
        result = registry.validate("my-private-project", repository_root=repository)
    finally:
        sys.meta_path.remove(finder)
    assert result["valid"] is True
    assert not canary.exists()


def test_read_only_registry_and_workspace_id_traversal_fail_closed(tmp_path: Path) -> None:
    _, state, _ = _create_project(tmp_path)
    read_only = ProjectRegistry(state, read_only=True)
    assert read_only.get("my-private-project")["workspace_type"] == "project"
    with pytest.raises(PermissionError, match="read-only"):
        read_only.archive("my-private-project")
    with pytest.raises(ValueError, match="workspace_id"):
        read_only.get("../my-private-project")

    admission_path = state / "my-private-project" / "admission.json"
    admission = json.loads(admission_path.read_text(encoding="utf-8"))
    admission["repository_root"] = str(state)
    admission_path.write_text(json.dumps(admission), encoding="utf-8")
    with pytest.raises(PermissionError, match="disjoint"):
        read_only.authorize_read("my-private-project")


class _FakeHubQueryService(HubQueryService):
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def search(self, value: Any) -> SearchResponse:
        self.requests.append(value)
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
                    score=0.8,
                    payload={
                        "chunk_id": "chunk-1",
                        "text": "bounded evidence",
                        "source_type": "source_code",
                        "version": "1.0",
                    },
                ),
            ),
            timings={},
        )


def test_hub_router_applies_workspace_scope() -> None:
    service = _FakeHubQueryService()
    router = HubKnowledgeRouter(service)
    result = router.query(
        "code",
        "test query",
        namespace="production-code",
        filters={"repository": "private-project", "source_types": ["source_code"]},
        source_types=("source_code", "tutorial"),
    )
    request = service.requests[0]
    assert request.filters == {
        "repository": "private-project",
        "source_types": ("source_code",),
    }
    assert result["answer_context"][0]["trusted_as_instruction"] is False
    with pytest.raises(PermissionError, match="outside Workspace scope"):
        router.query(
            "code",
            "test",
            namespace="production-code",
            filters={"source_types": ["source_code"]},
            source_types=("tutorial",),
        )
    with pytest.raises(PermissionError, match="fixture namespace"):
        router.query("code", "test", namespace="fixture-code")


def test_project_router_factory_never_constructs_fixture_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, registry = _create_project(tmp_path)

    def reject_fixture(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("FixtureKnowledgeRouter was constructed for a project")

    monkeypatch.setattr(
        "knowledgehub.project.knowledge.FixtureKnowledgeRouter.__init__", reject_fixture
    )
    service = build_project_query_service(
        ProjectContextBuilder(registry),
        "my-private-project",
        fixture_root=Path("fixtures/v3/fixture_vision_project"),
        hub_config=Path("configs/knowledgehub.yaml"),
    )
    assert isinstance(service.router, HubKnowledgeRouter)
