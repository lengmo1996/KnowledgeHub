from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledgehub.governance.schema import SchemaRegistry
from knowledgehub.governance.snapshots import (
    CollectionPromotionManager,
    IndexSnapshotManager,
    active_collection,
)
from knowledgehub.governance.tasks import TaskStore


def test_schema_migration_is_explicit_and_strict() -> None:
    registry = SchemaRegistry()
    value = registry.migrate(
        "normalized_document",
        {"document_id": "d", "knowledge_base": "code", "content_hash": "a" * 64},
    ).to_dict()
    assert registry.validate(value).schema_version == "2.0"
    value["schema_version"] = "3.0"
    with pytest.raises(ValueError, match="incompatible"):
        registry.validate(value)


def test_task_idempotency_and_locks(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    first = store.begin("build", knowledge_base="code", library="transformers", inputs={"v": "1"})
    second = store.begin("build", knowledge_base="code", library="transformers", inputs={"v": "1"})
    assert first["task_id"] == second["task_id"]
    store.acquire("index:code", first["task_id"])
    with pytest.raises(RuntimeError, match="lock is held"):
        store.acquire("index:code", "other")
    store.release("index:code", force=True)
    store.finish(first["task_id"], "completed", output_manifest="manifest.json")
    assert store.list_tasks()[0]["status"] == "completed"


def test_snapshot_manifest_and_confirmation_gate(tmp_path: Path) -> None:
    class Client:
        recovered = False

        def get_collection(self, _name):  # type: ignore[no-untyped-def]
            return SimpleNamespace(points_count=7)

        def create_snapshot(self, _name, wait=True):  # type: ignore[no-untyped-def]
            return SimpleNamespace(name="snapshot-1", checksum="sum")

        def recover_snapshot(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.recovered = True
            return True

    client = Client()
    manager = IndexSnapshotManager(tmp_path, client)
    snapshot = manager.create("code", "collection")
    assert snapshot["points"] == 7 and len(manager.list("code")) == 1
    with pytest.raises(ValueError, match="confirmation"):
        manager.rollback("code", snapshot["snapshot_id"])
    assert manager.rollback("code", snapshot["snapshot_id"], confirmed=True)["restored_at"]
    assert client.recovered


def test_candidate_promotion_and_atomic_alias_rollback(tmp_path: Path) -> None:
    class Client:
        def __init__(self) -> None:
            self.aliases: dict[str, str] = {}

        def get_collection(self, name):  # type: ignore[no-untyped-def]
            return SimpleNamespace(points_count={"old": 7, "candidate": 9}[name])

        def get_aliases(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                aliases=[
                    SimpleNamespace(alias_name=alias, collection_name=collection)
                    for alias, collection in self.aliases.items()
                ]
            )

        def update_collection_aliases(self, operations):  # type: ignore[no-untyped-def]
            # Qdrant applies this operation list atomically. The fake mirrors its result.
            for operation in operations:
                if getattr(operation, "delete_alias", None):
                    self.aliases.pop(operation.delete_alias.alias_name, None)
                if getattr(operation, "create_alias", None):
                    self.aliases[operation.create_alias.alias_name] = (
                        operation.create_alias.collection_name
                    )
            return True

    client = Client()
    manager = CollectionPromotionManager(tmp_path, client)
    staged = manager.stage("code", "candidate")
    assert staged["candidate_points"] == 9
    with pytest.raises(ValueError, match="confirmation"):
        manager.promote("code", "old")
    promoted = manager.promote("code", "old", confirmed=True)
    assert promoted["previous_collection"] == "old"
    assert client.aliases["knowledgehub_code_current"] == "candidate"
    assert active_collection(tmp_path, "code", "old") == "knowledgehub_code_current"
    with pytest.raises(ValueError, match="confirmation"):
        manager.rollback("code")
    rolled_back = manager.rollback("code", confirmed=True)
    assert rolled_back["active_collection"] == "old"
    assert client.aliases["knowledgehub_code_current"] == "old"
