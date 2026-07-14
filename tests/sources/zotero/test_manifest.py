from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from knowledgehub.manifests.writer import write_delta, write_snapshot
from knowledgehub.sources.zotero.manifest import (
    build_delta_records,
    build_snapshot_records,
    collection_snapshot,
    document_id,
    document_state,
)


def _row(payload: dict[str, object], *, version: int = 1, deleted: int = 0) -> dict[str, object]:
    return {
        "object_version": version,
        "raw_json": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "deleted": deleted,
    }


def _snapshot_records(*, status: str = "ready", pdf_sha256: str | None = "b" * 64):
    parent = {
        "key": "PARENT",
        "version": 2,
        "data": {
            "key": "PARENT",
            "itemType": "journalArticle",
            "title": "A Paper",
            "creators": [
                {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"},
                {"creatorType": "author", "name": "Example Institute"},
            ],
            "date": "2026-07-14",
            "DOI": " 10.1000 / ABC ",
            "tags": [{"tag": "rag"}, {"tag": "AI"}, {"tag": "rag"}],
            "collections": ["C2", "C1"],
            "dateModified": "2026-07-14T10:00:00Z",
        },
    }
    attachment_payload = {
        "key": "ATTACH",
        "version": 3,
        "data": {
            "key": "ATTACH",
            "itemType": "attachment",
            "parentItem": "PARENT",
            "contentType": "application/pdf",
            "filename": "paper.pdf",
            "dateModified": "2026-07-14T11:00:00Z",
        },
    }
    objects = {
        "PARENT": _row(parent, version=2),
        "ATTACH": _row(attachment_payload, version=3),
    }
    collections = {
        "C1": {"name": "One", "path": "Research/One", "deleted": 0},
        "C2": {"name": "Two", "path": "Research/Two", "deleted": 0},
    }
    attachments = {
        "ATTACH": {
            "parent_item_key": "PARENT",
            "attachment_version": 3,
            "mime_type": "application/pdf",
            "resolver_status": status,
            "resolver_error": None,
            "archive_path": "/readonly/ATTACH.zip",
            "prop_path": "/readonly/ATTACH.prop",
            "prop_exists": 1,
            "archive_sha256": "a" * 64,
            "archive_size_bytes": 100,
            "archive_mtime_ns": 123,
            "pdf_path": "/cache/ATTACH/paper.pdf" if pdf_sha256 else None,
            "pdf_sha256": pdf_sha256,
            "pdf_size_bytes": 50 if pdf_sha256 else None,
        }
    }
    return build_snapshot_records(
        library_type="user",
        library_id=42,
        library_version=9,
        objects=objects,
        collections=collections,
        attachments=attachments,
    )


def test_snapshot_projection_is_stable_complete_and_sorted() -> None:
    records = _snapshot_records()

    assert len(records) == 1
    record = records[0]
    assert record["document_id"] == "zotero:user:42:PARENT:ATTACH:0"
    assert record["library_id"] == "42"
    assert record["library_version"] == 9
    assert record["title"] == "A Paper"
    assert record["doi"] == "10.1000/abc"
    assert record["year"] == 2026
    assert record["tags"] == ["AI", "rag"]
    assert record["creators"] == [
        {"creator_type": "author", "first_name": "Ada", "last_name": "Lovelace"},
        {"creator_type": "author", "name": "Example Institute"},
    ]
    assert [value["key"] for value in record["collections"]] == ["C1", "C2"]
    assert record["attachment"]["prop_exists"] is True
    assert record["content_fingerprint"] == "b" * 64
    assert record["updated_at"] == "2026-07-14T11:00:00Z"
    assert len(record["metadata_fingerprint"]) == 64
    assert len(record["document_fingerprint"]) == 64


def test_snapshot_includes_unready_pdf_with_null_content() -> None:
    record = _snapshot_records(status="missing_archive", pdf_sha256=None)[0]

    assert record["status"] == "missing_archive"
    assert record["content_fingerprint"] is None
    assert record["attachment"]["pdf_path"] is None
    assert record["attachment"]["pdf_sha256"] is None


def test_snapshot_excludes_non_pdf_orphan_and_deleted_attachment() -> None:
    base = _snapshot_records()[0]
    objects = {
        "PARENT": _row({"key": "PARENT", "data": {"key": "PARENT", "title": "P"}}),
        "DELETED": _row(
            {
                "key": "DELETED",
                "data": {"key": "DELETED", "parentItem": "PARENT", "itemType": "attachment"},
            },
            deleted=1,
        ),
    }
    attachments = {
        "NOTE": {"parent_item_key": "PARENT", "mime_type": "text/html"},
        "ORPHAN": {"parent_item_key": "MISSING", "mime_type": "application/pdf"},
        "DELETED": {"parent_item_key": "PARENT", "mime_type": "application/pdf"},
    }

    assert (
        build_snapshot_records(
            library_type="user",
            library_id=42,
            library_version=1,
            objects=objects,
            collections={},
            attachments=attachments,
        )
        == []
    )
    assert base["document_id"] == document_id("user", 42, "PARENT", "ATTACH", pdf_index=0)


def _previous(record: dict[str, object]) -> dict[str, object]:
    return {
        "document_fingerprint": record["document_fingerprint"],
        "metadata_fingerprint": record["metadata_fingerprint"],
        "content_fingerprint": record["content_fingerprint"],
        "manifest_json": json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "deleted": 0,
    }


def test_delta_new_document_requires_chunk_only_when_ready() -> None:
    ready = _snapshot_records()[0]
    missing = _snapshot_records(status="missing_archive", pdf_sha256=None)[0]

    ready_delta = build_delta_records(sync_id="sync", previous={}, current=[ready])[0]
    missing_delta = build_delta_records(sync_id="sync", previous={}, current=[missing])[0]

    assert ready_delta["reason"] == "new_document"
    assert ready_delta["operation"] == "upsert"
    assert ready_delta["chunk_required"] is True
    assert missing_delta["reason"] == "new_document"
    assert missing_delta["chunk_required"] is False


def test_delta_metadata_and_collection_chunk_policy() -> None:
    old = _snapshot_records()[0]
    metadata = deepcopy(old)
    metadata["title"] = "Changed"
    metadata["metadata_fingerprint"] = "c" * 64
    metadata["document_fingerprint"] = "d" * 64

    default = build_delta_records(
        sync_id="sync",
        previous={str(old["document_id"]): _previous(old)},
        current=[metadata],
    )[0]
    opted_in = build_delta_records(
        sync_id="sync",
        previous={str(old["document_id"]): _previous(old)},
        current=[metadata],
        metadata_changes_require_chunking=True,
    )[0]
    collection = deepcopy(metadata)
    collection["collections"] = []

    collection_delta = build_delta_records(
        sync_id="sync",
        previous={str(old["document_id"]): _previous(old)},
        current=[collection],
    )[0]

    assert (default["reason"], default["chunk_required"]) == ("metadata_changed", False)
    assert opted_in["chunk_required"] is True
    assert collection_delta["reason"] == "collection_changed"
    assert collection_delta["chunk_required"] is False


@pytest.mark.parametrize(
    ("old_status", "new_status", "old_hash", "new_hash", "reason", "required"),
    [
        ("missing_archive", "ready", None, "b" * 64, "attachment_became_available", True),
        ("ready", "missing_archive", "b" * 64, None, "attachment_missing", False),
        ("ready", "invalid_archive", "b" * 64, None, "attachment_became_invalid", False),
        ("ready", "ready", "b" * 64, "c" * 64, "content_changed", True),
    ],
)
def test_delta_attachment_transition_priority(
    old_status: str,
    new_status: str,
    old_hash: str | None,
    new_hash: str | None,
    reason: str,
    required: bool,
) -> None:
    old = _snapshot_records(status=old_status, pdf_sha256=old_hash)[0]
    new = _snapshot_records(status=new_status, pdf_sha256=new_hash)[0]

    delta = build_delta_records(
        sync_id="sync",
        previous={str(old["document_id"]): _previous(old)},
        current=[new],
    )[0]

    assert delta["reason"] == reason
    assert delta["chunk_required"] is required


def test_archive_replacement_and_explicit_delete_reasons() -> None:
    old = _snapshot_records()[0]
    replaced = deepcopy(old)
    replaced["attachment"]["archive_sha256"] = "f" * 64
    replace_delta = build_delta_records(
        sync_id="replace",
        previous={str(old["document_id"]): _previous(old)},
        current=[replaced],
    )[0]
    delete_delta = build_delta_records(
        sync_id="delete",
        previous={str(old["document_id"]): _previous(old)},
        current=[],
        delete_reasons={str(old["document_id"]): "zotero_attachment_deleted"},
    )[0]

    assert (replace_delta["reason"], replace_delta["chunk_required"]) == (
        "attachment_replaced",
        True,
    )
    assert delete_delta == {
        "schema_version": 1,
        "sync_id": "delete",
        "operation": "delete",
        "document_id": old["document_id"],
        "previous_fingerprint": old["document_fingerprint"],
        "current_fingerprint": None,
        "metadata_changed": False,
        "content_changed": False,
        "chunk_required": False,
        "reason": "zotero_attachment_deleted",
    }


def test_document_state_and_collection_snapshot_are_wire_ready() -> None:
    record = _snapshot_records()[0]
    state = document_state(record)
    collections = collection_snapshot(
        library_type="group",
        library_id=7,
        library_version=3,
        collections={
            "B": {"name": "B", "path": "Z/B", "parent_collection_key": None, "deleted": 0},
            "A": {"name": "A", "path": "A", "parent_collection_key": None, "deleted": 0},
            "D": {"name": "D", "path": "D", "deleted": 1},
        },
    )

    assert json.loads(state["manifest_json"]) == record
    assert state["deleted"] is False
    assert collections["library_id"] == "7"
    assert [value["key"] for value in collections["collections"]] == ["A", "B"]


def test_manifest_writers_are_deterministic_and_reject_duplicates(tmp_path: Path) -> None:
    one = _snapshot_records()[0]
    two = deepcopy(one)
    two["document_id"] = "zotero:user:42:A:A:0"
    snapshot = tmp_path / "snapshot.jsonl"

    write_snapshot(snapshot, [one, two])
    lines = [json.loads(line) for line in snapshot.read_text(encoding="utf-8").splitlines()]
    assert [line["document_id"] for line in lines] == sorted(
        [str(one["document_id"]), str(two["document_id"])]
    )
    assert snapshot.read_bytes().endswith(b"\n")
    with pytest.raises(ValueError, match="duplicate document_id"):
        write_snapshot(snapshot, [one, one])

    delta = build_delta_records(sync_id="s", previous={}, current=[one])[0]
    with pytest.raises(ValueError, match="duplicate document_id"):
        write_delta(tmp_path / "delta.jsonl", [delta, delta])
