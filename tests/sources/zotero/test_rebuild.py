from __future__ import annotations

import json
from typing import Any

import httpx

from knowledgehub.sources.zotero.models import RuntimeDependencies
from knowledgehub.sources.zotero.rebuild import rebuild_source
from knowledgehub.sources.zotero.state import ZoteroStateStore


class EmptyLibraryTransport:
    def __init__(self, version: int = 7) -> None:
        self.version = version
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "GET"
        if request.url.path == "/keys/current":
            return httpx.Response(
                200,
                json={"userID": 42, "access": {"user": {"library": True}}},
            )
        if request.url.path == "/users/42/deleted":
            return self._versioned({"items": [], "collections": [], "searches": [], "tags": []})
        if request.url.path in {
            "/users/42/items",
            "/users/42/collections",
            "/users/42/searches",
        }:
            if request.headers.get("If-Modified-Since-Version") == str(self.version):
                return httpx.Response(304)
            assert request.url.params["format"] == "versions"
            assert request.url.params["since"] == "0"
            return self._versioned({})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def _versioned(self, payload: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json=payload,
            headers={"Last-Modified-Version": str(self.version)},
        )


def test_confirmed_rebuild_promotes_valid_candidate_and_replaces_old_runtime(
    zotero_config_factory,
    fake_clock,
) -> None:
    config = zotero_config_factory(max_retries=0, sync_max_retries=0)
    config.prepare_runtime()
    old_store = ZoteroStateStore(config.data_dir)
    old_store.initialize()
    old_store.bind_library("user", 42)
    with old_store.transaction() as connection:
        old_store.set_success_version(connection, version=2, sync_id="old-sync")
    old_snapshot = config.data_dir / "manifests" / "documents.jsonl"
    old_snapshot.write_text("stale snapshot\n", encoding="utf-8")
    old_cache = config.data_dir / "extracted" / "OLD"
    old_cache.mkdir()
    (old_cache / "old.pdf").write_bytes(b"stale")
    preserved_log = config.data_dir / "logs" / "preserved.log"
    preserved_log.write_text("keep", encoding="utf-8")

    webdav_before = list(config.webdav_dir.iterdir())
    remote = EmptyLibraryTransport(version=7)
    result = rebuild_source(
        config,
        confirmed=True,
        dependencies=RuntimeDependencies(
            http_transport=httpx.MockTransport(remote),
            sleeper=fake_clock.sleep,
            monotonic=fake_clock.monotonic,
            random=lambda: 0.0,
        ),
    )

    assert result["dry_run"] is False
    assert result["library_version"] == 7
    assert result["validation"]["valid"] is True
    state = ZoteroStateStore(config.data_dir).library_state()
    assert state is not None
    assert state["library_version"] == 7
    assert old_snapshot.read_text(encoding="utf-8") == ""
    assert not old_cache.exists()
    assert preserved_log.read_text(encoding="utf-8") == "keep"
    assert list(config.webdav_dir.iterdir()) == webdav_before

    summary = json.loads(
        (config.data_dir / "runs" / result["sync_id"] / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == "success"
    assert summary["committed_version"] == 7
    assert not list((config.data_dir / "runs").glob("*/publish-intent.json"))
    assert not list((config.data_dir / ".rebuild").iterdir())
    assert [request.url.path for request in remote.requests] == [
        "/keys/current",
        "/users/42/collections",
        "/users/42/searches",
        "/users/42/items",
        "/users/42/deleted",
        "/users/42/collections",
    ]
