"""Transactional orchestration for Zotero metadata and WebDAV attachments."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from knowledgehub.core.atomic import (
    atomic_write_json,
    fsync_directory,
    safe_remove,
)
from knowledgehub.core.locking import FileLock
from knowledgehub.manifests.catalog import append_delta_catalog
from knowledgehub.manifests.writer import write_delta, write_json, write_snapshot

from .attachments import (
    AttachmentCacheState,
    AttachmentRequest,
    AttachmentResolver,
    AttachmentStatus,
    locate_archive,
    validate_attachment_mapping,
)
from .client import ZoteroClient, assert_target_versions
from .collections import build_collection_paths
from .config import ZoteroConfig
from .manifest import (
    build_delta_records,
    build_snapshot_records,
    collection_snapshot,
    document_state,
)
from .models import (
    RemoteVersionChanged,
    RuntimeDependencies,
    SyncMode,
    SyncSummary,
    ZoteroError,
)
from .state import ZoteroStateStore, utc_now

LOGGER = logging.getLogger(__name__)
_SUPPORTED_LINK_MODES = {"imported_file", "imported_url"}
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class _RemoteChanges:
    target_version: int
    not_modified: bool
    objects: dict[str, list[dict[str, Any]]]
    deleted: dict[str, list[str]]
    unchanged: int


def sync_once(
    config: ZoteroConfig,
    *,
    mode: SyncMode = SyncMode.INCREMENTAL,
    dependencies: RuntimeDependencies | None = None,
) -> SyncSummary:
    """Synchronize one coherent remote library version and publish manifests."""

    if mode is SyncMode.ATTACHMENTS:
        return resolve_attachments_once(config, dependencies=dependencies)
    return _run(config, mode=mode, dependencies=dependencies, remote=True)


def resolve_attachments_once(
    config: ZoteroConfig,
    *,
    dependencies: RuntimeDependencies | None = None,
) -> SyncSummary:
    """Rescan local archives without contacting Zotero or changing library_version."""

    return _run(config, mode=SyncMode.ATTACHMENTS, dependencies=dependencies, remote=False)


def _run(
    config: ZoteroConfig,
    *,
    mode: SyncMode,
    dependencies: RuntimeDependencies | None,
    remote: bool,
) -> SyncSummary:
    deps = dependencies or RuntimeDependencies()
    sleeper = deps.sleeper or time.sleep
    monotonic = deps.monotonic or time.monotonic
    random_fn = deps.random or random.random
    started = monotonic()
    sync_id = _new_sync_id()
    config.validate_static()
    config.prepare_runtime()
    store = ZoteroStateStore(config.data_dir)
    store.initialize()

    with FileLock(
        config.data_dir / "state" / "zotero.lock",
        sync_id=sync_id,
        clock=monotonic,
        sleeper=sleeper,
    ):
        recover_publications(config.data_dir, store)
        initial_state = store.library_state()
        from_version = int(initial_state["library_version"]) if initial_state else 0
        summary = SyncSummary(
            sync_id=sync_id,
            mode=mode.value,
            status="running",
            from_version=from_version,
        )
        store.start_run(summary)
        try:
            if remote:
                with ZoteroClient(
                    config,
                    transport=deps.http_transport,
                    sleeper=sleeper,
                    monotonic=monotonic,
                    random_fn=random_fn,
                ) as verification_client:
                    access = verification_client.verify_key()
                if config.library_id is None:
                    config = config.with_library_id(access.library_id)
                store.bind_library(config.library_type, access.library_id)
            else:
                state = store.library_state()
                if state is None:
                    raise ZoteroError(
                        "state_error",
                        "Cannot rescan attachments before a successful metadata sync",
                    )
                if state["library_type"] != config.library_type or (
                    config.library_id is not None and int(state["library_id"]) != config.library_id
                ):
                    raise ZoteroError(
                        "library_binding_mismatch", "Configured library does not match local state"
                    )
                config = config.with_library_id(int(state["library_id"]))

            state = store.library_state()
            if state is None:
                raise ZoteroError("state_error", "Library state was not initialized")
            if config.library_id is None:
                raise ZoteroError("state_error", "Library ID was not initialized")
            from_version = int(state["library_version"])
            summary.from_version = from_version
        except BaseException as exc:
            _record_failed_run(
                config.data_dir,
                store,
                summary,
                exc,
                duration=round(max(0.0, monotonic() - started), 6),
            )
            raise
        try:
            attempts = config.sync_max_retries + 1 if remote else 1
            last_drift: RemoteVersionChanged | None = None
            for attempt in range(attempts):
                _reset_attempt_summary(summary)
                try:
                    if remote:
                        with ZoteroClient(
                            config,
                            transport=deps.http_transport,
                            sleeper=sleeper,
                            monotonic=monotonic,
                            random_fn=random_fn,
                        ) as client:
                            changes = _fetch_remote_changes(
                                client,
                                store,
                                mode=mode,
                                from_version=from_version,
                            )
                            _apply_and_publish(
                                config,
                                store,
                                summary,
                                changes,
                                client=client,
                                sleeper=sleeper,
                                elapsed=lambda: max(0.0, monotonic() - started),
                            )
                    else:
                        changes = _RemoteChanges(
                            target_version=from_version,
                            not_modified=True,
                            objects={"item": [], "collection": [], "search": []},
                            deleted={"items": [], "collections": [], "searches": [], "tags": []},
                            unchanged=0,
                        )
                        _apply_and_publish(
                            config,
                            store,
                            summary,
                            changes,
                            client=None,
                            sleeper=sleeper,
                            elapsed=lambda: max(0.0, monotonic() - started),
                        )
                    return summary
                except RemoteVersionChanged as exc:
                    last_drift = exc
                    LOGGER.warning(
                        "remote library changed during sync",
                        extra={"sync_id": sync_id, "retry_count": attempt, "retryable": True},
                    )
                    if attempt + 1 >= attempts:
                        raise
                    sleeper(min(60.0, 2**attempt + random_fn()))
            if last_drift is not None:
                raise last_drift
            raise ZoteroError("sync_error", "Synchronization did not execute")
        except BaseException as exc:
            _record_failed_run(
                config.data_dir,
                store,
                summary,
                exc,
                duration=round(max(0.0, monotonic() - started), 6),
            )
            raise


def _record_failed_run(
    data_dir: Path,
    store: ZoteroStateStore,
    summary: SyncSummary,
    error: BaseException,
    *,
    duration: float,
) -> None:
    summary.status = "failed"
    summary.duration_seconds = duration
    summary.error_code = error.code if isinstance(error, ZoteroError) else "unexpected_error"
    summary.error_message = str(error)
    try:
        store.finish_run(summary)
        _write_run_summary(data_dir, summary)
    except BaseException:
        LOGGER.exception(
            "failed to persist failed sync summary", extra={"sync_id": summary.sync_id}
        )


def _fetch_remote_changes(
    client: ZoteroClient,
    store: ZoteroStateStore,
    *,
    mode: SyncMode,
    from_version: int,
) -> _RemoteChanges:
    since = 0 if mode is SyncMode.FULL else from_version
    conditional = from_version if mode is SyncMode.INCREMENTAL and from_version > 0 else None
    collections = client.versions("collection", since=since, conditional_version=conditional)
    if collections.not_modified:
        return _RemoteChanges(
            target_version=from_version,
            not_modified=True,
            objects={"item": [], "collection": [], "search": []},
            deleted={"items": [], "collections": [], "searches": [], "tags": []},
            unchanged=0,
        )
    target = collections.library_version
    searches = client.versions("search", since=since)
    items = client.versions("item", since=since)
    assert_target_versions(target, {searches.library_version, items.library_version})

    local_items = store.load_objects("item", include_deleted=True)
    local_searches = store.load_objects("search", include_deleted=True)
    local_collections = store.load_collections(include_deleted=True)
    version_maps = {
        "item": items.versions,
        "search": searches.versions,
        "collection": collections.versions,
    }
    local_maps = {
        "item": local_items,
        "search": local_searches,
        "collection": local_collections,
    }
    fetched: dict[str, list[dict[str, Any]]] = {}
    observed: set[int] = set()
    unchanged = 0
    for kind in ("collection", "search", "item"):
        keys: list[str] = []
        for key, version in version_maps[kind].items():
            local = local_maps[kind].get(key)
            local_version_key = "collection_version" if kind == "collection" else "object_version"
            if (
                mode is SyncMode.FULL
                or local is None
                or int(local.get(local_version_key) or -1) != version
                or local.get("deleted")
            ):
                keys.append(key)
            else:
                unchanged += 1
        values, response_versions = client.fetch_objects(
            kind, {key: version_maps[kind][key] for key in keys}
        )
        fetched[kind] = values
        observed.update(response_versions)
    assert_target_versions(target, observed)
    deleted, deleted_version = client.deleted(since=since)
    assert_target_versions(target, {deleted_version})
    return _RemoteChanges(
        target_version=target,
        not_modified=False,
        objects=fetched,
        deleted=deleted,
        unchanged=unchanged,
    )


def _apply_and_publish(
    config: ZoteroConfig,
    store: ZoteroStateStore,
    summary: SyncSummary,
    changes: _RemoteChanges,
    *,
    client: ZoteroClient | None,
    sleeper: Callable[[float], None],
    elapsed: Callable[[], float],
) -> None:
    summary.target_version = changes.target_version
    summary.unchanged = changes.unchanged
    publication: _PublicationSession | None = None
    database_committed = False
    try:
        with store.transaction() as connection:
            previous_documents = store.load_documents(include_deleted=True, connection=connection)
            delete_reasons = _apply_remote_state(store, connection, summary, changes)
            paths = _rebuild_collection_paths(store, connection)
            if summary.mode == SyncMode.ATTACHMENTS.value or not changes.not_modified:
                attachment_scan_policy = "all"
            elif config.attachment_scan_on_304:
                attachment_scan_policy = "changed"
            else:
                attachment_scan_policy = "none"
            attachment_rows, attachment_publications = _resolve_attachments(
                config,
                store,
                connection,
                summary,
                sleeper=sleeper,
                scan_policy=attachment_scan_policy,
            )
            if client is not None:
                client.ensure_target_unchanged(changes.target_version)

            objects = store.load_objects("item", connection=connection)
            collections = store.load_collections(connection=connection)
            records = build_snapshot_records(
                library_type=config.library_type,
                library_id=int(config.library_id or 0),
                library_version=changes.target_version,
                objects=objects,
                collections=collections,
                attachments=attachment_rows,
            )
            deltas = build_delta_records(
                sync_id=summary.sync_id,
                previous=previous_documents,
                current=records,
                delete_reasons=delete_reasons,
                metadata_changes_require_chunking=config.metadata_changes_require_chunking,
            )
            summary.delta_upserts = sum(value["operation"] == "upsert" for value in deltas)
            summary.delta_deletes = sum(value["operation"] == "delete" for value in deltas)
            rewrite_snapshot = not (
                config.data_dir / "manifests" / "documents.jsonl"
            ).exists() or _snapshot_records_changed(previous_documents, records)
            rewrite_collections = (
                not changes.not_modified
                or not (config.data_dir / "manifests" / "collections.json").exists()
            )

            current_ids = {str(record["document_id"]) for record in records}
            for old_id, old in previous_documents.items():
                if not old.get("deleted") and old_id not in current_ids:
                    reason = delete_reasons.get(old_id, "zotero_item_deleted")
                    store.mark_document_deleted(connection, old_id, reason)
            for record in records:
                store.upsert_document(connection, document_state(record))

            summary.status = "success"
            summary.committed_version = changes.target_version
            summary.duration_seconds = round(elapsed(), 6)
            summary.details["collection_validation_errors"] = [
                asdict(value) for value in paths.errors
            ]
            summary.details["metadata_not_modified"] = changes.not_modified
            summary.details["document_count"] = len(records)
            collections_payload = collection_snapshot(
                library_type=config.library_type,
                library_id=int(config.library_id or 0),
                library_version=changes.target_version,
                collections=collections,
            )
            publication = _PublicationSession.prepare(
                data_dir=config.data_dir,
                sync_id=summary.sync_id,
                records=records,
                deltas=deltas,
                collections=collections_payload,
                summary=summary.to_dict(),
                rewrite_snapshot=rewrite_snapshot,
                rewrite_collections=rewrite_collections,
                pre_staged_entries=attachment_publications,
            )
            publication.publish()
            # This is deliberately the final state mutation in the transaction.
            store.set_success_version(
                connection, version=changes.target_version, sync_id=summary.sync_id
            )
            store.finish_run_in_transaction(connection, summary)
        database_committed = True
    except BaseException:
        if publication is not None and not database_committed:
            try:
                committed_state = store.library_state()
                database_committed = bool(
                    committed_state and committed_state.get("active_sync_id") == summary.sync_id
                )
            except BaseException:
                database_committed = False
            if not database_committed:
                publication.rollback()
        elif publication is None:
            staging = config.data_dir / ".staging" / summary.sync_id
            if staging.exists():
                safe_remove(staging, root=config.data_dir)
        if not database_committed:
            raise
        LOGGER.warning(
            "database commit succeeded despite a late transaction error",
            extra={"sync_id": summary.sync_id},
        )
    if publication is not None:
        try:
            publication.commit()
        except BaseException:
            LOGGER.exception(
                "publication cleanup deferred to startup recovery",
                extra={"sync_id": summary.sync_id},
            )


def _reset_attempt_summary(summary: SyncSummary) -> None:
    """Discard candidate-only values before a whole-library retry."""

    summary.status = "running"
    summary.target_version = None
    summary.committed_version = None
    summary.added = 0
    summary.updated = 0
    summary.deleted = 0
    summary.unchanged = 0
    summary.attachments_ready = 0
    summary.attachments_missing = 0
    summary.attachments_unstable = 0
    summary.attachments_error = 0
    summary.delta_upserts = 0
    summary.delta_deletes = 0
    summary.duration_seconds = 0.0
    summary.error_code = None
    summary.error_message = None
    summary.details.clear()


def _snapshot_records_changed(
    previous: Mapping[str, Mapping[str, Any]], records: Sequence[Mapping[str, Any]]
) -> bool:
    old_records: list[dict[str, Any]] = []
    for document_id in sorted(previous):
        row = previous[document_id]
        if row.get("deleted"):
            continue
        raw = row.get("manifest_json")
        if not isinstance(raw, str):
            return True
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return True
        if not isinstance(parsed, dict):
            return True
        old_records.append(parsed)
    return old_records != [dict(record) for record in records]


def _apply_remote_state(
    store: ZoteroStateStore,
    connection: sqlite3.Connection,
    summary: SyncSummary,
    changes: _RemoteChanges,
) -> dict[str, str]:
    if changes.not_modified:
        return {}
    for payload in changes.objects.get("collection", []):
        key = str(payload.get("key") or payload.get("data", {}).get("key") or "")
        existed = key in store.load_collections(include_deleted=True, connection=connection)
        _key, changed = store.upsert_collection(connection, payload)
        if changed:
            summary.updated += int(existed)
            summary.added += int(not existed)
    for kind in ("search", "item"):
        existing = store.load_objects(kind, include_deleted=True, connection=connection)
        for payload in changes.objects.get(kind, []):
            key = str(payload.get("key") or payload.get("data", {}).get("key") or "")
            existed = key in existing
            _key, changed = store.upsert_remote_object(connection, kind, payload)
            if changed:
                summary.updated += int(existed)
                summary.added += int(not existed)

    previous_documents = store.load_documents(include_deleted=False, connection=connection)
    deleted_items = set(changes.deleted.get("items", []))
    delete_reasons: dict[str, str] = {}
    for doc_id, document in previous_documents.items():
        if document.get("parent_item_key") in deleted_items:
            delete_reasons[doc_id] = "zotero_item_deleted"
        elif document.get("attachment_key") in deleted_items:
            delete_reasons[doc_id] = "zotero_attachment_deleted"
    for key in sorted(deleted_items):
        if store.mark_deleted(connection, "item", key, sync_id=summary.sync_id):
            summary.deleted += 1
    for key in changes.deleted.get("collections", []):
        if store.mark_deleted(connection, "collection", key, sync_id=summary.sync_id):
            summary.deleted += 1
    for key in changes.deleted.get("searches", []):
        if store.mark_deleted(connection, "search", key, sync_id=summary.sync_id):
            summary.deleted += 1
    return delete_reasons


def _rebuild_collection_paths(store: ZoteroStateStore, connection: sqlite3.Connection) -> Any:
    collections = store.load_collections(connection=connection)
    raw_values: list[dict[str, Any]] = []
    for value in collections.values():
        try:
            parsed = json.loads(value["raw_json"])
        except (KeyError, json.JSONDecodeError):
            parsed = {
                "key": value["collection_key"],
                "data": {
                    "key": value["collection_key"],
                    "name": value["name"],
                    "parentCollection": value["parent_collection_key"],
                },
            }
        raw_values.append(parsed)
    result = build_collection_paths(raw_values)
    store.update_collection_paths(connection, result.by_key)
    return result


def _resolve_attachments(
    config: ZoteroConfig,
    store: ZoteroStateStore,
    connection: sqlite3.Connection,
    summary: SyncSummary,
    *,
    sleeper: Callable[[float], None],
    scan_policy: str,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    if scan_policy not in {"all", "changed", "none"}:
        raise ValueError(f"unknown attachment scan policy: {scan_policy}")
    items = store.load_objects("item", connection=connection)
    previous = store.load_attachments(connection)
    all_requests: list[AttachmentRequest] = []
    projections: dict[str, dict[str, Any]] = {}
    for key, row in items.items():
        payload = json.loads(row["raw_json"])
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if data.get("itemType") != "attachment":
            continue
        parent_key = str(data.get("parentItem") or "")
        if not parent_key or parent_key not in items:
            # Keep the complete raw item mirror, but do not project a document
            # from a child whose parent is no longer current.
            continue
        mime = str(data.get("contentType") or "")
        link_mode = str(data.get("linkMode") or "")
        projection: dict[str, Any] = {
            "attachment_key": key,
            "parent_item_key": parent_key,
            "attachment_version": int(row.get("object_version") or 0),
            "link_mode": link_mode,
            "mime_type": mime,
            "api_filename": data.get("filename") or None,
            "archive_path": None,
            "prop_path": None,
            "prop_exists": 0,
            "archive_sha256": None,
            "archive_size_bytes": None,
            "archive_mtime_ns": None,
            "pdf_path": None,
            "pdf_sha256": None,
            "pdf_size_bytes": None,
            "resolver_status": "unsupported_attachment",
            "resolver_error": None,
            "pdf_candidates": [],
            "updated_at": utc_now(),
        }
        projections[key] = projection
        if mime.lower() == "application/pdf" and link_mode in _SUPPORTED_LINK_MODES:
            filename = projection["api_filename"]
            all_requests.append(AttachmentRequest(key, str(filename) if filename else None))
        elif mime.lower() == "application/pdf":
            projection["resolver_error"] = f"unsupported linkMode: {link_mode or '<empty>'}"

    requests: list[AttachmentRequest] = []
    for request in all_requests:
        previous_row = previous.get(request.attachment_key)
        should_scan = scan_policy == "all" or (
            scan_policy == "changed"
            and _attachment_needs_scan(config.webdav_dir, request.attachment_key, previous_row)
        )
        if should_scan:
            requests.append(request)
        elif previous_row is not None:
            _restore_previous_resolution(projections[request.attachment_key], previous_row)

    mapping = store.mapping_validation(connection)
    webdav_realpath = str(config.webdav_dir.resolve(strict=True))
    mapping_binding_matches = bool(
        mapping
        and mapping.get("library_type") == config.library_type
        and int(mapping.get("library_id") or 0) == int(config.library_id or 0)
        and mapping.get("webdav_realpath") == webdav_realpath
    )
    mapping_valid = bool(
        mapping_binding_matches and mapping is not None and mapping.get("status") == "verified"
    )
    if all_requests and not mapping_binding_matches and not requests:
        # Changing the library or WebDAV realpath is a trust-boundary event and
        # always forces fresh mapping validation, even when ordinary 304 scans
        # are disabled.
        requests = list(all_requests)
    if requests and not mapping_valid:
        validation = validate_attachment_mapping(
            config.webdav_dir,
            all_requests,
            sample_size=config.mapping_validation_sample_size,
        )
        mapping_valid = validation.verified
        store.set_mapping_validation(
            connection,
            status="verified" if validation.verified else "unverified",
            library_type=config.library_type,
            library_id=int(config.library_id or 0),
            webdav_realpath=validation.webdav_realpath,
            sample_count=validation.sampled,
            passed_count=validation.passed,
            summary={
                "failed": validation.failed,
                "samples": [asdict(value) for value in validation.samples],
            },
        )

    staged_publications: list[dict[str, str]] = []
    if requests and mapping_valid:
        resolver = AttachmentResolver(
            config.webdav_dir,
            config.data_dir / "extracted",
            stability_observations=config.zip_stability_check_count,
            stability_interval=config.zip_stability_interval_seconds,
            sleeper=sleeper,
            staging_root=config.data_dir / ".staging" / summary.sync_id / "extracted",
        )
        previous_cache: dict[str, AttachmentCacheState] = {}
        for request in requests:
            old = previous.get(request.attachment_key, {})
            projection = projections[request.attachment_key]
            selector_unchanged = old.get("attachment_version") == projection.get(
                "attachment_version"
            ) and old.get("api_filename") == projection.get("api_filename")
            previous_cache[request.attachment_key] = AttachmentCacheState(
                archive_sha256=old.get("archive_sha256") if selector_unchanged else None,
                pdf_sha256=old.get("pdf_sha256") if selector_unchanged else None,
                pdf_path=old.get("pdf_path") if selector_unchanged else None,
                source_size=old.get("archive_size_bytes") if selector_unchanged else None,
                source_mtime_ns=old.get("archive_mtime_ns") if selector_unchanged else None,
                api_filename=(
                    str(old["api_filename"])
                    if selector_unchanged and old.get("api_filename") is not None
                    else None
                ),
            )
        resolutions = resolver.resolve_many(requests, previous=previous_cache)
        for key, resolution in resolutions.items():
            projection = projections[key]
            location = locate_archive(config.webdav_dir, key)
            projection.update(
                {
                    "archive_path": resolution.archive_path,
                    "prop_path": str(location.prop_path)
                    if location.prop_path and location.prop_path.exists()
                    else None,
                    "prop_exists": int(bool(location.prop_path and location.prop_path.exists())),
                    "archive_sha256": resolution.archive_sha256,
                    "archive_size_bytes": resolution.source_size,
                    "archive_mtime_ns": resolution.source_mtime_ns,
                    "pdf_path": resolution.pdf_path,
                    "pdf_sha256": resolution.pdf_sha256,
                    "pdf_size_bytes": resolution.pdf_size,
                    "resolver_status": _wire_status(resolution.status),
                    "resolver_error": resolution.status_detail,
                    "pdf_candidates": _candidates(resolution.status_detail),
                }
            )
        staged_publications = [
            {"staged": str(value.staged), "target": str(value.target)}
            for value in resolver.staged_attachments
        ]
    elif requests:
        # A newly invalidated mapping applies to every API PDF document, not
        # merely the subset selected by the 304 stat scan.
        for request in all_requests:
            location = locate_archive(config.webdav_dir, request.attachment_key)
            projections[request.attachment_key].update(
                {
                    "archive_path": str(location.archive_path) if location.archive_path else None,
                    "prop_path": str(location.prop_path)
                    if location.prop_path and location.prop_path.exists()
                    else None,
                    "prop_exists": int(bool(location.prop_path and location.prop_path.exists())),
                    "resolver_status": "mapping_unverified",
                    "resolver_error": "attachment-key mapping has not been verified",
                }
            )

    for projection in projections.values():
        store.upsert_attachment(connection, projection)
        status = projection["resolver_status"]
        if status == "ready":
            summary.attachments_ready += 1
        elif status == "missing_archive":
            summary.attachments_missing += 1
        elif status == "unstable_archive":
            summary.attachments_unstable += 1
        elif (
            status != "unsupported_attachment"
            or projection["mime_type"].lower() == "application/pdf"
        ):
            summary.attachments_error += 1
    store.retain_attachment_projections(connection, list(projections))
    return store.load_attachments(connection), staged_publications


_RESOLUTION_FIELDS = (
    "archive_path",
    "prop_path",
    "prop_exists",
    "archive_sha256",
    "archive_size_bytes",
    "archive_mtime_ns",
    "pdf_path",
    "pdf_sha256",
    "pdf_size_bytes",
    "resolver_status",
    "resolver_error",
    "pdf_candidates",
    "updated_at",
)


def _restore_previous_resolution(projection: dict[str, Any], previous: Mapping[str, Any]) -> None:
    for field in _RESOLUTION_FIELDS:
        if field in previous:
            projection[field] = previous[field]


def _attachment_needs_scan(
    webdav_root: Path, attachment_key: str, previous: Mapping[str, Any] | None
) -> bool:
    if previous is None or previous.get("resolver_status") != "ready":
        return True
    location = locate_archive(webdav_root, attachment_key)
    if location.problem is not None or location.archive_path is None:
        return True
    try:
        source_stat = location.archive_path.stat(follow_symlinks=False)
    except OSError:
        return True
    return bool(
        str(location.archive_path) != previous.get("archive_path")
        or source_stat.st_size != previous.get("archive_size_bytes")
        or source_stat.st_mtime_ns != previous.get("archive_mtime_ns")
        or not previous.get("prop_exists")
    )


def _wire_status(status: AttachmentStatus) -> str:
    if status is AttachmentStatus.READY:
        return "ready"
    if status is AttachmentStatus.MISSING_ARCHIVE:
        return "missing_archive"
    if status is AttachmentStatus.UNSTABLE_ARCHIVE:
        return "unstable_archive"
    if status.value in {"missing_pdf", "no_pdf"}:
        return "missing_pdf"
    if status is AttachmentStatus.AMBIGUOUS_ATTACHMENT:
        return "ambiguous_attachment"
    if status.value == "extraction_error":
        return "error"
    return "invalid_archive"


def _candidates(detail: str | None) -> list[str]:
    if not detail:
        return []
    try:
        parsed = json.loads(detail)
    except json.JSONDecodeError:
        return []
    candidates = parsed.get("candidates") if isinstance(parsed, dict) else None
    return sorted(str(value) for value in candidates) if isinstance(candidates, list) else []


class _PublicationSession:
    def __init__(
        self,
        data_dir: Path,
        sync_id: str,
        entries: list[dict[str, Any]],
        intent_path: Path,
        target_version: int | None = None,
    ) -> None:
        self.data_dir = data_dir.resolve(strict=True)
        _validate_publication_entries(
            self.data_dir, sync_id, entries, intent_path.resolve(strict=False)
        )
        self.sync_id = sync_id
        self.entries = entries
        self.intent_path = intent_path
        self.target_version = target_version
        self.published = False

    @classmethod
    def prepare(
        cls,
        *,
        data_dir: Path,
        sync_id: str,
        records: Sequence[Mapping[str, Any]],
        deltas: Sequence[Mapping[str, Any]],
        collections: Mapping[str, Any],
        summary: Mapping[str, Any],
        rewrite_snapshot: bool,
        rewrite_collections: bool,
        pre_staged_entries: Sequence[Mapping[str, str]] = (),
    ) -> "_PublicationSession":
        staging = data_dir / ".staging" / sync_id
        staging.mkdir(parents=True, exist_ok=True)
        manifests = data_dir / "manifests"
        staged_manifest = staging / "manifests"
        staged_manifest.mkdir(parents=True)
        entries: list[dict[str, Any]] = []

        def add(staged: Path, target: Path) -> None:
            backup = target.parent / f".{target.name}.backup-{sync_id}"
            entries.append(
                {
                    "staged": str(staged),
                    "target": str(target),
                    "backup": str(backup),
                    "had_target": os.path.lexists(target),
                }
            )

        for entry in pre_staged_entries:
            add(Path(entry["staged"]), Path(entry["target"]))

        if rewrite_snapshot:
            snapshot = staged_manifest / "documents.jsonl"
            write_snapshot(snapshot, records)
            add(snapshot, manifests / "documents.jsonl")
        if rewrite_collections:
            collection_file = staged_manifest / "collections.json"
            write_json(collection_file, collections)
            add(collection_file, manifests / "collections.json")
        delta_file = staged_manifest / f"{sync_id}.jsonl"
        write_delta(delta_file, deltas)
        add(delta_file, manifests / "deltas" / f"{sync_id}.jsonl")
        catalog_file = staged_manifest / "delta-catalog.jsonl"
        append_delta_catalog(
            current_path=manifests / "delta-catalog.jsonl",
            output_path=catalog_file,
            sync_id=sync_id,
            from_version=int(summary.get("from_version") or 0),
            target_version=int(summary.get("target_version") or 0),
            staged_delta_path=delta_file,
            row_count=len(deltas),
        )
        add(catalog_file, manifests / "delta-catalog.jsonl")
        summary_file = staged_manifest / "summary.json"
        write_json(summary_file, summary)
        add(summary_file, manifests / "summary.json")
        run_file = staging / "run-summary.json"
        write_json(run_file, summary)
        add(run_file, data_dir / "runs" / sync_id / "summary.json")

        run_dir = data_dir / "runs" / sync_id
        run_dir.mkdir(parents=True, exist_ok=True)
        intent_path = run_dir / "publish-intent.json"
        target_version_value = summary.get("target_version")
        target_version = int(target_version_value) if target_version_value is not None else None
        atomic_write_json(
            intent_path,
            {
                "schema_version": 1,
                "sync_id": sync_id,
                "target_version": target_version,
                "status": "prepared",
                "entries": entries,
            },
            mode=0o600,
        )
        return cls(data_dir, sync_id, entries, intent_path, target_version)

    def publish(self) -> None:
        for entry in self.entries:
            staged = _publication_path(Path(entry["staged"]), self.data_dir)
            target = _publication_path(Path(entry["target"]), self.data_dir)
            backup = _publication_path(Path(entry["backup"]), self.data_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists():
                safe_remove(backup, root=self.data_dir)
            if target.exists():
                os.replace(target, backup)
                fsync_directory(target.parent)
            os.replace(staged, target)
            fsync_directory(target.parent)
        self.published = True
        atomic_write_json(
            self.intent_path,
            {
                "schema_version": 1,
                "sync_id": self.sync_id,
                "target_version": self.target_version,
                "status": "published",
                "entries": self.entries,
            },
            mode=0o600,
        )

    def commit(self) -> None:
        for entry in self.entries:
            backup = _publication_path(Path(entry["backup"]), self.data_dir)
            if backup.exists():
                safe_remove(backup, root=self.data_dir)
        staging = self.data_dir / ".staging" / self.sync_id
        if staging.exists():
            safe_remove(staging, root=self.data_dir)
        if self.intent_path.exists():
            self.intent_path.unlink()
            fsync_directory(self.intent_path.parent)

    def rollback(self) -> None:
        for entry in reversed(self.entries):
            target = _publication_path(Path(entry["target"]), self.data_dir)
            backup = _publication_path(Path(entry["backup"]), self.data_dir)
            staged = _publication_path(Path(entry["staged"]), self.data_dir)
            had_target = bool(entry.get("had_target", backup.exists()))
            if backup.exists():
                if target.exists():
                    safe_remove(target, root=self.data_dir)
                os.replace(backup, target)
                fsync_directory(target.parent)
            elif not had_target and (self.published or not staged.exists()) and target.exists():
                safe_remove(target, root=self.data_dir)
        staging = self.data_dir / ".staging" / self.sync_id
        if staging.exists():
            safe_remove(staging, root=self.data_dir)
        if self.intent_path.exists():
            self.intent_path.unlink()
            fsync_directory(self.intent_path.parent)


def _publication_path(path: Path, root: Path) -> Path:
    """Validate a publication path without losing its lexical identity."""

    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved_root = root.resolve(strict=True)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ZoteroError(
            "publication_error", f"Publication path escapes data dir: {path}"
        ) from exc
    if candidate.is_symlink():
        raise ZoteroError("publication_error", f"Publication path is a symlink: {path}")
    return candidate


def _validate_publication_entries(
    data_dir: Path,
    sync_id: str,
    entries: Sequence[Mapping[str, Any]],
    intent_path: Path,
) -> None:
    if not _SAFE_COMPONENT.fullmatch(sync_id):
        raise ZoteroError("publication_error", "Unsafe publication sync ID")
    expected_intent = data_dir / "runs" / sync_id / "publish-intent.json"
    if intent_path != expected_intent:
        raise ZoteroError("publication_error", "Unexpected publication intent path")
    for entry in entries:
        try:
            staged = Path(str(entry["staged"]))
            target = Path(str(entry["target"]))
            backup = Path(str(entry["backup"]))
        except KeyError as exc:
            raise ZoteroError("publication_error", "Incomplete publication entry") from exc
        if not staged.is_absolute() or not target.is_absolute() or not backup.is_absolute():
            raise ZoteroError("publication_error", "Publication paths must be absolute")
        try:
            staged_relative = staged.resolve(strict=False).relative_to(data_dir)
            target_relative = target.relative_to(data_dir)
        except ValueError as exc:
            raise ZoteroError("publication_error", "Publication entry escapes data dir") from exc
        if ".." in target_relative.parts or not _allowed_publication_target(
            target_relative, sync_id
        ):
            raise ZoteroError(
                "publication_error", f"Unexpected publication target: {target_relative}"
            )
        if not (
            staged_relative.parts[:2] == (".staging", sync_id)
            or staged_relative.parts[:1] == (".rebuild",)
        ):
            raise ZoteroError("publication_error", "Unexpected publication staging path")
        rebuild_targets = {
            Path("state/zotero.sqlite3"),
            Path("extracted"),
            Path("manifests"),
        }
        is_rebuild_staging = staged_relative.parts[:1] == (".rebuild",)
        if is_rebuild_staging != (target_relative in rebuild_targets):
            raise ZoteroError("publication_error", "Publication mode and target do not match")
        expected_backup = target.parent / f".{target.name}.backup-{sync_id}"
        if backup != expected_backup:
            raise ZoteroError("publication_error", "Unexpected publication backup path")


def _allowed_publication_target(relative: Path, sync_id: str) -> bool:
    exact = {
        Path("manifests/documents.jsonl"),
        Path("manifests/collections.json"),
        Path("manifests/summary.json"),
        Path("manifests/delta-catalog.jsonl"),
        Path(f"manifests/deltas/{sync_id}.jsonl"),
        Path(f"runs/{sync_id}/summary.json"),
        Path("state/zotero.sqlite3"),
        Path("extracted"),
        Path("manifests"),
    }
    if relative in exact:
        return True
    return bool(
        len(relative.parts) == 2
        and relative.parts[0] == "extracted"
        and _SAFE_COMPONENT.fullmatch(relative.parts[1])
    )


def recover_publications(data_dir: Path, store: ZoteroStateStore) -> None:
    runs = data_dir / "runs"
    if not runs.exists():
        return
    state = store.library_state()
    active_sync_id = state.get("active_sync_id") if state else None
    for intent_path in sorted(runs.glob("*/publish-intent.json")):
        try:
            payload = json.loads(intent_path.read_text(encoding="utf-8"))
            entries = payload["entries"]
            sync_id = str(payload["sync_id"])
            if not isinstance(entries, list):
                raise ValueError("entries is not a list")
            target_version_value = payload.get("target_version")
            target_version = int(target_version_value) if target_version_value is not None else None
            session = _PublicationSession(data_dir, sync_id, entries, intent_path, target_version)
            session.published = payload.get("status") == "published"
            version_committed = bool(
                state
                and (
                    target_version is None
                    or int(state.get("library_version") or 0) == target_version
                )
            )
            if active_sync_id == sync_id and version_committed:
                session.commit()
            else:
                session.rollback()
        except (OSError, ValueError, KeyError, json.JSONDecodeError, TypeError) as exc:
            LOGGER.exception(
                "cannot recover manifest publication", extra={"intent": str(intent_path)}
            )
            raise ZoteroError(
                "recovery_error", f"Cannot recover publication intent: {intent_path}"
            ) from exc


def _write_run_summary(data_dir: Path, summary: SyncSummary) -> None:
    path = data_dir / "runs" / summary.sync_id / "summary.json"
    atomic_write_json(path, summary.to_dict())


def _new_sync_id() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex}"
