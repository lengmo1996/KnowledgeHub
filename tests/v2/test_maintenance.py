from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
import yaml

from knowledgehub.cli.main import build_parser
from knowledgehub.code_rag.registry import CodeSourceRegistry
from knowledgehub.core.hashing import sha256_json
from knowledgehub.governance.maintenance import CleanupService, SyncPlanner


def _registry(tmp_path: Path) -> CodeSourceRegistry:
    path = tmp_path / "registry.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "defaults": {"include": ["README*"], "version_strategy": ["latest"]},
                "libraries": {
                    "demo": {
                        "enabled": True,
                        "package_name": "missing-demo-package",
                        "repository": "owner/demo",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return CodeSourceRegistry.load(path)


def test_maintenance_cli_defaults_to_planning() -> None:
    parser = build_parser()
    clean = parser.parse_args(
        ["clean", "source", "--library", "demo", "--version", "1.0"]
    )
    assert clean.execute is False and clean.yes is False
    sync = parser.parse_args(
        ["sync", "plan", "--trigger", "periodic", "--interval-hours", "24"]
    )
    assert sync.sync_domain == "plan" and sync.interval_hours == 24


def test_sync_plans_never_start_scheduler_or_download(tmp_path: Path) -> None:
    planner = SyncPlanner(_registry(tmp_path))
    release = planner.plan(trigger="release", libraries=["demo"])
    assert release["actions"][0]["action"] == "check_release_and_notify"
    assert release["automatic_download"] is False
    assert release["scheduler_started"] is False
    periodic = planner.plan(trigger="periodic", interval_hours=24)
    assert periodic["interval_hours"] == 24
    with pytest.raises(ValueError, match="interval_hours"):
        planner.plan(trigger="periodic")


def test_cleanup_protects_current_source_and_requires_confirmation(tmp_path: Path) -> None:
    code = tmp_path / "code"
    version = code / "sources" / "repositories" / "demo" / "1.0"
    current = version / "current-checkout"
    stale = version / "old-checkout"
    current.mkdir(parents=True)
    stale.mkdir()
    (stale / "old.py").write_text("old", encoding="utf-8")
    (version / "current.json").write_text(
        json.dumps({"source_path": str(current)}), encoding="utf-8"
    )
    service = CleanupService(
        code_root=code,
        rag_dirs={"code": tmp_path / "rag-code", "writing": tmp_path / "rag-writing"},
        index_root=tmp_path / "indexes",
    )
    plan = service.plan_source("demo", "1.0")
    assert plan["candidate_count"] == 1
    assert plan["candidates"][0]["path"] == str(stale)
    with pytest.raises(ValueError, match="confirmation"):
        service.execute(plan)
    result = service.execute(plan, confirmed=True)
    assert not stale.exists() and current.is_dir()
    assert Path(result["audit_manifest"]).is_file()


def test_cache_age_and_unreferenced_artifact_plans_are_bounded(tmp_path: Path) -> None:
    code = tmp_path / "code"
    old = code / ".staging" / "old"
    fresh = code / ".staging" / "fresh"
    old.mkdir(parents=True)
    fresh.mkdir()
    old_time = time.time() - 48 * 3600
    os.utime(old, (old_time, old_time))
    rag = tmp_path / "rag-code"
    chunks = rag / "chunks"
    chunks.mkdir(parents=True)
    state = rag / "state" / "index.sqlite3"
    state.parent.mkdir()
    with sqlite3.connect(state) as connection:
        connection.execute("CREATE TABLE documents(document_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO documents VALUES ('d1')")
    expected = chunks / f"{sha256_json('d1')[:32]}.jsonl"
    extra = chunks / "unreferenced.jsonl"
    expected.write_text("{}\n", encoding="utf-8")
    extra.write_text("{}\n", encoding="utf-8")
    service = CleanupService(
        code_root=code,
        rag_dirs={"code": rag},
        index_root=tmp_path / "indexes",
    )
    cache = service.plan_cache(min_age_hours=24)
    assert [Path(item["path"]).name for item in cache["candidates"]] == ["old"]
    prune = service.plan_unreferenced(["code"])
    assert [Path(item["path"]).name for item in prune["candidates"]] == [extra.name]

    missing_state = tmp_path / "rag-missing-state"
    (missing_state / "chunks").mkdir(parents=True)
    (missing_state / "chunks" / "unknown.jsonl").write_text("{}\n", encoding="utf-8")
    unsafe = CleanupService(
        code_root=code,
        rag_dirs={"code": missing_state},
        index_root=tmp_path / "indexes",
    )
    with pytest.raises(RuntimeError, match="cannot prove"):
        unsafe.plan_unreferenced(["code"])


def test_snapshot_cleanup_keeps_newest_and_current(tmp_path: Path) -> None:
    snapshots = tmp_path / "indexes" / "code" / "snapshots"
    snapshots.mkdir(parents=True)
    for index in range(4):
        path = snapshots / f"s{index}.json"
        path.write_text(
            json.dumps(
                {
                    "snapshot_id": f"s{index}",
                    "collection": "code",
                    "qdrant_snapshot": f"q{index}",
                }
            ),
            encoding="utf-8",
        )
        os.utime(path, (index + 1, index + 1))
    (tmp_path / "indexes" / "code" / "current.json").write_text(
        json.dumps({"snapshot_id": "s0"}), encoding="utf-8"
    )
    service = CleanupService(
        code_root=tmp_path / "code",
        rag_dirs={},
        index_root=tmp_path / "indexes",
    )
    plan = service.plan_snapshots("code", keep=2)
    assert [Path(item["path"]).stem for item in plan["candidates"]] == ["s1"]

    class Client:
        def __init__(self) -> None:
            self.deleted: list[tuple[str, str]] = []

        def delete_snapshot(self, collection, snapshot, wait=True):  # type: ignore[no-untyped-def]
            self.deleted.append((collection, snapshot))
            return True

    client = Client()
    service.execute(plan, confirmed=True, qdrant_client=client)
    assert client.deleted == [("code", "q1")]
    assert (snapshots / "s0.json").is_file()
