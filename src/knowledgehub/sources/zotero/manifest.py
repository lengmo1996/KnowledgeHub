"""Projection of Zotero state into the stable manifest v1 contract."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from .fingerprints import build_fingerprints, chunk_required, normalise_metadata

SCHEMA_VERSION = 1
READY = "ready"
_INVALID_STATUSES = {
    "unstable_archive",
    "invalid_archive",
    "missing_pdf",
    "ambiguous_attachment",
    "unsupported_attachment",
    "mapping_unverified",
    "error",
}


def build_snapshot_records(
    *,
    library_type: str,
    library_id: int,
    library_version: int,
    objects: Mapping[str, Mapping[str, Any]],
    collections: Mapping[str, Mapping[str, Any]],
    attachments: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build one record per API PDF attachment with a valid parent item."""

    records: list[dict[str, Any]] = []
    for attachment_key in sorted(attachments):
        attachment = attachments[attachment_key]
        if str(attachment.get("mime_type") or "").lower() != "application/pdf":
            continue
        parent_key = str(attachment.get("parent_item_key") or "")
        parent_row = objects.get(parent_key)
        if not parent_key or parent_row is None or bool(parent_row.get("deleted")):
            continue
        attachment_row = objects.get(attachment_key)
        if attachment_row is None or bool(attachment_row.get("deleted")):
            continue
        parent_payload = _raw(parent_row)
        attachment_payload = _raw(attachment_row)
        parent_data = _data(parent_payload)
        attachment_data = _data(attachment_payload)

        collection_refs: list[dict[str, str]] = []
        raw_collection_keys = parent_data.get("collections", [])
        if isinstance(raw_collection_keys, list):
            for collection_key_value in raw_collection_keys:
                collection_key = str(collection_key_value)
                collection = collections.get(collection_key)
                if collection is None or bool(collection.get("deleted")):
                    continue
                collection_refs.append(
                    {
                        "key": collection_key,
                        "name": str(collection.get("name") or ""),
                        "path": str(
                            collection.get("path") or collection.get("name") or collection_key
                        ),
                    }
                )
        collection_refs.sort(key=lambda value: (value["path"], value["key"]))

        metadata_input = dict(parent_data)
        metadata_input["collection_refs"] = collection_refs
        normalized = normalise_metadata(metadata_input)
        status = str(attachment.get("resolver_status") or "metadata_only")
        content_hash = str(attachment["pdf_sha256"]) if attachment.get("pdf_sha256") else None
        fingerprints = build_fingerprints(metadata_input, content_hash, status)
        creators = normalized["creators"] if isinstance(normalized["creators"], list) else []
        year_value = normalized.get("year")
        year = int(year_value) if isinstance(year_value, str) and year_value.isdigit() else None
        status_detail = _status_detail(attachment.get("resolver_error"))
        updated_at = _stable_updated_at(parent_data, attachment_data)
        record = {
            "schema_version": SCHEMA_VERSION,
            "document_id": document_id(
                library_type, library_id, parent_key, attachment_key, pdf_index=0
            ),
            "source": "zotero",
            "library_type": library_type,
            "library_id": str(library_id),
            "library_version": library_version,
            "item_key": parent_key,
            "item_version": int(parent_row.get("object_version") or 0),
            "item_type": normalized.get("item_type") or str(parent_data.get("itemType") or ""),
            "attachment_key": attachment_key,
            "attachment_version": int(attachment.get("attachment_version") or 0),
            "pdf_index": 0,
            "title": normalized.get("title") or "",
            "creators": creators,
            "abstract": normalized.get("abstract") or "",
            "publication_title": normalized.get("publication") or "",
            "date": normalized.get("date") or "",
            "year": year,
            "doi": normalized.get("doi") or "",
            "url": normalized.get("url") or "",
            "language": normalized.get("language") or "",
            "rights": normalized.get("rights") or "",
            "relations": normalized.get("relations") or {},
            "tags": normalized.get("tags") or [],
            "collections": collection_refs,
            "mime_type": "application/pdf",
            "attachment": {
                "backend": "nutstore_webdav",
                "archive_path": attachment.get("archive_path"),
                "prop_path": attachment.get("prop_path"),
                "prop_exists": bool(attachment.get("prop_exists")),
                "archive_sha256": attachment.get("archive_sha256"),
                "archive_size_bytes": attachment.get("archive_size_bytes"),
                "archive_mtime_ns": attachment.get("archive_mtime_ns"),
                "pdf_path": attachment.get("pdf_path"),
                "pdf_sha256": attachment.get("pdf_sha256"),
                "pdf_size_bytes": attachment.get("pdf_size_bytes"),
            },
            "metadata_fingerprint": fingerprints.metadata,
            "content_fingerprint": fingerprints.content,
            "document_fingerprint": fingerprints.document,
            "status": status,
            "status_detail": status_detail,
            "updated_at": updated_at,
        }
        records.append(record)
    return sorted(records, key=lambda value: value["document_id"])


def build_delta_records(
    *,
    sync_id: str,
    previous: Mapping[str, Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
    delete_reasons: Mapping[str, str] | None = None,
    metadata_changes_require_chunking: bool = False,
) -> list[dict[str, Any]]:
    current_by_id = {str(record["document_id"]): dict(record) for record in current}
    active_previous = {
        key: value for key, value in previous.items() if not bool(value.get("deleted"))
    }
    reasons = delete_reasons or {}
    deltas: list[dict[str, Any]] = []

    for doc_id in sorted(set(active_previous) - set(current_by_id)):
        old = active_previous[doc_id]
        deltas.append(
            {
                "schema_version": SCHEMA_VERSION,
                "sync_id": sync_id,
                "operation": "delete",
                "document_id": doc_id,
                "previous_fingerprint": old.get("document_fingerprint"),
                "current_fingerprint": None,
                "metadata_changed": False,
                "content_changed": False,
                "chunk_required": False,
                "reason": reasons.get(doc_id, "zotero_item_deleted"),
            }
        )

    for doc_id in sorted(current_by_id):
        record = current_by_id[doc_id]
        old_row = active_previous.get(doc_id)
        reason: str | None
        if old_row is None:
            reason = "new_document"
            metadata_changed = True
            content_changed = record.get("content_fingerprint") is not None
            previous_fingerprint = None
        else:
            old_manifest = _old_manifest(old_row)
            metadata_changed = old_row.get("metadata_fingerprint") != record.get(
                "metadata_fingerprint"
            )
            content_changed = old_row.get("content_fingerprint") != record.get(
                "content_fingerprint"
            )
            previous_fingerprint = old_row.get("document_fingerprint")
            reason = _change_reason(old_manifest, record, metadata_changed, content_changed)
            if reason is None:
                continue
        ready = record.get("status") == READY
        deltas.append(
            {
                "schema_version": SCHEMA_VERSION,
                "sync_id": sync_id,
                "operation": "upsert",
                "document_id": doc_id,
                "previous_fingerprint": previous_fingerprint,
                "current_fingerprint": record.get("document_fingerprint"),
                "metadata_changed": metadata_changed,
                "content_changed": content_changed,
                "chunk_required": chunk_required(
                    reason,
                    ready=ready,
                    chunk_on_metadata_change=metadata_changes_require_chunking,
                ),
                "reason": reason,
                "manifest_record": record,
            }
        )
    return sorted(deltas, key=lambda value: (value["document_id"], value["operation"]))


def collection_snapshot(
    *,
    library_type: str,
    library_id: int,
    library_version: int,
    collections: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    values = [
        {
            "key": key,
            "name": str(value.get("name") or ""),
            "path": str(value.get("path") or value.get("name") or key),
            "parent_key": value.get("parent_collection_key"),
        }
        for key, value in collections.items()
        if not bool(value.get("deleted"))
    ]
    values.sort(key=lambda value: (value["path"], value["key"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "zotero",
        "library_type": library_type,
        "library_id": str(library_id),
        "library_version": library_version,
        "collections": values,
    }


def document_id(
    library_type: str,
    library_id: int,
    parent_item_key: str,
    attachment_key: str,
    *,
    pdf_index: int,
) -> str:
    return f"zotero:{library_type}:{library_id}:{parent_item_key}:{attachment_key}:{pdf_index}"


def document_state(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "document_id": record["document_id"],
        "parent_item_key": record["item_key"],
        "attachment_key": record["attachment_key"],
        "pdf_index": 0,
        "metadata_fingerprint": record["metadata_fingerprint"],
        "content_fingerprint": record.get("content_fingerprint"),
        "document_fingerprint": record["document_fingerprint"],
        "status": record["status"],
        "deleted": False,
        "delete_reason": None,
        "manifest_json": json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "updated_at": record.get("updated_at") or "",
    }


def _change_reason(
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    metadata_changed: bool,
    content_changed: bool,
) -> str | None:
    old_status = str(old.get("status") or "")
    new_status = str(new.get("status") or "")
    if old_status != READY and new_status == READY:
        return "attachment_became_available"
    if old_status == READY and new_status == "missing_archive":
        return "attachment_missing"
    if old_status != new_status and new_status in _INVALID_STATUSES:
        return "attachment_became_invalid"
    if content_changed:
        return "content_changed"
    old_attachment_value = old.get("attachment")
    new_attachment_value = new.get("attachment")
    old_attachment: Mapping[str, Any] = (
        old_attachment_value if isinstance(old_attachment_value, Mapping) else {}
    )
    new_attachment: Mapping[str, Any] = (
        new_attachment_value if isinstance(new_attachment_value, Mapping) else {}
    )
    if old_attachment.get("archive_sha256") != new_attachment.get("archive_sha256"):
        return "attachment_replaced"
    if metadata_changed:
        if old.get("collections") != new.get("collections"):
            old_without = dict(old)
            new_without = dict(new)
            old_without.pop("collections", None)
            new_without.pop("collections", None)
            # Collection membership/path changes get their dedicated reason even
            # though they are intentionally part of metadata_fingerprint.
            return "collection_changed"
        return "metadata_changed"
    if old_status != new_status:
        return "attachment_became_invalid"
    return None


def _old_manifest(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("manifest_json")
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _raw(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("raw_json")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return dict(value) if isinstance(value, Mapping) else {}


def _data(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("data")
    return dict(value) if isinstance(value, Mapping) else dict(payload)


def _status_detail(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"message": value}
        return parsed
    return value


def _stable_updated_at(parent: Mapping[str, Any], attachment: Mapping[str, Any]) -> str:
    values = [
        str(value)
        for value in (
            parent.get("dateModified"),
            parent.get("dateAdded"),
            attachment.get("dateModified"),
            attachment.get("dateAdded"),
        )
        if value
    ]
    return max(values) if values else "1970-01-01T00:00:00Z"
