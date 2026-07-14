"""Versioned snapshot and delta manifest records."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

MANIFEST_SCHEMA_VERSION = 1
JSONMapping = Mapping[str, Any]


class DeltaOperation(str, Enum):
    UPSERT = "upsert"
    DELETE = "delete"


class DeltaReason(str, Enum):
    NEW_DOCUMENT = "new_document"
    METADATA_CHANGED = "metadata_changed"
    CONTENT_CHANGED = "content_changed"
    ATTACHMENT_REPLACED = "attachment_replaced"
    ATTACHMENT_BECAME_AVAILABLE = "attachment_became_available"
    ATTACHMENT_MISSING = "attachment_missing"
    ATTACHMENT_BECAME_INVALID = "attachment_became_invalid"
    ZOTERO_ITEM_DELETED = "zotero_item_deleted"
    ZOTERO_ATTACHMENT_DELETED = "zotero_attachment_deleted"
    COLLECTION_CHANGED = "collection_changed"


@dataclass(frozen=True, slots=True, kw_only=True)
class Creator:
    creator_type: str
    first_name: str = ""
    last_name: str = ""
    name: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"creator_type": self.creator_type}
        if self.name is not None:
            result["name"] = self.name
        else:
            result["first_name"] = self.first_name
            result["last_name"] = self.last_name
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class CollectionReference:
    key: str
    name: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "name": self.name, "path": self.path}


@dataclass(frozen=True, slots=True, kw_only=True)
class AttachmentManifest:
    backend: str = "nutstore_webdav"
    archive_path: Optional[Union[str, Path]] = None
    prop_path: Optional[Union[str, Path]] = None
    archive_sha256: Optional[str] = None
    archive_size_bytes: Optional[int] = None
    archive_mtime_ns: Optional[int] = None
    pdf_path: Optional[Union[str, Path]] = None
    pdf_sha256: Optional[str] = None
    pdf_size_bytes: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive_mtime_ns": self.archive_mtime_ns,
            "archive_path": str(self.archive_path) if self.archive_path is not None else None,
            "archive_sha256": self.archive_sha256,
            "archive_size_bytes": self.archive_size_bytes,
            "backend": self.backend,
            "pdf_path": str(self.pdf_path) if self.pdf_path is not None else None,
            "pdf_sha256": self.pdf_sha256,
            "pdf_size_bytes": self.pdf_size_bytes,
            "prop_path": str(self.prop_path) if self.prop_path is not None else None,
        }


def _creator_dict(value: Union[Creator, JSONMapping]) -> dict[str, Any]:
    return value.to_dict() if isinstance(value, Creator) else dict(value)


def _collection_dict(value: Union[CollectionReference, JSONMapping]) -> dict[str, Any]:
    return value.to_dict() if isinstance(value, CollectionReference) else dict(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class SnapshotRecord:
    """One concrete PDF (or metadata-only PDF attachment) in the current snapshot."""

    document_id: str
    library_type: str
    library_id: str
    library_version: int
    item_key: str
    item_version: int
    item_type: str
    attachment_key: str
    attachment_version: int
    title: str
    metadata_fingerprint: str
    document_fingerprint: str
    updated_at: str
    source: str = "zotero"
    schema_version: int = MANIFEST_SCHEMA_VERSION
    pdf_index: int = 0
    creators: Sequence[Union[Creator, JSONMapping]] = field(default_factory=tuple)
    abstract: str = ""
    publication_title: str = ""
    date: str = ""
    year: Optional[int] = None
    doi: str = ""
    url: str = ""
    language: str = ""
    rights: str = ""
    relations: JSONMapping = field(default_factory=dict)
    tags: Sequence[str] = field(default_factory=tuple)
    collections: Sequence[Union[CollectionReference, JSONMapping]] = field(default_factory=tuple)
    mime_type: str = "application/pdf"
    attachment: Union[AttachmentManifest, JSONMapping] = field(default_factory=AttachmentManifest)
    content_fingerprint: Optional[str] = None
    status: str = "metadata_only"
    status_detail: Optional[Any] = None

    def __post_init__(self) -> None:
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest schema version: {self.schema_version}")
        if not self.document_id:
            raise ValueError("document_id cannot be empty")
        if self.pdf_index < 0:
            raise ValueError("pdf_index cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        attachment = (
            self.attachment.to_dict()
            if isinstance(self.attachment, AttachmentManifest)
            else dict(self.attachment)
        )
        collections = sorted(
            (_collection_dict(value) for value in self.collections),
            key=lambda value: (str(value.get("path", "")), str(value.get("key", ""))),
        )
        return {
            "abstract": self.abstract,
            "attachment": attachment,
            "attachment_key": self.attachment_key,
            "attachment_version": self.attachment_version,
            "collections": collections,
            "content_fingerprint": self.content_fingerprint,
            "creators": [_creator_dict(value) for value in self.creators],
            "date": self.date,
            "document_fingerprint": self.document_fingerprint,
            "document_id": self.document_id,
            "doi": self.doi,
            "item_key": self.item_key,
            "item_type": self.item_type,
            "item_version": self.item_version,
            "language": self.language,
            "library_id": self.library_id,
            "library_type": self.library_type,
            "library_version": self.library_version,
            "metadata_fingerprint": self.metadata_fingerprint,
            "mime_type": self.mime_type,
            "pdf_index": self.pdf_index,
            "publication_title": self.publication_title,
            "relations": dict(self.relations),
            "rights": self.rights,
            "schema_version": self.schema_version,
            "source": self.source,
            "status": self.status,
            "status_detail": self.status_detail,
            "tags": sorted(set(self.tags)),
            "title": self.title,
            "updated_at": self.updated_at,
            "url": self.url,
            "year": self.year,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DeltaRecord:
    """An explicit downstream operation for one changed document."""

    sync_id: str
    operation: Union[DeltaOperation, str]
    document_id: str
    previous_fingerprint: Optional[str]
    current_fingerprint: Optional[str]
    metadata_changed: bool
    content_changed: bool
    chunk_required: bool
    reason: Union[DeltaReason, str]
    manifest_record: Optional[Union[SnapshotRecord, JSONMapping]] = None
    schema_version: int = MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest schema version: {self.schema_version}")
        if not self.sync_id or not self.document_id:
            raise ValueError("sync_id and document_id cannot be empty")
        operation = (
            self.operation.value if isinstance(self.operation, DeltaOperation) else self.operation
        )
        if operation not in {value.value for value in DeltaOperation}:
            raise ValueError(f"unsupported delta operation: {operation}")
        if operation == DeltaOperation.DELETE.value and self.current_fingerprint is not None:
            raise ValueError("delete operations must have a null current_fingerprint")
        if operation == DeltaOperation.UPSERT.value and self.manifest_record is None:
            raise ValueError("upsert operations require manifest_record")

    def to_dict(self) -> dict[str, Any]:
        operation = (
            self.operation.value if isinstance(self.operation, DeltaOperation) else self.operation
        )
        reason = self.reason.value if isinstance(self.reason, DeltaReason) else self.reason
        result: dict[str, Any] = {
            "chunk_required": self.chunk_required,
            "content_changed": self.content_changed,
            "current_fingerprint": self.current_fingerprint,
            "document_id": self.document_id,
            "metadata_changed": self.metadata_changed,
            "operation": operation,
            "previous_fingerprint": self.previous_fingerprint,
            "reason": reason,
            "schema_version": self.schema_version,
            "sync_id": self.sync_id,
        }
        if self.manifest_record is not None:
            result["manifest_record"] = (
                self.manifest_record.to_dict()
                if isinstance(self.manifest_record, SnapshotRecord)
                else dict(self.manifest_record)
            )
        return result
