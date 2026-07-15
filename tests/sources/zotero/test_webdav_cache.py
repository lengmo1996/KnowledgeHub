from __future__ import annotations

import json
import logging
from urllib.parse import quote

import httpx
import pytest

from knowledgehub.sources.zotero.config import SecretValue
from knowledgehub.sources.zotero.models import ZoteroError
from knowledgehub.sources.zotero.webdav_cache import (
    NutstoreWebDAVClient,
    refresh_webdav_cache,
)


def _multistatus(*files: tuple[str, bytes, str]) -> bytes:
    responses = [
        """
        <d:response>
          <d:href>/dav/zotero/</d:href>
          <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
          <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
        </d:response>
        """
    ]
    for name, content, etag in files:
        responses.append(
            f"""
            <d:response>
              <d:href>/dav/zotero/{quote(name)}</d:href>
              <d:propstat><d:prop>
                <d:getcontentlength>{len(content)}</d:getcontentlength>
                <d:getlastmodified>Wed, 15 Jul 2026 00:00:00 GMT</d:getlastmodified>
                <d:getetag>{etag}</d:getetag><d:resourcetype/>
              </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
            </d:response>
            """
        )
    return (f'<d:multistatus xmlns:d="DAV:">{"".join(responses)}</d:multistatus>').encode()


def _configured(zotero_config_factory, **overrides):
    values = {
        "webdav_username": SecretValue("user@example.com"),
        "webdav_password": SecretValue("application-password"),
        "webdav_request_interval_seconds": 0,
        "webdav_retry_cooldown_seconds": 0,
        "webdav_max_retry_delay_seconds": 60,
        "max_retries": 0,
        **overrides,
    }
    return zotero_config_factory(**values)


def test_refresh_follows_next_pages_then_downloads_prunes_and_reuses_index(
    zotero_config_factory,
) -> None:
    objects = {
        "AAAA1111.zip": b"zip-one",
        "AAAA1111.prop": b"prop-one",
        "BBBB2222.zip": b"zip-two",
        "BBBB2222.prop": b"prop-two",
    }
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        assert request.headers["authorization"].startswith("Basic ")
        if request.method == "PROPFIND":
            assert request.headers["depth"] == "1"
            if request.url.query:
                return httpx.Response(
                    207,
                    content=_multistatus(
                        ("BBBB2222.zip", objects["BBBB2222.zip"], '"b-zip"'),
                        ("BBBB2222.prop", objects["BBBB2222.prop"], '"b-prop"'),
                    ),
                )
            return httpx.Response(
                207,
                headers={
                    "Link": '<https://dav.jianguoyun.com/dav/zotero/?mk=%2FBBBB2222.zip>; rel="next"'
                },
                content=_multistatus(
                    ("AAAA1111.zip", objects["AAAA1111.zip"], '"a-zip"'),
                    ("AAAA1111.prop", objects["AAAA1111.prop"], '"a-prop"'),
                ),
            )
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=objects[name])

    config = _configured(zotero_config_factory)
    (config.webdav_dir / "STALE999.zip").write_bytes(b"stale")
    transport = httpx.MockTransport(handler)

    first = refresh_webdav_cache(config, transport=transport)

    assert first.pages == 2
    assert first.remote_objects == 4
    assert first.downloaded == 4
    assert first.adopted == 0
    assert first.unchanged == 0
    assert first.resumed == 0
    assert first.deleted == 1
    assert first.bytes_downloaded == sum(map(len, objects.values()))
    assert not (config.webdav_dir / "STALE999.zip").exists()
    for name, content in objects.items():
        assert (config.webdav_dir / name).read_bytes() == content

    requests.clear()
    second = refresh_webdav_cache(config, transport=transport)

    assert second.downloaded == 0
    assert second.unchanged == 4
    assert second.resumed == 0
    assert [method for method, _url in requests] == ["PROPFIND", "PROPFIND"]
    index = json.loads(
        (config.webdav_dir / ".knowledgehub-webdav-index.json").read_text(encoding="utf-8")
    )
    assert sorted(index["objects"]) == sorted(objects)


def test_refresh_paces_listing_download_and_retry_requests(
    zotero_config_factory,
    fake_clock,
    caplog,
) -> None:
    objects = {
        "AAAA1111.zip": b"zip-one",
        "BBBB2222.prop": b"prop-two",
    }
    request_started_at: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_started_at.append(fake_clock.now)
        if request.method == "PROPFIND":
            return httpx.Response(
                207,
                content=_multistatus(
                    *((name, content, f'"{name}-etag"') for name, content in objects.items())
                ),
            )
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=objects[name])

    config = _configured(zotero_config_factory, webdav_request_interval_seconds=0.5)
    caplog.set_level(logging.INFO, logger="knowledgehub.sources.zotero.webdav_cache")
    summary = refresh_webdav_cache(
        config,
        transport=httpx.MockTransport(handler),
        sleeper=fake_clock.sleep,
        monotonic=fake_clock.monotonic,
    )

    assert summary.downloaded == 2
    assert request_started_at == [0.0, 0.5, 1.0]
    assert fake_clock.sleeps == [0.5, 0.5]
    messages = [record.getMessage() for record in caplog.records]
    assert "listed 2 remote objects across 1 WebDAV page(s)" in messages
    assert [message for message in messages if message.startswith("WebDAV cache progress")] == [
        "WebDAV cache progress 1/2 downloaded=1 adopted=0 resumed=0 unchanged=0 "
        "object=AAAA1111.zip",
        "WebDAV cache progress 2/2 downloaded=2 adopted=0 resumed=0 unchanged=0 "
        "object=BBBB2222.prop",
    ]


def test_retry_backoff_already_satisfies_request_interval(
    zotero_config_factory,
    fake_clock,
) -> None:
    attempts = 0
    request_started_at: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        request_started_at.append(fake_clock.now)
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(207, content=_multistatus())

    config = _configured(
        zotero_config_factory,
        max_retries=1,
        webdav_request_interval_seconds=0.5,
    )
    refresh_webdav_cache(
        config,
        transport=httpx.MockTransport(handler),
        sleeper=fake_clock.sleep,
        monotonic=fake_clock.monotonic,
        random_fn=lambda: 0.5,
    )

    assert request_started_at == [0.0, 0.5]
    assert fake_clock.sleeps == [0.5]


def test_503_uses_configured_sustained_cooldown(
    zotero_config_factory,
    fake_clock,
    caplog,
) -> None:
    attempts = 0
    request_started_at: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        request_started_at.append(fake_clock.now)
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(207, content=_multistatus())

    config = _configured(
        zotero_config_factory,
        max_retries=1,
        webdav_retry_cooldown_seconds=900,
        webdav_max_retry_delay_seconds=1800,
    )
    refresh_webdav_cache(
        config,
        transport=httpx.MockTransport(handler),
        sleeper=fake_clock.sleep,
        monotonic=fake_clock.monotonic,
        random_fn=lambda: 0.5,
    )

    assert request_started_at == [0.0, 900.0]
    assert fake_clock.sleeps == [900.0]
    assert "WebDAV HTTP 503 triggered 900.0-second cooldown before retry 1/1" in [
        record.getMessage() for record in caplog.records
    ]


def test_refresh_can_adopt_explicitly_trusted_unindexed_file(
    zotero_config_factory,
) -> None:
    content = b"copied-out-of-band"
    get_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return httpx.Response(
                207,
                content=_multistatus(("AAAA1111.zip", content, '"remote-etag"')),
            )
        get_requests.append(request.url.path)
        return httpx.Response(200, content=content)

    config = _configured(zotero_config_factory)
    destination = config.webdav_dir / "AAAA1111.zip"
    destination.write_bytes(content)

    summary = refresh_webdav_cache(
        config,
        adopt_existing=True,
        transport=httpx.MockTransport(handler),
    )

    assert summary.adopted == 1
    assert summary.downloaded == 0
    assert get_requests == []
    assert destination.read_bytes() == content
    index = json.loads(
        (config.webdav_dir / ".knowledgehub-webdav-index.json").read_text(encoding="utf-8")
    )
    assert index["objects"]["AAAA1111.zip"]["etag"] == '"remote-etag"'


def test_adopt_existing_redownloads_size_mismatch(zotero_config_factory) -> None:
    remote_content = b"remote-content"
    get_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return httpx.Response(
                207,
                content=_multistatus(("AAAA1111.zip", remote_content, '"remote-etag"')),
            )
        get_requests.append(request.url.path)
        return httpx.Response(200, content=remote_content)

    config = _configured(zotero_config_factory)
    destination = config.webdav_dir / "AAAA1111.zip"
    destination.write_bytes(b"wrong-size")

    summary = refresh_webdav_cache(
        config,
        adopt_existing=True,
        transport=httpx.MockTransport(handler),
    )

    assert summary.adopted == 0
    assert summary.downloaded == 1
    assert get_requests == ["/dav/zotero/AAAA1111.zip"]
    assert destination.read_bytes() == remote_content


def test_failed_initial_refresh_resumes_checkpointed_downloads(
    zotero_config_factory,
) -> None:
    objects = {
        "AAAA1111.zip": b"zip-one",
        "BBBB2222.zip": b"zip-two",
        "CCCC3333.zip": b"zip-three",
    }
    fail_second = True
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_second
        requests.append((request.method, request.url.path))
        if request.method == "PROPFIND":
            return httpx.Response(
                207,
                content=_multistatus(
                    *((name, content, f'"{name}-etag"') for name, content in objects.items())
                ),
            )
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "BBBB2222.zip" and fail_second:
            return httpx.Response(503)
        return httpx.Response(200, content=objects[name])

    config = _configured(zotero_config_factory)
    transport = httpx.MockTransport(handler)

    with pytest.raises(ZoteroError, match=r"BBBB2222\.zip") as error:
        refresh_webdav_cache(config, transport=transport)

    assert error.value.code == "network_error"
    progress_path = config.webdav_dir / ".knowledgehub-webdav-progress.json"
    assert error.value.context == {
        "checkpointed_objects": 1,
        "progress_index": str(progress_path),
    }
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert sorted(progress["objects"]) == ["AAAA1111.zip"]
    assert progress_path.stat().st_mode & 0o777 == 0o600
    assert not (config.webdav_dir / ".knowledgehub-webdav-index.json").exists()

    unavailable = httpx.MockTransport(lambda _request: httpx.Response(503))
    with pytest.raises(ZoteroError, match="listing remained unavailable") as listing_error:
        refresh_webdav_cache(config, transport=unavailable)
    assert listing_error.value.context == {
        "checkpointed_objects": 1,
        "endpoint": "listing",
        "progress_index": str(progress_path),
        "status": 503,
    }

    fail_second = False
    requests.clear()
    summary = refresh_webdav_cache(config, transport=transport)

    assert summary.downloaded == 2
    assert summary.resumed == 1
    assert summary.unchanged == 0
    assert [path.rsplit("/", 1)[-1] for method, path in requests if method == "GET"] == [
        "BBBB2222.zip",
        "CCCC3333.zip",
    ]
    assert not progress_path.exists()
    index = json.loads(
        (config.webdav_dir / ".knowledgehub-webdav-index.json").read_text(encoding="utf-8")
    )
    assert sorted(index["objects"]) == sorted(objects)


def test_resume_redownloads_checkpoint_when_remote_metadata_changed(
    zotero_config_factory,
) -> None:
    objects = {
        "AAAA1111.zip": b"old-data",
        "BBBB2222.zip": b"second",
    }
    etags = {"AAAA1111.zip": '"old"', "BBBB2222.zip": '"second"'}
    fail_second = True
    get_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_second
        if request.method == "PROPFIND":
            return httpx.Response(
                207,
                content=_multistatus(
                    *((name, content, etags[name]) for name, content in objects.items())
                ),
            )
        name = request.url.path.rsplit("/", 1)[-1]
        get_requests.append(name)
        if name == "BBBB2222.zip" and fail_second:
            return httpx.Response(503)
        return httpx.Response(200, content=objects[name])

    config = _configured(zotero_config_factory)
    transport = httpx.MockTransport(handler)
    with pytest.raises(ZoteroError, match=r"BBBB2222\.zip"):
        refresh_webdav_cache(config, transport=transport)

    objects["AAAA1111.zip"] = b"new-data"
    etags["AAAA1111.zip"] = '"new"'
    fail_second = False
    get_requests.clear()

    summary = refresh_webdav_cache(config, transport=transport)

    assert summary.downloaded == 2
    assert summary.resumed == 0
    assert get_requests == ["AAAA1111.zip", "BBBB2222.zip"]
    assert (config.webdav_dir / "AAAA1111.zip").read_bytes() == b"new-data"


def test_no_prune_keeps_local_remote_shaped_files(zotero_config_factory) -> None:
    config = _configured(zotero_config_factory)
    stale = config.webdav_dir / "STALE999.prop"
    stale.write_bytes(b"keep")
    transport = httpx.MockTransport(lambda _request: httpx.Response(207, content=_multistatus()))

    summary = refresh_webdav_cache(config, prune=False, transport=transport)

    assert summary.deleted == 0
    assert stale.read_bytes() == b"keep"


def test_cross_origin_or_invalid_next_link_is_rejected(zotero_config_factory) -> None:
    config = _configured(zotero_config_factory)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            207,
            headers={"Link": '<https://attacker.example/steal?mk=x>; rel="next"'},
            content=_multistatus(),
        )
    )

    with NutstoreWebDAVClient(config, transport=transport) as client:
        with pytest.raises(ZoteroError, match="outside configured collection") as error:
            client.list_objects()

    assert error.value.code == "invalid_response"


def test_size_mismatch_does_not_replace_existing_cache_file(zotero_config_factory) -> None:
    config = _configured(zotero_config_factory)
    destination = config.webdav_dir / "AAAA1111.zip"
    destination.write_bytes(b"old")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return httpx.Response(
                207,
                content=_multistatus(("AAAA1111.zip", b"advertised", '"new"')),
            )
        return httpx.Response(200, content=b"short")

    with pytest.raises(ZoteroError, match="size changed") as error:
        refresh_webdav_cache(config, transport=httpx.MockTransport(handler))

    assert error.value.code == "invalid_response"
    assert destination.read_bytes() == b"old"
    assert list(config.webdav_dir.glob("*.part")) == []


def test_refresh_requires_external_webdav_credentials(zotero_config_factory) -> None:
    with pytest.raises(ZoteroError, match="WEBDAV_USERNAME") as error:
        refresh_webdav_cache(zotero_config_factory())

    assert error.value.code == "config_error"
