from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import knowledgehub.cli.hub as hub_cli
from knowledgehub.cli.main import build_parser
from knowledgehub.governance.tasks import TaskConflictError, TaskExecutor, TaskStore
from knowledgehub.pipeline.config import RagConfig


def test_executor_repeats_completed_logical_task_and_keeps_attempts(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    executor = TaskExecutor(store)
    calls = 0

    def operation() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"status": "success", "manifest": f"run-{calls}.json"}

    def output_manifest(result: dict[str, Any]) -> str:
        return str(result["manifest"])

    first = executor.execute(
        "code_build",
        operation,
        knowledge_base="code",
        library="demo",
        version="1.0",
        inputs={"limit": 10},
        lock_keys=("library:demo", "index:code:test"),
        output_manifest=output_manifest,
    )
    second = executor.execute(
        "code_build",
        operation,
        knowledge_base="code",
        library="demo",
        version="1.0",
        inputs={"limit": 10},
        lock_keys=("library:demo", "index:code:test"),
        output_manifest=output_manifest,
    )
    assert calls == 2
    assert first["task"]["task_id"] == second["task"]["task_id"]
    assert first["task"]["reused"] is False
    assert second["task"]["reused"] is True
    attempts = store.list_attempts(str(first["task"]["task_id"]))
    assert [item["attempt_number"] for item in attempts] == [1, 2]
    assert [item["status"] for item in attempts] == ["completed", "completed"]
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM locks").fetchone()[0] == 0


def test_executor_failure_is_recorded_and_next_attempt_is_a_retry(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    executor = TaskExecutor(store)

    def fail() -> dict[str, Any]:
        raise RuntimeError("bounded failure")

    with pytest.raises(RuntimeError, match="bounded failure"):
        executor.execute(
            "writing_derive",
            fail,
            knowledge_base="writing",
            inputs={"limit": 5},
            lock_keys=("derive:writing",),
        )
    failed = store.list_tasks()[0]
    assert failed["status"] == "failed"
    assert failed["retry_count"] == 0
    result = executor.execute(
        "writing_derive",
        lambda: {"status": "success"},
        knowledge_base="writing",
        inputs={"limit": 5},
        lock_keys=("derive:writing",),
    )
    assert result["task"]["retry_count"] == 1
    assert [item["status"] for item in store.list_attempts(failed["task_id"])] == [
        "failed",
        "completed",
    ]


def test_equivalent_running_task_is_rejected_without_overwriting_owner(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    owner = store.begin(
        "code_sync",
        knowledge_base="code",
        library="demo",
        inputs={"version": "1.0"},
        reuse_completed=False,
    )
    with pytest.raises(TaskConflictError, match=str(owner["task_id"])):
        TaskExecutor(store).execute(
            "code_sync",
            lambda: {"status": "success"},
            knowledge_base="code",
            library="demo",
            inputs={"version": "1.0"},
            lock_keys=("library:demo",),
        )
    assert store.get(str(owner["task_id"]))["status"] == "running"  # type: ignore[index]
    assert len(store.list_attempts(str(owner["task_id"]))) == 1


def test_resource_lock_conflict_fails_contender_and_preserves_owner(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    owner = store.begin("owner", inputs={"id": 1}, reuse_completed=False)
    store.acquire("index:code:test", str(owner["task_id"]))
    with pytest.raises(RuntimeError, match="lock is held"):
        TaskExecutor(store).execute(
            "contender",
            lambda: {"status": "success"},
            knowledge_base="code",
            inputs={"id": 2},
            lock_keys=("index:code:test",),
        )
    tasks = {item["task_type"]: item for item in store.list_tasks()}
    assert tasks["owner"]["status"] == "running"
    assert tasks["contender"]["status"] == "failed"
    with store.connect() as connection:
        lock = connection.execute(
            "SELECT task_id FROM locks WHERE lock_key='index:code:test'"
        ).fetchone()
    assert lock[0] == owner["task_id"]


def test_stale_running_task_and_lock_are_recovered_as_retry(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    owner = store.begin(
        "code_sync",
        knowledge_base="code",
        library="demo",
        inputs={"version": "1.0"},
        reuse_completed=False,
    )
    store.acquire("library:demo", str(owner["task_id"]), ttl_seconds=60)
    with store.connect() as connection:
        connection.execute(
            "UPDATE tasks SET started_at='2000-01-01T00:00:00+00:00' WHERE task_id=?",
            (owner["task_id"],),
        )
        connection.execute(
            "UPDATE locks SET expires_at='2000-01-01T00:00:01+00:00' WHERE task_id=?",
            (owner["task_id"],),
        )
    result = TaskExecutor(store).execute(
        "code_sync",
        lambda: {"status": "success"},
        knowledge_base="code",
        library="demo",
        inputs={"version": "1.0"},
        lock_keys=("library:demo",),
        ttl_seconds=1,
    )
    assert result["task"]["task_id"] == owner["task_id"]
    assert result["task"]["retry_count"] == 1
    attempts = store.list_attempts(str(owner["task_id"]))
    assert [item["status"] for item in attempts] == ["failed", "completed"]
    assert attempts[0]["error_summary"] == "stale_task_recovered"
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM locks").fetchone()[0] == 0


def test_live_lease_prevents_old_running_task_from_stale_recovery(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    owner = store.begin(
        "code_build",
        knowledge_base="code",
        inputs={"limit": 1},
        reuse_completed=False,
    )
    store.acquire("index:code:test", str(owner["task_id"]), ttl_seconds=60)
    with store.connect() as connection:
        connection.execute(
            "UPDATE tasks SET started_at='2000-01-01T00:00:00+00:00' WHERE task_id=?",
            (owner["task_id"],),
        )
    repeated = store.begin(
        "code_build",
        knowledge_base="code",
        inputs={"limit": 1},
        reuse_completed=False,
        stale_after_seconds=1,
    )
    assert repeated["execution_required"] is False
    assert repeated["status"] == "running"
    assert len(store.list_attempts(str(owner["task_id"]))) == 1


def test_executor_heartbeats_and_fails_closed_when_lease_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    renewals = 0
    original = store.renew

    def counting_renew(*args: Any, **kwargs: Any) -> None:
        nonlocal renewals
        renewals += 1
        original(*args, **kwargs)

    monkeypatch.setattr(store, "renew", counting_renew)
    result = TaskExecutor(store).execute(
        "code_build",
        lambda: (time.sleep(0.08) or {"status": "success"}),
        knowledge_base="code",
        inputs={"limit": 1},
        lock_keys=("index:code:test",),
        ttl_seconds=1,
        heartbeat_interval_seconds=0.01,
    )
    assert result["task"]["status"] == "completed"
    assert renewals >= 2

    def lost_lease(*_args: Any, **_kwargs: Any) -> None:
        raise TaskConflictError("simulated lease loss")

    monkeypatch.setattr(store, "renew", lost_lease)
    with pytest.raises(TaskConflictError, match="simulated lease loss"):
        TaskExecutor(store).execute(
            "writing_derive",
            lambda: (time.sleep(0.04) or {"status": "success"}),
            knowledge_base="writing",
            inputs={"limit": 2},
            lock_keys=("index:writing:test",),
            ttl_seconds=1,
            heartbeat_interval_seconds=0.01,
        )
    failed = next(
        item for item in store.list_tasks() if item["task_type"] == "writing_derive"
    )
    assert failed["status"] == "failed"
    assert "simulated lease loss" in failed["error_summary"]


def test_task_store_migrates_v2_database_without_losing_rows(tmp_path: Path) -> None:
    path = tmp_path / "tasks.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
              task_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
              task_type TEXT NOT NULL, status TEXT NOT NULL, knowledge_base TEXT,
              library TEXT, version TEXT, started_at TEXT NOT NULL, ended_at TEXT,
              input_manifest TEXT, output_manifest TEXT, error_summary TEXT,
              retry_count INTEGER NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL
            );
            CREATE TABLE locks (
              lock_key TEXT PRIMARY KEY, task_id TEXT NOT NULL,
              acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL
            );
            INSERT INTO tasks VALUES(
              'old','key','build','completed','code','demo','1.0','start','end',
              NULL,NULL,NULL,0,'{}'
            );
            """
        )
    store = TaskStore(path)
    assert store.get("old")["status"] == "completed"  # type: ignore[index]
    with store.connect() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        attempts = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='attempts'"
        ).fetchone()
    assert "result_json" in columns
    assert attempts is not None


def test_cli_tracks_sync_build_and_derive_but_not_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = TaskStore(tmp_path / "state" / "tasks.sqlite3")
    executor = TaskExecutor(store)
    rag_configs = {
        "code": RagConfig(
            data_dir=tmp_path / "rag-code",
            gpu_mode="cpu",
            embedding_dim=2,
            qdrant_collection="code-test",
        ),
        "writing": RagConfig(
            data_dir=tmp_path / "rag-writing",
            gpu_mode="cpu",
            embedding_dim=2,
            qdrant_collection="writing-test",
        ),
    }
    config = SimpleNamespace(
        code=SimpleNamespace(
            registry=tmp_path / "registry.yaml",
            data_root=tmp_path / "code",
            github_token_env="GITHUB_TOKEN",
            timeout_seconds=1,
            max_retries=1,
        ),
        writing=SimpleNamespace(
            literature_data_dir=tmp_path / "literature",
            data_root=tmp_path / "writing",
            processor_version="rules-test",
            minimum_quality=0.1,
            default_limit=5,
        ),
        knowledge_bases={
            "code": SimpleNamespace(collection="code-test"),
            "writing": SimpleNamespace(collection="writing-test"),
        },
        rag_config=lambda name: rag_configs[name],
    )
    monkeypatch.setattr(hub_cli.HubConfig, "load", lambda _path: config)
    monkeypatch.setattr(hub_cli.CodeSourceRegistry, "load", lambda _path: object())
    monkeypatch.setattr(hub_cli, "_task_executor", lambda: executor)

    def sync(_self: Any, library: str, *, version: str | None, dry_run: bool) -> dict[str, Any]:
        return {
            "status": "planned" if dry_run else "success",
            "library": library,
            "version": version,
        }

    monkeypatch.setattr(hub_cli.CodeSyncService, "sync", sync)

    class FakeBuild:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def build(self, *_args: Any, dry_run: bool, **_kwargs: Any) -> dict[str, Any]:
            return {
                "status": "success",
                "dry_run": dry_run,
                "normalized_manifest": str(tmp_path / "normalized.jsonl"),
            }

        def close(self) -> None:
            pass

    class FakeWriting:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def derive(self, *, dry_run: bool, **_kwargs: Any) -> dict[str, Any]:
            return {
                "status": "success",
                "dry_run": dry_run,
                "derived_manifest": str(tmp_path / "writing.jsonl"),
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr(hub_cli, "CodeBuildService", FakeBuild)
    monkeypatch.setattr(hub_cli, "WritingDerivationService", FakeWriting)

    class FakeWatch:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def check(self, library: str, *, dry_run: bool) -> dict[str, Any]:
            return {"library": library, "dry_run": dry_run, "action": "none"}

    class FakeImporter:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def import_version(
            self,
            library: str,
            version: str,
            *,
            allowed: bool,
            build_limit: int,
            dry_run: bool,
        ) -> dict[str, Any]:
            return {
                "status": "completed" if allowed and not dry_run else "planned",
                "library": library,
                "version": version,
                "build_limit": build_limit,
            }

    monkeypatch.setattr(hub_cli, "ReleaseWatchService", FakeWatch)
    monkeypatch.setattr(hub_cli, "OnDemandVersionImporter", FakeImporter)

    commands = [
        ["sync", "code", "--library", "demo", "--version", "1.0"],
        ["sync", "releases", "--library", "demo"],
        [
            "sync",
            "version",
            "--library",
            "demo",
            "--version",
            "2.0",
            "--allow-download",
        ],
        ["build", "code", "--library", "demo", "--version", "1.0"],
        ["derive", "writing", "--limit", "2"],
    ]
    for command in commands:
        assert hub_cli.run_hub_command(build_parser().parse_args(command)) == 0
        value = json.loads(capsys.readouterr().out)
        if command[:2] in (["sync", "code"], ["sync", "releases"]):
            assert value["results"][0]["task"]["status"] == "completed"
        else:
            assert value["task"]["status"] == "completed"
    assert {item["task_type"] for item in store.list_tasks()} == {
        "code_sync",
        "code_version_import",
        "release_watch",
        "code_build",
        "writing_derive",
    }

    before = len(store.list_tasks())
    assert hub_cli.run_hub_command(
        build_parser().parse_args(
            ["sync", "code", "--library", "other", "--version", "1.0", "--dry-run"]
        )
    ) == 0
    capsys.readouterr()
    assert len(store.list_tasks()) == before
