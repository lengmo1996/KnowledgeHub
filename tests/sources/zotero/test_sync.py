from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from knowledgehub.core.atomic import atomic_write_json
from knowledgehub.sources.zotero.models import (
    RuntimeDependencies,
    SyncMode,
    SyncSummary,
    ZoteroError,
)
from knowledgehub.sources.zotero.state import ZoteroStateStore
from knowledgehub.sources.zotero.sync import (
    _apply_remote_state,
    _PublicationSession,
    _RemoteChanges,
    recover_publications,
    resolve_attachments_once,
    sync_once,
)


class VersionDriftTransport:
    def __init__(self, *, recover: bool) -> None:
        self.recover = recover
        self.search_version_requests = 0
        self.collection_since: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/keys/current":
            return httpx.Response(
                200,
                json={"userID": 42, "access": {"user": {"library": True}}},
            )
        if path == "/users/42/collections":
            if "If-Modified-Since-Version" in request.headers:
                return httpx.Response(304)
            self.collection_since.append(request.url.params["since"])
            target = 1 if not self.search_version_requests else 3
            return httpx.Response(
                200,
                json={},
                headers={"Last-Modified-Version": str(target)},
            )
        if path == "/users/42/searches":
            self.search_version_requests += 1
            version = 2 if self.search_version_requests == 1 else 3
            if not self.recover:
                version = 2
            return httpx.Response(200, json={}, headers={"Last-Modified-Version": str(version)})
        if path == "/users/42/items":
            return httpx.Response(200, json={}, headers={"Last-Modified-Version": "3"})
        if path == "/users/42/deleted":
            return httpx.Response(
                200,
                json={"items": [], "collections": [], "searches": [], "tags": []},
                headers={"Last-Modified-Version": "3"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


def _dependencies(transport: Any, fake_clock) -> RuntimeDependencies:
    return RuntimeDependencies(
        http_transport=httpx.MockTransport(transport),
        sleeper=fake_clock.sleep,
        monotonic=fake_clock.monotonic,
        random=lambda: 0.0,
    )


def _publication_session(
    data_dir: Path,
    sync_id: str,
    *,
    original: bytes | None,
) -> tuple[_PublicationSession, Path, Path, Path, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    staging = data_dir / ".staging" / sync_id
    staging.mkdir(parents=True)
    staged = staging / "payload.txt"
    staged.write_bytes(b"new payload")

    target = data_dir / "manifests" / "summary.json"
    target.parent.mkdir(parents=True)
    if original is not None:
        target.write_bytes(original)
    backup = target.parent / f".{target.name}.backup-{sync_id}"
    entry = {
        "staged": str(staged),
        "target": str(target),
        "backup": str(backup),
        "had_target": original is not None,
    }

    intent = data_dir / "runs" / sync_id / "publish-intent.json"
    intent.parent.mkdir(parents=True)
    atomic_write_json(
        intent,
        {
            "schema_version": 1,
            "sync_id": sync_id,
            "status": "prepared",
            "entries": [entry],
        },
    )
    session = _PublicationSession(data_dir, sync_id, [entry], intent)
    return session, staged, target, backup, intent


def test_sync_restarts_whole_remote_read_after_version_drift(
    zotero_config_factory,
    fake_clock,
) -> None:
    transport = VersionDriftTransport(recover=True)
    config = zotero_config_factory(sync_max_retries=1, max_retries=0)

    summary = sync_once(
        config,
        mode=SyncMode.INCREMENTAL,
        dependencies=_dependencies(transport, fake_clock),
    )

    assert summary.status == "success"
    assert summary.from_version == 0
    assert summary.target_version == 3
    assert summary.committed_version == 3
    assert transport.search_version_requests == 2
    assert transport.collection_since == ["0", "0"]
    assert fake_clock.sleeps == [1.0]
    state = ZoteroStateStore(config.data_dir).library_state()
    assert state is not None
    assert state["library_version"] == 3


def test_exhausted_version_drift_records_failure_without_advancing_version(
    zotero_config_factory,
    fake_clock,
) -> None:
    transport = VersionDriftTransport(recover=False)
    config = zotero_config_factory(sync_max_retries=0, max_retries=0)

    with pytest.raises(ZoteroError) as error:
        sync_once(
            config,
            dependencies=_dependencies(transport, fake_clock),
        )

    assert error.value.code == "remote_version_changed"
    store = ZoteroStateStore(config.data_dir)
    state = store.library_state()
    assert state is not None
    assert state["library_version"] == 0
    run = store.recent_runs()[0]
    assert run["status"] == "failed"
    assert run["error_code"] == "remote_version_changed"
    assert run["committed_version"] is None


def test_attachment_rescan_requires_existing_bound_state(
    zotero_config_factory,
    fake_clock,
) -> None:
    config = zotero_config_factory()
    dependencies = RuntimeDependencies(
        http_transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(AssertionError(f"network call: {request.url}"))
        ),
        sleeper=fake_clock.sleep,
        monotonic=fake_clock.monotonic,
    )

    with pytest.raises(ZoteroError, match="before a successful metadata sync") as error:
        resolve_attachments_once(config, dependencies=dependencies)

    assert error.value.code == "state_error"


def test_attachment_rescan_rejects_same_id_with_different_library_type(
    zotero_config_factory,
    fake_clock,
) -> None:
    config = zotero_config_factory(library_type="user", library_id=42)
    config.prepare_runtime()
    store = ZoteroStateStore(config.data_dir)
    store.initialize()
    store.bind_library("group", 42)

    with pytest.raises(ZoteroError, match="does not match local state") as error:
        resolve_attachments_once(
            config,
            dependencies=RuntimeDependencies(
                sleeper=fake_clock.sleep,
                monotonic=fake_clock.monotonic,
            ),
        )

    assert error.value.code == "library_binding_mismatch"


def test_parent_deletion_reason_wins_when_parent_and_attachment_are_deleted_together(
    tmp_path: Path,
) -> None:
    store = ZoteroStateStore(tmp_path / "data")
    store.initialize()
    store.bind_library("user", 42)
    document_id = "zotero:user:42:A_PARENT:Z_ATTACH:0"
    with store.transaction() as connection:
        store.upsert_remote_object(
            connection,
            "item",
            {"key": "A_PARENT", "version": 1, "data": {"key": "A_PARENT"}},
        )
        store.upsert_remote_object(
            connection,
            "item",
            {
                "key": "Z_ATTACH",
                "version": 1,
                "data": {"key": "Z_ATTACH", "parentItem": "A_PARENT"},
            },
        )
        store.upsert_document(
            connection,
            {
                "document_id": document_id,
                "parent_item_key": "A_PARENT",
                "attachment_key": "Z_ATTACH",
                "pdf_index": 0,
                "metadata_fingerprint": "a" * 64,
                "content_fingerprint": "b" * 64,
                "document_fingerprint": "c" * 64,
                "status": "ready",
                "manifest_json": "{}",
            },
        )

    summary = SyncSummary(sync_id="delete-both", mode="incremental", status="running")
    changes = _RemoteChanges(
        target_version=2,
        not_modified=False,
        objects={"item": [], "collection": [], "search": []},
        deleted={
            "items": ["A_PARENT", "Z_ATTACH"],
            "collections": [],
            "searches": [],
            "tags": [],
        },
        unchanged=0,
    )
    with store.transaction() as connection:
        reasons = _apply_remote_state(store, connection, summary, changes)

    assert reasons == {document_id: "zotero_item_deleted"}


def test_attachment_mode_delegates_to_local_rescan(monkeypatch, zotero_config_factory) -> None:
    import knowledgehub.sources.zotero.sync as sync_module

    expected = object()
    seen: dict[str, object] = {}

    def fake_resolve(config, *, dependencies=None):
        seen["config"] = config
        seen["dependencies"] = dependencies
        return expected

    monkeypatch.setattr(sync_module, "resolve_attachments_once", fake_resolve)
    dependencies = RuntimeDependencies()
    config = zotero_config_factory()

    result = sync_module.sync_once(config, mode=SyncMode.ATTACHMENTS, dependencies=dependencies)

    assert result is expected
    assert seen == {"config": config, "dependencies": dependencies}


def test_api_key_verification_failure_is_audited_with_run_summary(
    zotero_config_factory,
    fake_clock,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(403)

    config = zotero_config_factory(max_retries=0)

    with pytest.raises(ZoteroError) as error:
        sync_once(
            config,
            dependencies=_dependencies(handler, fake_clock),
        )

    assert error.value.code == "invalid_api_key"
    store = ZoteroStateStore(config.data_dir)
    runs = store.recent_runs()
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "failed"
    assert run["error_code"] == "invalid_api_key"
    assert run["from_version"] == 0
    assert run["target_version"] is None
    assert run["committed_version"] is None
    assert store.library_state() is None

    summary_path = config.data_dir / "runs" / run["sync_id"] / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["sync_id"] == run["sync_id"]
    assert summary["status"] == "failed"
    assert summary["error_code"] == "invalid_api_key"
    assert "test-api-key" not in summary_path.read_text(encoding="utf-8")
    assert [request.url.path for request in requests] == ["/keys/current"]


@pytest.mark.parametrize("original", [b"old payload", None])
def test_publication_rollback_is_idempotent(
    tmp_path: Path,
    original: bytes | None,
) -> None:
    data_dir = tmp_path / "data"
    session, staged, target, backup, intent = _publication_session(
        data_dir,
        "rollback",
        original=original,
    )
    session.publish()
    assert target.read_bytes() == b"new payload"

    session.rollback()
    session.rollback()

    if original is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == original
    assert not staged.exists()
    assert not backup.exists()
    assert not intent.exists()


def test_recover_publications_commits_active_sync(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    session, staged, target, backup, intent = _publication_session(
        data_dir,
        "active-sync",
        original=b"old payload",
    )
    session.publish()
    store = ZoteroStateStore(data_dir)
    store.initialize()
    store.bind_library("user", 42)
    with store.transaction() as connection:
        store.set_success_version(connection, version=7, sync_id="active-sync")

    recover_publications(data_dir, store)
    recover_publications(data_dir, store)

    assert target.read_bytes() == b"new payload"
    assert not staged.exists()
    assert not backup.exists()
    assert not intent.exists()


def test_recover_publications_rolls_back_uncommitted_sync(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    session, staged, target, backup, intent = _publication_session(
        data_dir,
        "uncommitted-sync",
        original=b"old payload",
    )
    session.publish()
    store = ZoteroStateStore(data_dir)
    store.initialize()
    store.bind_library("user", 42)
    with store.transaction() as connection:
        store.set_success_version(connection, version=6, sync_id="previous-sync")

    recover_publications(data_dir, store)
    recover_publications(data_dir, store)

    assert target.read_bytes() == b"old payload"
    assert not staged.exists()
    assert not backup.exists()
    assert not intent.exists()
