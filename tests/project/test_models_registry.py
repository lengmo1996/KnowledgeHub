from __future__ import annotations

from pathlib import Path

import pytest

from knowledgehub.core.locking import FileLock, LockBusyError
from knowledgehub.project.models import ClaimRecord, ExperimentRecord, Workspace
from knowledgehub.project.registry import ProjectRegistry


def workspace(now: str = "2026-07-17T00:00:00+00:00") -> Workspace:
    return Workspace(
        workspace_id="fixture-unit-project",
        name="Fixture Unit Project",
        description="test",
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
        created_at=now,
        updated_at=now,
    )


def experiment(experiment_id: str = "fixture-unit-exp-001") -> ExperimentRecord:
    return ExperimentRecord(
        experiment_id=experiment_id,
        workspace_id="fixture-unit-project",
        run_id=f"run-{experiment_id}",
        objective="test",
        hypothesis="test",
        repository_id="fixture-main",
        git_commit="abc123",
        git_dirty=False,
        environment_id="fixture-cpu",
        command="python test.py",
        config_path="fixtures/v3/fixture_vision_project/configs/baseline.yaml",
        config_hash="abc",
        dataset={"type": "synthetic"},
        status="completed",
        started_at="2026-07-17T00:00:00+00:00",
        ended_at="2026-07-17T00:00:01+00:00",
        seed=42,
        metrics={"accuracy": 1.0},
        artifacts=(),
    )


def test_workspace_rejects_formal_namespace() -> None:
    value = workspace().to_dict()
    value["knowledge"]["code"]["namespace"] = "production-code"
    with pytest.raises(ValueError, match="fixture code"):
        Workspace.from_dict(value)


def test_experiment_terminal_state_requires_end_time() -> None:
    value = experiment().to_dict()
    value["ended_at"] = None
    with pytest.raises(ValueError, match="ended_at"):
        ExperimentRecord.from_dict(value)


def test_claim_requires_scope_and_limitations() -> None:
    with pytest.raises(ValueError, match="scope and limitations"):
        ClaimRecord(
            claim_id="fixture-claim-999",
            workspace_id="fixture-unit-project",
            claim_type="experimental",
            claim="test",
            status="draft",
            evidence=(),
            scope="",
            limitations=(),
        )


def test_registry_is_idempotent_and_fixture_hidden(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / "state")
    assert registry.create(workspace())["status"] == "created"
    assert registry.create(workspace())["status"] == "unchanged"
    assert registry.list_workspaces() == []
    assert len(registry.list_workspaces(include_fixtures=True)) == 1
    registry.capture_fixture_environment("fixture-unit-project", "fixture-cpu")
    assert registry.validate("fixture-unit-project", repository_root=Path.cwd())["valid"]


def test_registry_refuses_overwrite_and_duplicate_run(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / "state")
    registry.create(workspace())
    first = experiment()
    registry.put_record("fixture-unit-project", "experiment", first.experiment_id, first.to_dict())
    changed = first.to_dict() | {"conclusion": "changed"}
    with pytest.raises(FileExistsError, match="immutable"):
        registry.put_record("fixture-unit-project", "experiment", first.experiment_id, changed)
    duplicate_run = experiment("fixture-unit-exp-002").to_dict() | {"run_id": first.run_id}
    with pytest.raises(FileExistsError, match="run_id"):
        registry.put_record(
            "fixture-unit-project", "experiment", "fixture-unit-exp-002", duplicate_run
        )


def test_cleanup_is_dry_run_and_bounded(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / "state")
    registry.create(workspace())
    target = registry.workspace_dir("fixture-unit-project")
    plan = registry.cleanup("fixture-unit-project")
    assert plan["dry_run"] is True
    assert target.is_dir()
    executed = registry.cleanup("fixture-unit-project", execute=True)
    assert executed["dry_run"] is False
    assert not target.exists()
    assert Path(executed["cleanup_manifest"]).is_file()


def test_workspace_archive_and_export(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / "state")
    registry.create(workspace())
    registry.capture_fixture_environment("fixture-unit-project", "fixture-cpu")
    assert registry.archive("fixture-unit-project")["workspace"]["status"] == "archived"
    assert registry.archive("fixture-unit-project")["status"] == "unchanged"
    assert registry.export("fixture-unit-project")["workspace"]["status"] == "archived"


def test_experiment_state_transition_keeps_event(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / "state")
    registry.create(workspace())
    running = experiment().to_dict() | {
        "status": "running",
        "ended_at": None,
        "metrics": {},
    }
    registry.put_record("fixture-unit-project", "experiment", running["experiment_id"], running)
    result = registry.transition_experiment(
        "fixture-unit-project",
        running["experiment_id"],
        "completed",
        {"ended_at": "2026-07-17T00:00:01+00:00", "metrics": {"accuracy": 1.0}},
    )
    assert result["record"]["status"] == "completed"
    assert Path(result["event"]).is_file()
    with pytest.raises(ValueError, match="invalid experiment transition"):
        registry.transition_experiment(
            "fixture-unit-project", running["experiment_id"], "running", {}
        )


def test_registry_write_fails_closed_when_cross_process_lock_is_busy(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / "state", lock_timeout_seconds=0.0)
    registry.create(workspace())
    lock = FileLock(
        registry.lock_path("fixture-unit-project"),
        sync_id="test:competing-process",
        timeout_seconds=0.0,
    )
    with lock, pytest.raises(LockBusyError, match="test:competing-process"):
        registry.archive("fixture-unit-project")
