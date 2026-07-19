from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledgehub.core.hashing import sha256_json, sha256_text
from knowledgehub.governance.releases import CandidateReleaseManager
from knowledgehub.governance.schema import SchemaRegistry
from knowledgehub.governance.snapshots import (
    CollectionPromotionManager,
    IndexSnapshotManager,
    active_collection,
    active_release_data_dir,
    active_release_normalized_root,
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
            self.recovery_target = args[0]
            self.recovered = True
            return True

    client = Client()
    manager = IndexSnapshotManager(tmp_path, client)
    snapshot = manager.create("code", "collection")
    assert snapshot["points"] == 7 and len(manager.list("code")) == 1
    with pytest.raises(ValueError, match="confirmation"):
        manager.rollback("code", snapshot["snapshot_id"])
    with pytest.raises(ValueError, match="new target"):
        manager.rollback("code", snapshot["snapshot_id"], confirmed=True)
    restored = manager.rollback(
        "code",
        snapshot["snapshot_id"],
        target_collection="recovered-candidate",
        allow_qdrant_only=True,
        confirmed=True,
    )
    assert restored["restored_at"]
    assert restored["recovery_status"] == "candidate"
    assert client.recovery_target == "recovered-candidate"
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
    candidate_rag = tmp_path / "candidate-rag"
    candidate_rag.mkdir()
    candidate_normalized = tmp_path / "candidate-normalized"
    candidate_normalized.mkdir()
    release_path = tmp_path / "release.json"
    release_path.write_text(
        json.dumps({"normalized_root": str(candidate_normalized)}),
        encoding="utf-8",
    )
    fallback_rag = tmp_path / "old-rag"
    fallback_rag.mkdir()
    fallback_normalized = tmp_path / "old-normalized"
    fallback_normalized.mkdir()
    verified_release = {
        "status": "validated",
        "promotion_eligible": True,
        "collection": "candidate",
        "validation": {
            "valid": True,
            "index": {"qdrant": {"status": "green", "points": 9}},
        },
        "manifest_path": str(release_path),
        "artifact_fingerprint": "a" * 64,
        "rag_data_dir": str(candidate_rag),
    }
    staged = manager.stage(
        "code",
        "candidate",
        verified_release=verified_release,
    )
    assert staged["candidate_points"] == 9
    with pytest.raises(ValueError, match="confirmation"):
        manager.promote("code", "old")
    promoted = manager.promote("code", "old", confirmed=True)
    assert promoted["previous_collection"] == "old"
    assert client.aliases["knowledgehub_code_current"] == "candidate"
    assert active_collection(tmp_path, "code", "old") == "knowledgehub_code_current"
    assert active_release_data_dir(tmp_path, "code", fallback_rag) == candidate_rag
    assert (
        active_release_normalized_root(tmp_path, "code", fallback_normalized)
        == candidate_normalized
    )
    with pytest.raises(ValueError, match="confirmation"):
        manager.rollback("code")
    rolled_back = manager.rollback("code", confirmed=True)
    assert rolled_back["active_collection"] == "old"
    assert client.aliases["knowledgehub_code_current"] == "old"
    assert active_release_data_dir(tmp_path, "code", fallback_rag) == fallback_rag
    assert (
        active_release_normalized_root(tmp_path, "code", fallback_normalized) == fallback_normalized
    )
    with pytest.raises(ValueError, match="explicit confirmation"):
        manager.finalize_retired_previous("code", "candidate")
    finalized = manager.finalize_retired_previous(
        "code",
        "candidate",
        confirmed=True,
    )
    assert finalized["active_collection"] == "old"
    assert finalized["previous_collection"] is None
    assert finalized["retired_collection"] == "candidate"
    assert not (tmp_path / "code" / "aliases" / "staged.json").exists()


def test_candidate_release_validation_binds_local_artifacts_to_qdrant(
    tmp_path: Path,
) -> None:
    manager = CandidateReleaseManager(tmp_path / "releases")
    layout = manager.prepare(
        "code",
        "candidate",
        build_scope={"all_libraries": True},
        embedding={"model": "test", "revision": "a" * 40, "dimension": 2},
        promotion_eligible=True,
    )
    document_id = "code:owner/example@1.0:src/api.py"
    chunk_id = "00000000-0000-0000-0000-000000000001"
    content_hash = sha256_text("source")
    document_metadata = {
        "knowledge_base": "code",
        "library": "example",
        "version": "1.0",
        "source_type": "source_code",
        "commit": "a" * 40,
    }
    chunk_metadata = {
        **document_metadata,
        "source_url": "https://example.test/src/api.py",
    }
    normalized = layout.normalized_root / "example" / "1.0.jsonl"
    normalized.parent.mkdir(parents=True)
    normalized.write_text(
        json.dumps(
            {
                "document_id": document_id,
                "content_hash": content_hash,
                "source_url": "https://example.test/src/api.py",
                "metadata": document_metadata,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = layout.rag_data_dir / "state" / "index.sqlite3"
    state.parent.mkdir(parents=True)
    with sqlite3.connect(state) as connection:
        connection.executescript(
            """
            CREATE TABLE documents (
                document_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
                metadata_hash TEXT NOT NULL, processor_version TEXT NOT NULL,
                embedding_fingerprint TEXT NOT NULL, active INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE tombstones (
                document_id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL,
                reason TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?, 1, ?)",
            (
                document_id,
                content_hash,
                sha256_json(document_metadata),
                "code-ast-v1",
                "b" * 64,
                "2026-01-01T00:00:00+00:00",
            ),
        )
    artifact = layout.rag_data_dir / "chunks" / f"{sha256_json(document_id)[:32]}.jsonl"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        json.dumps(
            {
                "chunk_id": chunk_id,
                "document_id": document_id,
                "chunk_index": 0,
                "text": "source",
                "text_sha256": sha256_text("source"),
                "chunk_fingerprint": "fingerprint",
                "metadata": chunk_metadata,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manager.record_build(layout, [{"status": "success", "failures": []}])

    class Client:
        def get_collection(self, _name):  # type: ignore[no-untyped-def]
            return SimpleNamespace(points_count=1, status="green")

        def collection_exists(self, _name):  # type: ignore[no-untyped-def]
            return False

        def create_snapshot(self, _name, wait=True):  # type: ignore[no-untyped-def]
            return SimpleNamespace(name="snapshot-1", checksum="sum")

        def recover_snapshot(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return True

        def count(self, **_kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(count=1)

        def scroll(self, **_kwargs):  # type: ignore[no-untyped-def]
            return (
                [
                    SimpleNamespace(
                        id=chunk_id,
                        payload={
                            "chunk_id": chunk_id,
                            "document_id": document_id,
                            "knowledge_base": "code",
                        },
                    )
                ],
                None,
            )

    validated = manager.validate(layout, qdrant_client=Client())
    assert validated["status"] == "validated"
    assert validated["validation"]["index"]["qdrant"]["points"] == 1
    verified = manager.verify_validated(layout.manifest_path)
    assert verified["artifact_fingerprint"] == validated["artifact_fingerprint"]
    snapshots = IndexSnapshotManager(tmp_path / "indexes", Client())
    snapshot = snapshots.create(
        "code",
        "candidate",
        release_manifest=layout.manifest_path,
    )
    recovered = snapshots.rollback(
        "code",
        snapshot["snapshot_id"],
        target_collection="recovered-candidate",
        confirmed=True,
    )
    recovered_manifest = Path(recovered["release_manifest"])
    assert recovered["recovery_status"] == "candidate"
    assert recovered_manifest != layout.manifest_path
    assert json.loads(recovered_manifest.read_text())["status"] == "validated"
    artifact.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after validation"):
        manager.verify_validated(layout.manifest_path)


def test_promotion_transaction_recovers_after_alias_switch_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Client:
        def __init__(self) -> None:
            self.aliases = {"knowledgehub_code_current": "old"}

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
    manager.stage(
        "code",
        "candidate",
        verified_release={
            "status": "validated",
            "promotion_eligible": True,
            "collection": "candidate",
            "validation": {
                "valid": True,
                "index": {"qdrant": {"status": "green", "points": 9}},
            },
            "manifest_path": str(tmp_path / "release.json"),
            "artifact_fingerprint": "a" * 64,
            "rag_data_dir": str(tmp_path / "candidate-rag"),
        },
    )

    def interrupt() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(manager, "_after_alias_switch", interrupt)
    with pytest.raises(KeyboardInterrupt):
        manager.promote("code", "old", confirmed=True)
    assert client.aliases["knowledgehub_code_current"] == "candidate"
    transaction = json.loads((tmp_path / "code" / "aliases" / "transaction.json").read_text())
    assert transaction["status"] == "alias_switched"
    recovered = CollectionPromotionManager(tmp_path, client).recover_pending("code", "old")
    assert recovered["status"] == "committed"
    current = json.loads((tmp_path / "code" / "aliases" / "current.json").read_text())
    assert current["active_collection"] == "candidate"


def test_promotion_transaction_aborts_when_alias_switch_fails(tmp_path: Path) -> None:
    class Client:
        def get_collection(self, name):  # type: ignore[no-untyped-def]
            return SimpleNamespace(points_count={"old": 7, "candidate": 9}[name])

        def get_aliases(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                aliases=[
                    SimpleNamespace(
                        alias_name="knowledgehub_code_current",
                        collection_name="old",
                    )
                ]
            )

        def update_collection_aliases(self, _operations):  # type: ignore[no-untyped-def]
            raise RuntimeError("simulated alias failure")

    manager = CollectionPromotionManager(tmp_path, Client())
    manager.stage(
        "code",
        "candidate",
        verified_release={
            "status": "validated",
            "promotion_eligible": True,
            "collection": "candidate",
            "validation": {
                "valid": True,
                "index": {"qdrant": {"status": "green", "points": 9}},
            },
            "manifest_path": str(tmp_path / "release.json"),
            "artifact_fingerprint": "a" * 64,
            "rag_data_dir": str(tmp_path / "candidate-rag"),
        },
    )
    with pytest.raises(RuntimeError, match="simulated alias failure"):
        manager.promote("code", "old", confirmed=True)
    recovered = manager.recover_pending("code", "old")
    assert recovered["status"] == "aborted"
    assert active_collection(tmp_path, "code", "old") == "old"
