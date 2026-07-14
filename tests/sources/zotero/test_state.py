from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from knowledgehub.sources.zotero.models import SyncSummary, ZoteroError
from knowledgehub.sources.zotero.state import SCHEMA_VERSION, ZoteroStateStore, canonical_raw


@pytest.fixture
def store(tmp_path: Path) -> ZoteroStateStore:
    value = ZoteroStateStore(tmp_path / "data")
    value.initialize()
    return value


def test_initialize_creates_schema_and_configures_connections(store: ZoteroStateStore) -> None:
    expected_tables = {
        "library_state",
        "objects",
        "collections",
        "attachments",
        "documents",
        "sync_runs",
        "mapping_validation",
        "deletion_events",
    }

    with store.connect() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
        assert int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        assert int(connection.execute("PRAGMA busy_timeout").fetchone()[0]) == 30_000
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert int(connection.execute("PRAGMA synchronous").fetchone()[0]) == 2

    assert tables == expected_tables
    assert store.quick_check() == ["ok"]


def test_readonly_connection_is_query_only_and_does_not_create_sidecars(
    store: ZoteroStateStore,
) -> None:
    before = {path.name for path in store.path.parent.iterdir()}

    with store.connect_readonly() as connection:
        assert int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1
        assert connection.execute("SELECT count(*) FROM library_state").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO deletion_events VALUES (1, 's', 'i', 'k', 'now')")

    assert {path.name for path in store.path.parent.iterdir()} == before


def test_readonly_connection_rejects_state_database_symlink(tmp_path: Path) -> None:
    external = ZoteroStateStore(tmp_path / "external")
    external.initialize()
    linked = ZoteroStateStore(tmp_path / "linked")
    linked.path.parent.mkdir(parents=True)
    linked.path.symlink_to(external.path)

    with pytest.raises(ZoteroError, match="non-symlink") as error:
        linked.connect_readonly()

    assert error.value.code == "state_error"


def test_initialize_rejects_newer_schema(tmp_path: Path) -> None:
    store = ZoteroStateStore(tmp_path / "data")
    store.path.parent.mkdir(parents=True)
    connection = sqlite3.connect(store.path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()

    with pytest.raises(ZoteroError, match="newer than supported") as error:
        store.initialize()

    assert error.value.code == "schema_error"


def test_transactions_commit_and_rollback(store: ZoteroStateStore) -> None:
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO deletion_events(sync_id, object_type, object_key, deleted_at) "
            "VALUES ('commit', 'item', 'A', 'now')"
        )

    with pytest.raises(RuntimeError, match="rollback"):
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO deletion_events(sync_id, object_type, object_key, deleted_at) "
                "VALUES ('rollback', 'item', 'B', 'now')"
            )
            raise RuntimeError("rollback")

    with store.connect() as connection:
        rows = connection.execute("SELECT sync_id FROM deletion_events ORDER BY id").fetchall()
    assert [row[0] for row in rows] == ["commit"]


def test_library_binding_is_idempotent_and_rejects_reuse(store: ZoteroStateStore) -> None:
    store.bind_library("user", 42)
    store.bind_library("user", 42)

    assert store.library_state() == {
        "singleton": 1,
        "library_type": "user",
        "library_id": 42,
        "library_version": 0,
        "schema_version": SCHEMA_VERSION,
        "last_attempted_sync_at": None,
        "last_successful_sync_at": None,
        "active_sync_id": None,
    }
    assert store.mapping_validation()["status"] == "unverified"

    with pytest.raises(ZoteroError) as error:
        store.bind_library("group", 42)
    assert error.value.code == "library_binding_mismatch"


def test_remote_objects_preserve_canonical_raw_json_and_parent(store: ZoteroStateStore) -> None:
    payload = {
        "version": 2,
        "key": "ATTACH",
        "meta": {"creatorSummary": "张三"},
        "data": {"key": "ATTACH", "itemType": "attachment", "parentItem": "PARENT"},
    }

    with store.transaction() as connection:
        key, changed = store.upsert_remote_object(connection, "item", payload)
        _, unchanged = store.upsert_remote_object(connection, "item", payload)

    row = store.load_objects("item")["ATTACH"]
    assert key == "ATTACH"
    assert changed is True
    assert unchanged is False
    assert row["parent_item_key"] == "PARENT"
    assert row["object_version"] == 2
    assert row["raw_json"] == canonical_raw(payload)
    assert json.loads(row["raw_json"]) == payload
    assert "张三" in row["raw_json"]


def test_remote_object_validation_and_resurrection(store: ZoteroStateStore) -> None:
    payload = {"key": "ITEM", "version": 1, "data": {"key": "ITEM", "title": "old"}}
    with store.transaction() as connection:
        store.upsert_remote_object(connection, "item", payload)
        assert store.mark_deleted(connection, "item", "ITEM", sync_id="delete")
        _, changed = store.upsert_remote_object(connection, "item", payload)
        assert changed

    assert store.load_objects("item")["ITEM"]["deleted"] == 0

    with store.transaction() as connection:
        with pytest.raises(ZoteroError, match="has no key"):
            store.upsert_remote_object(connection, "item", {"version": 1})
        with pytest.raises(ZoteroError, match="invalid version"):
            store.upsert_remote_object(
                connection,
                "item",
                {"key": "BAD", "version": "not-an-int", "data": {}},
            )


def test_collections_store_raw_data_paths_and_tombstones(store: ZoteroStateStore) -> None:
    payload = {
        "key": "CHILD",
        "version": 3,
        "data": {"key": "CHILD", "name": "论文", "parentCollection": "ROOT"},
    }
    with store.transaction() as connection:
        key, changed = store.upsert_collection(connection, payload)
        store.update_collection_paths(connection, {"CHILD": "Research/论文"})
        deleted = store.mark_deleted(connection, "collection", "CHILD", sync_id="sync-1")
        repeated = store.mark_deleted(connection, "collection", "CHILD", sync_id="sync-1")

    row = store.load_collections(include_deleted=True)["CHILD"]
    assert (key, changed, deleted, repeated) == ("CHILD", True, True, False)
    assert row["parent_collection_key"] == "ROOT"
    assert row["path"] == "Research/论文"
    assert row["deleted"] == 1
    assert json.loads(row["raw_json"]) == payload
    assert store.load_collections() == {}

    with store.connect() as connection:
        events = connection.execute(
            "SELECT sync_id, object_type, object_key FROM deletion_events"
        ).fetchall()
    assert [tuple(row) for row in events] == [("sync-1", "collection", "CHILD")]


def test_attachment_round_trip_normalizes_candidates(store: ZoteroStateStore) -> None:
    value = {
        "attachment_key": "ATTACH",
        "parent_item_key": "PARENT",
        "attachment_version": 4,
        "link_mode": "imported_file",
        "mime_type": "application/pdf",
        "api_filename": "paper.pdf",
        "archive_path": "/readonly/ATTACH.zip",
        "prop_path": "/readonly/ATTACH.prop",
        "prop_exists": 1,
        "archive_sha256": "a" * 64,
        "archive_size_bytes": 100,
        "archive_mtime_ns": 123,
        "pdf_path": "/data/extracted/ATTACH/paper.pdf",
        "pdf_sha256": "b" * 64,
        "pdf_size_bytes": 50,
        "resolver_status": "ready",
        "resolver_error": None,
        "pdf_candidates": ["z.pdf", "a.pdf"],
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    with store.transaction() as connection:
        store.upsert_attachment(connection, value)

    row = store.load_attachments()["ATTACH"]
    assert row["pdf_candidates"] == ["z.pdf", "a.pdf"]
    assert "pdf_candidates_json" not in row
    assert row["archive_sha256"] == "a" * 64
    assert row["resolver_status"] == "ready"


def _document(*, fingerprint: str, updated_at: str) -> dict[str, object]:
    return {
        "document_id": "zotero:user:42:PARENT:ATTACH:0",
        "parent_item_key": "PARENT",
        "attachment_key": "ATTACH",
        "pdf_index": 0,
        "metadata_fingerprint": "a" * 64,
        "content_fingerprint": "b" * 64,
        "document_fingerprint": fingerprint,
        "status": "ready",
        "manifest_json": "{}",
        "updated_at": updated_at,
    }


def test_document_timestamp_only_changes_with_fingerprint_or_delete(
    store: ZoteroStateStore,
) -> None:
    with store.transaction() as connection:
        store.upsert_document(connection, _document(fingerprint="c" * 64, updated_at="first"))
        store.upsert_document(connection, _document(fingerprint="c" * 64, updated_at="second"))

    document_id = "zotero:user:42:PARENT:ATTACH:0"
    assert store.load_documents()[document_id]["updated_at"] == "first"

    with store.transaction() as connection:
        store.upsert_document(connection, _document(fingerprint="d" * 64, updated_at="third"))
    assert store.load_documents()[document_id]["updated_at"] == "third"

    with store.transaction() as connection:
        store.mark_document_deleted(connection, document_id, "zotero_attachment_deleted")
    assert store.load_documents(include_deleted=False) == {}
    assert store.load_documents()[document_id]["delete_reason"] == "zotero_attachment_deleted"


def test_mapping_validation_round_trip_is_canonical(store: ZoteroStateStore) -> None:
    store.bind_library("user", 42)
    with store.transaction() as connection:
        store.set_mapping_validation(
            connection,
            status="verified",
            library_type="user",
            library_id=42,
            webdav_realpath="/readonly/zotero",
            sample_count=2,
            passed_count=2,
            summary={"z": 1, "a": ["A", "B"]},
        )

    result = store.mapping_validation()
    assert result is not None
    assert result["status"] == "verified"
    assert result["summary_json"] == '{"a":["A","B"],"z":1}'


def test_run_audit_and_success_version_are_persisted(store: ZoteroStateStore) -> None:
    store.bind_library("user", 42)
    summary = SyncSummary(sync_id="run-1", mode="incremental", status="running", from_version=5)
    store.start_run(summary)

    running = store.recent_runs()[0]
    assert running["status"] == "running"
    assert running["from_version"] == 5
    assert store.library_state()["last_attempted_sync_at"] is not None

    summary.status = "success"
    summary.target_version = 8
    summary.committed_version = 8
    summary.added = 2
    summary.updated = 3
    summary.deleted = 1
    summary.attachments_ready = 4
    summary.delta_upserts = 5
    summary.duration_seconds = 1.25
    with store.transaction() as connection:
        store.set_success_version(connection, version=8, sync_id=summary.sync_id)
        store.finish_run_in_transaction(connection, summary)

    finished = store.recent_runs()[0]
    assert finished["status"] == "success"
    assert finished["committed_version"] == 8
    assert finished["added_count"] == 2
    assert finished["updated_count"] == 3
    assert finished["deleted_count"] == 1
    assert finished["attachments_ready"] == 4
    assert finished["delta_upserts"] == 5
    assert finished["duration_seconds"] == 1.25
    state = store.library_state()
    assert state["library_version"] == 8
    assert state["active_sync_id"] == "run-1"
    assert state["last_successful_sync_at"] is not None


def test_failed_run_is_audited_without_advancing_library_version(store: ZoteroStateStore) -> None:
    store.bind_library("group", 7)
    summary = SyncSummary(sync_id="failed", mode="full", status="running", from_version=0)
    store.start_run(summary)
    summary.status = "failed"
    summary.target_version = 12
    summary.error_code = "network_error"
    summary.error_message = "sanitized failure"
    store.finish_run(summary)

    run = store.recent_runs()[0]
    assert run["status"] == "failed"
    assert run["error_code"] == "network_error"
    assert run["error_message"] == "sanitized failure"
    assert run["committed_version"] is None
    assert store.library_state()["library_version"] == 0
