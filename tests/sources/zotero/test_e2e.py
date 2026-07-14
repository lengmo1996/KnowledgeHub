from __future__ import annotations

import hashlib
import json
import zipfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from knowledgehub.sources.zotero.models import RuntimeDependencies, SyncMode
from knowledgehub.sources.zotero.state import ZoteroStateStore
from knowledgehub.sources.zotero.sync import resolve_attachments_once, sync_once
from knowledgehub.sources.zotero.validation import validate_source

PDF_V1 = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PDF_V2 = b"%PDF-1.4\n1 0 obj<</Version 2>>endobj\ntrailer<<>>\n%%EOF\n"


def _write_archive(root: Path, key: str, pdf: bytes, *, generation: int) -> Path:
    archive = root / f"{key}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
        stream.writestr("nested/paper.pdf", pdf)
        stream.writestr("nested/readme.txt", b"not extracted")
    archive.with_suffix(".prop").write_text(f"generation={generation}\n", encoding="utf-8")
    return archive


def _fingerprint(path: Path) -> tuple[int, int, int, str]:
    value = path.stat()
    return (
        value.st_ino,
        value.st_mtime_ns,
        value.st_size,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class MockZoteroLibrary:
    def __init__(self) -> None:
        self.version = 1
        self.items: dict[str, dict[str, Any]] = {
            "PARENT": {
                "key": "PARENT",
                "version": 1,
                "data": {
                    "key": "PARENT",
                    "version": 1,
                    "itemType": "journalArticle",
                    "title": "Original title",
                    "creators": [
                        {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}
                    ],
                    "abstractNote": "RAG paper",
                    "publicationTitle": "Knowledge Journal",
                    "date": "2026-07-14",
                    "DOI": "10.1000/ABC",
                    "url": "https://example.test/paper",
                    "language": "en",
                    "rights": "CC-BY",
                    "tags": [{"tag": "rag"}],
                    "collections": ["COLL"],
                    "relations": {},
                    "dateAdded": "2026-07-01T00:00:00Z",
                    "dateModified": "2026-07-14T01:00:00Z",
                },
            },
            "ATTACH": {
                "key": "ATTACH",
                "version": 1,
                "data": {
                    "key": "ATTACH",
                    "version": 1,
                    "itemType": "attachment",
                    "parentItem": "PARENT",
                    "linkMode": "imported_file",
                    "contentType": "application/pdf",
                    "filename": "paper.pdf",
                    "dateAdded": "2026-07-01T00:00:00Z",
                    "dateModified": "2026-07-14T01:00:00Z",
                },
            },
        }
        self.collections: dict[str, dict[str, Any]] = {
            "COLL": {
                "key": "COLL",
                "version": 1,
                "data": {
                    "key": "COLL",
                    "version": 1,
                    "name": "Research",
                    "parentCollection": False,
                },
            }
        }
        self.searches: dict[str, dict[str, Any]] = {}
        self.deleted: dict[str, list[str]] = {
            "items": [],
            "collections": [],
            "searches": [],
            "tags": [],
        }
        self.requests: list[httpx.Request] = []

    def update_parent_title(self, title: str) -> None:
        self.version += 1
        parent = deepcopy(self.items["PARENT"])
        parent["version"] = self.version
        parent["data"]["version"] = self.version
        parent["data"]["title"] = title
        parent["data"]["dateModified"] = "2026-07-14T02:00:00Z"
        self.items["PARENT"] = parent

    def delete_parent(self) -> None:
        self.version += 1
        self.items.pop("PARENT")
        self.deleted["items"] = ["PARENT"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "GET"
        assert request.headers["Zotero-API-Key"] == "test-api-key"
        assert request.headers["Zotero-API-Version"] == "3"
        path = request.url.path
        if path == "/keys/current":
            return httpx.Response(
                200,
                json={"userID": 42, "access": {"user": {"library": True}}},
            )
        if path == "/users/42/deleted":
            since = int(request.url.params["since"])
            payload = self.deleted if since < self.version else {key: [] for key in self.deleted}
            return self._versioned(payload)
        endpoints = {
            "/users/42/items": (self.items, "itemKey"),
            "/users/42/collections": (self.collections, "collectionKey"),
            "/users/42/searches": (self.searches, "searchKey"),
        }
        if path not in endpoints:
            raise AssertionError(f"unexpected Zotero request: {request.url}")
        values, key_parameter = endpoints[path]
        conditional = request.headers.get("If-Modified-Since-Version")
        if conditional is not None and int(conditional) == self.version:
            return httpx.Response(304)
        if request.url.params.get("format") == "versions":
            since = int(request.url.params["since"])
            versions = {
                key: int(value["version"])
                for key, value in values.items()
                if int(value["version"]) > since
            }
            return self._versioned(versions)
        requested = request.url.params[key_parameter].split(",")
        return self._versioned([deepcopy(values[key]) for key in requested])

    def _versioned(self, payload: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json=payload,
            headers={"Last-Modified-Version": str(self.version)},
        )


def test_mock_zotero_to_sqlite_zip_manifests_incremental_lifecycle(
    tmp_path: Path,
    zotero_config_factory,
    fake_clock,
) -> None:
    webdav = tmp_path / "webdav"
    archive = _write_archive(webdav, "ATTACH", PDF_V1, generation=1)
    prop = archive.with_suffix(".prop")
    source_before = (_fingerprint(archive), _fingerprint(prop))
    config = zotero_config_factory(
        webdav_dir=webdav,
        data_dir=tmp_path / "data",
        max_retries=0,
        sync_max_retries=0,
        zip_stability_interval_seconds=0,
        zip_stability_check_count=2,
    )
    server = MockZoteroLibrary()
    dependencies = RuntimeDependencies(
        http_transport=httpx.MockTransport(server),
        sleeper=fake_clock.sleep,
        monotonic=fake_clock.monotonic,
        random=lambda: 0.0,
    )

    first = sync_once(config, mode=SyncMode.FULL, dependencies=dependencies)
    store = ZoteroStateStore(config.data_dir)
    first_snapshot = _jsonl(config.data_dir / "manifests" / "documents.jsonl")
    first_delta = _jsonl(config.data_dir / "manifests" / "deltas" / f"{first.sync_id}.jsonl")
    attachment = store.load_attachments()["ATTACH"]
    extracted = Path(attachment["pdf_path"])
    extracted_before = _fingerprint(extracted)
    snapshot_before = (config.data_dir / "manifests" / "documents.jsonl").stat()

    assert first.status == "success"
    assert first.committed_version == 1
    assert first.attachments_ready == 1
    assert len(first_snapshot) == 1
    assert first_snapshot[0]["status"] == "ready"
    assert first_snapshot[0]["title"] == "Original title"
    assert first_snapshot[0]["attachment"]["pdf_path"] == str(extracted)
    assert extracted.read_bytes() == PDF_V1
    assert list(extracted.parent.iterdir()) == [extracted]
    assert first_delta[0]["reason"] == "new_document"
    assert first_delta[0]["chunk_required"] is True
    assert source_before == (_fingerprint(archive), _fingerprint(prop))
    assert store.mapping_validation()["status"] == "verified"

    requests_before_304 = len(server.requests)
    second = sync_once(config, dependencies=dependencies)
    second_delta = config.data_dir / "manifests" / "deltas" / f"{second.sync_id}.jsonl"
    snapshot_after = (config.data_dir / "manifests" / "documents.jsonl").stat()

    assert second.status == "success"
    assert second.details["metadata_not_modified"] is True
    assert second.committed_version == 1
    assert second_delta.read_text(encoding="utf-8") == ""
    assert extracted_before == _fingerprint(extracted)
    assert snapshot_before.st_ino == snapshot_after.st_ino
    assert snapshot_before.st_mtime_ns == snapshot_after.st_mtime_ns
    second_requests = server.requests[requests_before_304:]
    assert [request.url.path for request in second_requests] == [
        "/keys/current",
        "/users/42/collections",
        "/users/42/collections",
    ]

    server.update_parent_title("Metadata changed")
    metadata_sync = sync_once(config, dependencies=dependencies)
    metadata_delta = _jsonl(
        config.data_dir / "manifests" / "deltas" / f"{metadata_sync.sync_id}.jsonl"
    )

    assert metadata_sync.committed_version == 2
    assert len(metadata_delta) == 1
    assert metadata_delta[0]["reason"] == "metadata_changed"
    assert metadata_delta[0]["metadata_changed"] is True
    assert metadata_delta[0]["content_changed"] is False
    assert metadata_delta[0]["chunk_required"] is False
    current_snapshot = _jsonl(config.data_dir / "manifests" / "documents.jsonl")
    assert current_snapshot[0]["title"] == "Metadata changed"
    assert extracted_before == _fingerprint(extracted)

    _write_archive(webdav, "ATTACH", PDF_V2, generation=2)
    archive_after_external_change = _fingerprint(archive)
    prop_after_external_change = _fingerprint(prop)
    calls_before_rescan = len(server.requests)
    content_sync = resolve_attachments_once(config, dependencies=dependencies)
    content_delta = _jsonl(
        config.data_dir / "manifests" / "deltas" / f"{content_sync.sync_id}.jsonl"
    )
    state_after_rescan = store.library_state()

    assert len(server.requests) == calls_before_rescan
    assert content_sync.mode == "attachments"
    assert content_sync.from_version == 2
    assert content_sync.committed_version == 2
    assert state_after_rescan["library_version"] == 2
    assert content_delta[0]["reason"] == "content_changed"
    assert content_delta[0]["content_changed"] is True
    assert content_delta[0]["chunk_required"] is True
    new_attachment = store.load_attachments()["ATTACH"]
    new_extracted = Path(new_attachment["pdf_path"])
    assert new_extracted.read_bytes() == PDF_V2
    assert new_attachment["pdf_sha256"] != attachment["pdf_sha256"]
    assert archive_after_external_change == _fingerprint(archive)
    assert prop_after_external_change == _fingerprint(prop)

    validation = validate_source(config)
    assert validation.valid, validation.to_dict()
    assert validation.checks["ready_attachments"] == 1
    assert validation.checks["current_document_count"] == 1

    server.delete_parent()
    deleted_sync = sync_once(config, dependencies=dependencies)
    delete_delta = _jsonl(
        config.data_dir / "manifests" / "deltas" / f"{deleted_sync.sync_id}.jsonl"
    )
    documents = store.load_documents(include_deleted=True)
    document = documents["zotero:user:42:PARENT:ATTACH:0"]

    assert deleted_sync.committed_version == 3
    assert deleted_sync.deleted == 1
    assert _jsonl(config.data_dir / "manifests" / "documents.jsonl") == []
    assert len(delete_delta) == 1
    assert delete_delta[0]["operation"] == "delete"
    assert delete_delta[0]["reason"] == "zotero_item_deleted"
    assert delete_delta[0]["chunk_required"] is False
    assert document["deleted"] == 1
    assert document["delete_reason"] == "zotero_item_deleted"
    assert new_extracted.read_bytes() == PDF_V2
    assert archive_after_external_change == _fingerprint(archive)
    assert prop_after_external_change == _fingerprint(prop)


def test_304_attachment_scan_policy_and_explicit_rescan(
    tmp_path: Path,
    zotero_config_factory,
    fake_clock,
    monkeypatch,
) -> None:
    """304 scans avoid I/O unless enabled and local archive stats actually changed."""

    import knowledgehub.sources.zotero.attachments as attachments_module

    webdav = tmp_path / "webdav"
    archive = _write_archive(webdav, "ATTACH", PDF_V1, generation=1)
    config = zotero_config_factory(
        webdav_dir=webdav,
        data_dir=tmp_path / "data",
        max_retries=0,
        sync_max_retries=0,
        zip_stability_interval_seconds=0,
        zip_stability_check_count=2,
    )
    server = MockZoteroLibrary()
    dependencies = RuntimeDependencies(
        http_transport=httpx.MockTransport(server),
        sleeper=fake_clock.sleep,
        monotonic=fake_clock.monotonic,
        random=lambda: 0.0,
    )

    initial = sync_once(config, mode=SyncMode.FULL, dependencies=dependencies)
    store = ZoteroStateStore(config.data_dir)
    initial_attachment = store.load_attachments()["ATTACH"]
    extracted = Path(initial_attachment["pdf_path"])
    snapshot_path = config.data_dir / "manifests" / "documents.jsonl"
    snapshot_before = snapshot_path.read_bytes()
    snapshot_stat_before = snapshot_path.stat()
    initial_version = int(store.library_state()["library_version"])
    assert initial.committed_version == initial_version == 1
    assert extracted.read_bytes() == PDF_V1

    calls = {"resolver": 0, "hash": 0}
    original_resolve_many = attachments_module.AttachmentResolver.resolve_many
    original_hash = attachments_module._sha256_readonly

    def counted_resolve_many(self, *args: Any, **kwargs: Any):
        calls["resolver"] += 1
        return original_resolve_many(self, *args, **kwargs)

    def counted_hash(path: Path) -> str:
        calls["hash"] += 1
        return original_hash(path)

    monkeypatch.setattr(attachments_module.AttachmentResolver, "resolve_many", counted_resolve_many)
    monkeypatch.setattr(attachments_module, "_sha256_readonly", counted_hash)

    # A ready archive whose path/size/mtime are unchanged is stat-checked, but
    # must not instantiate the resolver or hash either source or cache content.
    unchanged = sync_once(config, dependencies=dependencies)
    assert unchanged.details["metadata_not_modified"] is True
    assert calls == {"resolver": 0, "hash": 0}
    assert (
        config.data_dir / "manifests" / "deltas" / f"{unchanged.sync_id}.jsonl"
    ).read_bytes() == b""
    assert snapshot_path.read_bytes() == snapshot_before
    assert snapshot_path.stat().st_ino == snapshot_stat_before.st_ino
    assert snapshot_path.stat().st_mtime_ns == snapshot_stat_before.st_mtime_ns

    # With 304 attachment scanning disabled, even a locally changed archive is
    # deliberately deferred.  The committed projection and snapshot stay on V1.
    _write_archive(webdav, "ATTACH", PDF_V2, generation=2)
    changed_source_fingerprint = _fingerprint(archive)
    no_304_scan = replace(config, attachment_scan_on_304=False)
    deferred = sync_once(no_304_scan, dependencies=dependencies)
    deferred_attachment = store.load_attachments()["ATTACH"]
    assert deferred.details["metadata_not_modified"] is True
    assert deferred.committed_version == initial_version
    assert calls == {"resolver": 0, "hash": 0}
    assert deferred_attachment["archive_sha256"] == initial_attachment["archive_sha256"]
    assert deferred_attachment["pdf_sha256"] == initial_attachment["pdf_sha256"]
    assert extracted.read_bytes() == PDF_V1
    assert snapshot_path.read_bytes() == snapshot_before
    assert (
        config.data_dir / "manifests" / "deltas" / f"{deferred.sync_id}.jsonl"
    ).read_bytes() == b""

    # Explicit attachment resolution ignores the 304 scan preference, performs
    # no HTTP calls, publishes V2, and leaves the Zotero library version intact.
    http_calls_before = len(server.requests)
    resolved = resolve_attachments_once(no_304_scan, dependencies=dependencies)
    resolved_attachment = store.load_attachments()["ATTACH"]
    resolved_delta = _jsonl(config.data_dir / "manifests" / "deltas" / f"{resolved.sync_id}.jsonl")
    assert len(server.requests) == http_calls_before
    assert calls["resolver"] == 1
    assert calls["hash"] >= 1
    assert resolved.mode == "attachments"
    assert resolved.from_version == initial_version
    assert resolved.committed_version == initial_version
    assert int(store.library_state()["library_version"]) == initial_version
    assert resolved_attachment["archive_sha256"] != initial_attachment["archive_sha256"]
    assert resolved_attachment["pdf_sha256"] != initial_attachment["pdf_sha256"]
    assert Path(resolved_attachment["pdf_path"]).read_bytes() == PDF_V2
    assert resolved_delta[0]["reason"] == "content_changed"
    assert resolved_delta[0]["content_changed"] is True
    assert resolved_delta[0]["chunk_required"] is True
    assert changed_source_fingerprint == _fingerprint(archive)
