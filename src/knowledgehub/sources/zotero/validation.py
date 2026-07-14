"""Integrity validation and diagnostics for a synchronized Zotero source."""

from __future__ import annotations

import json
import re
import sqlite3
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from knowledgehub.core.atomic import ensure_within
from knowledgehub.core.hashing import sha256_file

from .collections import build_collection_paths
from .config import ZoteroConfig
from .fingerprints import document_fingerprint, metadata_fingerprint
from .models import ZoteroError
from .state import SCHEMA_VERSION, ZoteroStateStore

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_REQUIRED = {
    "schema_version",
    "document_id",
    "source",
    "library_type",
    "library_id",
    "library_version",
    "item_key",
    "attachment_key",
    "pdf_index",
    "metadata_fingerprint",
    "content_fingerprint",
    "document_fingerprint",
    "status",
    "attachment",
}
_DELTA_REQUIRED = {
    "schema_version",
    "sync_id",
    "operation",
    "document_id",
    "previous_fingerprint",
    "current_fingerprint",
    "metadata_changed",
    "content_changed",
    "chunk_required",
    "reason",
}


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    suggestion: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    valid: bool = True
    issues: list[ValidationIssue] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def add(
        self,
        severity: str,
        code: str,
        message: str,
        suggestion: str,
        **context: Any,
    ) -> None:
        self.issues.append(ValidationIssue(severity, code, message, suggestion, context))
        if severity == "error":
            self.valid = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "checks": self.checks,
            "issues": [asdict(value) for value in self.issues],
        }


def validate_source(
    config: ZoteroConfig, *, ignore_publish_intent: Path | None = None
) -> ValidationReport:
    report = ValidationReport()
    store = ZoteroStateStore(config.data_dir)
    if not store.path.exists() and not store.path.is_symlink():
        report.add(
            "error",
            "missing_state",
            "State database does not exist",
            "Run `knowledgehub zotero sync --full`.",
        )
        return report
    try:
        with store.connect_readonly() as connection:
            quick = store.quick_check(connection)
            report.checks["sqlite_quick_check"] = quick
            if quick != ["ok"]:
                report.add(
                    "error",
                    "sqlite_corrupt",
                    "; ".join(quick),
                    "Run a dry-run rebuild, then rebuild with --yes.",
                )
            schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
            state = store.library_state(connection)
            objects = store.load_objects("item", connection=connection)
            collections = store.load_collections(connection=connection)
            attachments = store.load_attachments(connection)
            documents = store.load_documents(include_deleted=True, connection=connection)
            mapping = store.mapping_validation(connection)
        report.checks["schema_version"] = schema
        if schema != SCHEMA_VERSION:
            report.add(
                "error",
                "schema_version",
                f"Expected schema {SCHEMA_VERSION}, found {schema}",
                "Upgrade KnowledgeHub or rebuild state.",
            )
    except (json.JSONDecodeError, TypeError, UnicodeError) as exc:
        report.add(
            "error",
            "invalid_state_data",
            f"State database contains invalid structured data: {exc}",
            "Run a dry-run rebuild; preserve the old database for audit.",
        )
        return report
    except (sqlite3.DatabaseError, OSError, ZoteroError) as exc:
        report.add(
            "error",
            "sqlite_error",
            str(exc),
            "Run a dry-run rebuild; preserve the old database for audit.",
        )
        return report

    if state is None:
        report.add("error", "missing_library_state", "library_state is empty", "Run a full sync.")
        return report
    report.checks["library"] = {
        "type": state["library_type"],
        "id": state["library_id"],
        "version": state["library_version"],
    }
    if state["library_type"] != config.library_type or (
        config.library_id is not None and int(state["library_id"]) != config.library_id
    ):
        report.add(
            "error",
            "library_mismatch",
            "Configured library differs from state database",
            "Use a separate ZOTERO_DATA_DIR per library.",
        )

    _check_relations(report, objects, attachments)
    _check_collections(report, collections)
    _check_collection_manifest(report, config, collections, state)
    _check_attachments(report, config, attachments)
    manifest_records = _check_snapshot(report, config, documents)
    _check_deltas(report, config, documents)

    db_current = sorted(key for key, value in documents.items() if not value.get("deleted"))
    manifest_current = sorted(manifest_records)
    report.checks["current_document_count"] = len(db_current)
    if db_current != manifest_current:
        report.add(
            "error",
            "document_set_mismatch",
            "SQLite and snapshot current document sets differ",
            "Run `knowledgehub zotero resolve-attachments`, then validate again.",
        )

    ignored = ignore_publish_intent.resolve(strict=False) if ignore_publish_intent else None
    intents = sorted(
        str(path)
        for path in (config.data_dir / "runs").glob("*/publish-intent.json")
        if path.resolve(strict=False) != ignored
    )
    report.checks["pending_publish_intents"] = intents
    if intents:
        report.add(
            "error",
            "pending_publication",
            "A manifest publication needs recovery",
            "Run `knowledgehub zotero sync --once` or `resolve-attachments` to recover it.",
            intents=intents,
        )

    report.checks["mapping_validation"] = mapping or {}
    try:
        mapping_matches = bool(
            mapping
            and mapping.get("library_type") == state.get("library_type")
            and int(mapping.get("library_id") or 0) == int(state.get("library_id") or 0)
            and mapping.get("webdav_realpath") == str(config.webdav_dir.resolve(strict=True))
        )
    except (OSError, TypeError, ValueError):
        mapping_matches = False
    if mapping and mapping.get("status") == "verified" and not mapping_matches:
        report.add(
            "error",
            "stale_mapping_validation",
            "Saved attachment mapping belongs to a different library or WebDAV root",
            "Run an attachment rescan to revalidate the mapping.",
        )
    elif mapping and mapping.get("status") != "verified" and attachments:
        report.add(
            "warning",
            "mapping_unverified",
            "Attachment-key mapping is not verified",
            "Ensure matching <attachment_key>.zip and .prop files exist, then rescan.",
        )
    return report


def _check_relations(
    report: ValidationReport,
    objects: Mapping[str, Mapping[str, Any]],
    attachments: Mapping[str, Mapping[str, Any]],
) -> None:
    missing_by_key: dict[str, dict[str, str]] = {}
    # The derived attachments table deliberately omits orphan children, so the
    # authoritative raw item mirror is the only complete place to validate this
    # relation (including non-PDF attachments).
    for key, row in objects.items():
        try:
            payload = json.loads(str(row.get("raw_json") or "{}"))
        except json.JSONDecodeError:
            continue
        data_value = payload.get("data") if isinstance(payload, dict) else None
        data = data_value if isinstance(data_value, dict) else payload
        if not isinstance(data, dict) or data.get("itemType") != "attachment":
            continue
        parent = str(data.get("parentItem") or "")
        if not parent or parent not in objects:
            missing_by_key[key] = {
                "attachment_key": key,
                "parent_item_key": parent,
            }
    for key, attachment in attachments.items():
        parent = str(attachment.get("parent_item_key") or "")
        if parent and parent not in objects:
            missing_by_key[key] = {"attachment_key": key, "parent_item_key": parent}
    missing = [missing_by_key[key] for key in sorted(missing_by_key)]
    report.checks["missing_parent_relations"] = missing
    for value in missing:
        report.add(
            "error",
            "missing_parent",
            f"Attachment {value['attachment_key']} has no parent {value['parent_item_key']}",
            "Run a full metadata sync.",
            **value,
        )


def _check_collections(
    report: ValidationReport, collections: Mapping[str, Mapping[str, Any]]
) -> None:
    values = [
        {
            "key": key,
            "data": {
                "key": key,
                "name": value.get("name") or "",
                "parentCollection": value.get("parent_collection_key"),
            },
        }
        for key, value in collections.items()
    ]
    result = build_collection_paths(values)
    report.checks["collection_errors"] = [asdict(value) for value in result.errors]
    for error in result.errors:
        report.add(
            "error",
            error.code,
            error.detail,
            "Repair the collection hierarchy in Zotero, then sync again.",
            collection_key=error.collection_key,
        )
    for key, expected in result.by_key.items():
        if collections[key].get("path") != expected:
            report.add(
                "error",
                "collection_path_mismatch",
                f"Collection {key} path is not reproducible",
                "Run a full sync.",
                collection_key=key,
            )


def _check_collection_manifest(
    report: ValidationReport,
    config: ZoteroConfig,
    collections: Mapping[str, Mapping[str, Any]],
    state: Mapping[str, Any],
) -> None:
    path = config.data_dir / "manifests" / "collections.json"
    if not path.is_file():
        if state.get("active_sync_id") is not None:
            report.add(
                "error",
                "missing_collection_manifest",
                "Collection manifest is missing",
                "Run a sync.",
            )
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        report.add(
            "error",
            "invalid_collection_manifest",
            f"Collection manifest is not valid JSON: {exc}",
            "Run a sync.",
        )
        return
    values = payload.get("collections") if isinstance(payload, dict) else None
    valid_envelope = bool(
        isinstance(payload, dict)
        and payload.get("schema_version") == 1
        and payload.get("source") == "zotero"
        and payload.get("library_type") == state.get("library_type")
        and str(payload.get("library_id")) == str(state.get("library_id"))
        and payload.get("library_version") == state.get("library_version")
        and isinstance(values, list)
        and all(isinstance(value, dict) for value in values)
    )
    if not valid_envelope:
        report.add(
            "error",
            "invalid_collection_manifest",
            "Collection manifest does not satisfy schema v1",
            "Run a sync.",
        )
        return
    assert isinstance(values, list)
    keys = [str(value.get("key") or "") for value in values]
    order = [(str(value.get("path") or ""), str(value.get("key") or "")) for value in values]
    if (
        any(not key for key in keys)
        or len(keys) != len(set(keys))
        or order != sorted(order)
        or sorted(keys) != sorted(collections)
    ):
        report.add(
            "error",
            "collection_manifest_mismatch",
            "Collection manifest is unsorted, duplicated, or differs from SQLite",
            "Run a full sync.",
        )


def _check_attachments(
    report: ValidationReport,
    config: ZoteroConfig,
    attachments: Mapping[str, Mapping[str, Any]],
) -> None:
    ready = 0
    for key, value in attachments.items():
        archive_path = value.get("archive_path")
        if archive_path:
            try:
                archive = Path(str(archive_path))
                archive.resolve(strict=False).relative_to(config.webdav_dir.resolve(strict=True))
                mode = archive.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    raise ValueError("not a regular non-symlink file")
            except (OSError, ValueError):
                report.add(
                    "error",
                    "unsafe_archive_path",
                    f"Attachment {key} archive provenance is unsafe",
                    "Restore the expected read-only WebDAV layout.",
                    attachment_key=key,
                )
        if value.get("resolver_status") != "ready":
            continue
        ready += 1
        pdf_path = value.get("pdf_path")
        expected_hash = value.get("pdf_sha256")
        if not pdf_path or not expected_hash:
            report.add(
                "error",
                "ready_without_pdf",
                f"Ready attachment {key} lacks a PDF path/hash",
                "Rescan attachments.",
                attachment_key=key,
            )
            continue
        try:
            lexical_pdf = Path(str(pdf_path))
            if lexical_pdf.is_symlink():
                raise ValueError("PDF cache path is a symlink")
            pdf = ensure_within(lexical_pdf, config.data_dir)
            if not pdf.is_file():
                raise FileNotFoundError(str(lexical_pdf))
            actual = sha256_file(pdf)
            if actual != expected_hash:
                report.add(
                    "error",
                    "pdf_hash_mismatch",
                    f"PDF hash mismatch for {key}",
                    "Rescan attachments; the cache will be rebuilt.",
                    attachment_key=key,
                )
        except (OSError, ValueError) as exc:
            report.add(
                "error",
                "missing_pdf",
                f"Ready attachment {key} PDF is unavailable: {exc}",
                "Rescan attachments.",
                attachment_key=key,
            )
    report.checks["ready_attachments"] = ready


def _check_snapshot(
    report: ValidationReport,
    config: ZoteroConfig,
    documents: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    path = config.data_dir / "manifests" / "documents.jsonl"
    if not path.is_file():
        report.add("error", "missing_snapshot", "Snapshot manifest is missing", "Run a sync.")
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        report.add(
            "error",
            "invalid_snapshot_file",
            f"Snapshot manifest cannot be read: {exc}",
            "Regenerate manifests with a sync.",
        )
        return {}
    records: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line_number, line in enumerate(lines, 1):
        try:
            value = json.loads(line)
            doc_id = str(value["document_id"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            report.add(
                "error",
                "invalid_snapshot_json",
                f"Invalid snapshot line {line_number}: {exc}",
                "Regenerate manifests with a sync.",
            )
            continue
        if doc_id in records:
            report.add(
                "error",
                "duplicate_document_id",
                f"Duplicate document_id {doc_id}",
                "Regenerate manifests.",
            )
        records[doc_id] = value
        order.append(doc_id)
        _check_snapshot_shape(report, value, line_number)
        _check_record_fingerprint(report, value)
        row = documents.get(doc_id)
        if row and row.get("manifest_json") != json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ):
            report.add(
                "error",
                "manifest_db_mismatch",
                f"Snapshot record {doc_id} differs from SQLite",
                "Run a sync.",
                document_id=doc_id,
            )
    if order != sorted(order):
        report.add(
            "error",
            "snapshot_order",
            "Snapshot is not sorted by document_id",
            "Regenerate manifests.",
        )
    return records


def _check_record_fingerprint(report: ValidationReport, record: Mapping[str, Any]) -> None:
    creators = []
    raw_creators = record.get("creators")
    creator_values = raw_creators if isinstance(raw_creators, list) else []
    for value in creator_values:
        if not isinstance(value, Mapping):
            continue
        creators.append(
            {
                "creatorType": value.get("creator_type"),
                "firstName": value.get("first_name"),
                "lastName": value.get("last_name"),
                "name": value.get("name"),
            }
        )
    metadata = {
        "title": record.get("title"),
        "creators": creators,
        "abstractNote": record.get("abstract"),
        "publicationTitle": record.get("publication_title"),
        "date": record.get("date"),
        "DOI": record.get("doi"),
        "url": record.get("url"),
        "language": record.get("language"),
        "rights": record.get("rights"),
        "itemType": record.get("item_type"),
        "relations": record.get("relations"),
        "tags": record.get("tags"),
        "collection_refs": record.get("collections"),
    }
    try:
        metadata_hash = metadata_fingerprint(metadata)
        content_hash = record.get("content_fingerprint")
        document_hash = document_fingerprint(metadata_hash, content_hash, str(record.get("status")))
    except (AttributeError, TypeError, ValueError):
        report.add(
            "error",
            "fingerprint_mismatch",
            f"Fingerprint inputs are invalid for {record.get('document_id')}",
            "Regenerate manifests and inspect metadata normalization.",
            document_id=record.get("document_id"),
        )
        return
    if metadata_hash != record.get("metadata_fingerprint") or document_hash != record.get(
        "document_fingerprint"
    ):
        report.add(
            "error",
            "fingerprint_mismatch",
            f"Fingerprint mismatch for {record.get('document_id')}",
            "Regenerate manifests and inspect metadata normalization.",
            document_id=record.get("document_id"),
        )


def _check_snapshot_shape(
    report: ValidationReport, record: Mapping[str, Any], line_number: int
) -> None:
    missing = sorted(_SNAPSHOT_REQUIRED - set(record))
    valid = (
        not missing
        and record.get("schema_version") == 1
        and record.get("source") == "zotero"
        and record.get("pdf_index") == 0
        and isinstance(record.get("document_id"), str)
        and isinstance(record.get("library_version"), int)
        and isinstance(record.get("attachment"), Mapping)
        and isinstance(record.get("status"), str)
        and isinstance(record.get("metadata_fingerprint"), str)
        and bool(_SHA256.fullmatch(str(record.get("metadata_fingerprint"))))
        and isinstance(record.get("document_fingerprint"), str)
        and bool(_SHA256.fullmatch(str(record.get("document_fingerprint"))))
        and (
            record.get("content_fingerprint") is None
            or (
                isinstance(record.get("content_fingerprint"), str)
                and bool(_SHA256.fullmatch(str(record.get("content_fingerprint"))))
            )
        )
    )
    if not valid:
        report.add(
            "error",
            "invalid_snapshot_schema",
            f"Snapshot line {line_number} does not satisfy manifest schema v1",
            "Regenerate manifests with a sync.",
            missing_fields=missing,
        )


def _check_deltas(
    report: ValidationReport,
    config: ZoteroConfig,
    documents: Mapping[str, Mapping[str, Any]],
) -> None:
    known = set(documents)
    count = 0
    for path in sorted((config.data_dir / "manifests" / "deltas").glob("*.jsonl")):
        order: list[str] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            report.add(
                "error",
                "invalid_delta_file",
                f"Delta {path.name} cannot be read: {exc}",
                "Regenerate the affected sync delta.",
            )
            continue
        for line_number, line in enumerate(lines, 1):
            count += 1
            try:
                value = json.loads(line)
                doc_id = str(value["document_id"])
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                report.add(
                    "error",
                    "invalid_delta_json",
                    f"Invalid {path.name}:{line_number}: {exc}",
                    "Regenerate the affected sync delta.",
                )
                continue
            order.append(doc_id)
            _check_delta_shape(report, value, path.name, line_number)
            if doc_id not in known:
                report.add(
                    "error",
                    "unknown_delta_document",
                    f"Delta references unknown document {doc_id}",
                    "Restore the matching state database or rebuild.",
                )
        if order != sorted(order) or len(order) != len(set(order)):
            report.add(
                "error",
                "delta_order",
                f"Delta {path.name} is unsorted or contains duplicates",
                "Regenerate manifests.",
            )
    report.checks["delta_records"] = count


def _check_delta_shape(
    report: ValidationReport,
    record: Mapping[str, Any],
    filename: str,
    line_number: int,
) -> None:
    missing = sorted(_DELTA_REQUIRED - set(record))
    operation = record.get("operation")
    current = record.get("current_fingerprint")
    manifest_record = record.get("manifest_record")
    expected_sync_id = filename.removesuffix(".jsonl")
    valid = (
        not missing
        and record.get("schema_version") == 1
        and operation in {"upsert", "delete"}
        and record.get("sync_id") == expected_sync_id
        and isinstance(record.get("document_id"), str)
        and isinstance(record.get("reason"), str)
        and all(
            isinstance(record.get(field), bool)
            for field in ("metadata_changed", "content_changed", "chunk_required")
        )
        and (
            record.get("previous_fingerprint") is None
            or (
                isinstance(record.get("previous_fingerprint"), str)
                and bool(_SHA256.fullmatch(str(record.get("previous_fingerprint"))))
            )
        )
        and (current is None or (isinstance(current, str) and bool(_SHA256.fullmatch(current))))
        and (
            (
                operation == "delete"
                and current is None
                and record.get("previous_fingerprint") is not None
                and "manifest_record" not in record
            )
            or (
                operation == "upsert"
                and current is not None
                and isinstance(manifest_record, Mapping)
                and manifest_record.get("schema_version") == 1
                and manifest_record.get("document_id") == record.get("document_id")
                and manifest_record.get("document_fingerprint") == current
            )
        )
    )
    if not valid:
        report.add(
            "error",
            "invalid_delta_schema",
            f"Delta {filename}:{line_number} does not satisfy manifest schema v1",
            "Regenerate the affected sync delta.",
            missing_fields=missing,
        )
