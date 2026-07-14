from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from knowledgehub.sources.zotero.client import ZoteroClient, assert_target_versions
from knowledgehub.sources.zotero.models import RemoteVersionChanged, ZoteroError


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _response(
    status: int,
    *,
    json: Any = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(status, json=json, headers=headers)


def test_verify_user_key_uses_v3_headers_and_resolves_user_id(zotero_config_factory) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _response(200, json={"userID": 42, "access": {"user": {"library": True}}})

    with ZoteroClient(
        zotero_config_factory(library_id=None), transport=_transport(handler)
    ) as client:
        access = client.verify_key()

    assert (access.user_id, access.library_type, access.library_id) == (42, "user", 42)
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/keys/current"
    assert seen[0].headers["Zotero-API-Key"] == "test-api-key"
    assert seen[0].headers["Zotero-API-Version"] == "3"


@pytest.mark.parametrize(
    ("payload", "library_id", "code"),
    [
        ({"access": {"user": True}}, 42, "invalid_api_key"),
        ({"userID": 42, "access": {"user": False}}, 42, "missing_library_permission"),
        ({"userID": 99, "access": {"user": True}}, 42, "user_id_mismatch"),
    ],
)
def test_verify_user_key_classifies_access_failures(
    zotero_config_factory,
    payload: dict[str, Any],
    library_id: int,
    code: str,
) -> None:
    transport = _transport(lambda _request: _response(200, json=payload))

    with ZoteroClient(zotero_config_factory(library_id=library_id), transport=transport) as client:
        with pytest.raises(ZoteroError) as error:
            client.verify_key()

    assert error.value.code == code


@pytest.mark.parametrize(
    "groups",
    [{"7": {"library": True}}, {"all": True}],
)
def test_verify_group_key_accepts_specific_or_all_permission(
    zotero_config_factory,
    groups: dict[str, Any],
) -> None:
    config = zotero_config_factory(library_type="group", library_id=7)
    transport = _transport(
        lambda _request: _response(200, json={"userID": 42, "access": {"groups": groups}})
    )

    with ZoteroClient(config, transport=transport) as client:
        access = client.verify_key()

    assert access.library_type == "group"
    assert access.library_id == 7
    assert client.library_prefix == "/groups/7"


def test_verify_group_key_rejects_missing_permission(zotero_config_factory) -> None:
    config = zotero_config_factory(library_type="group", library_id=7)
    transport = _transport(
        lambda _request: _response(
            200,
            json={"userID": 42, "access": {"groups": {"8": {"library": True}}}},
        )
    )

    with ZoteroClient(config, transport=transport) as client:
        with pytest.raises(ZoteroError) as error:
            client.verify_key()

    assert error.value.code == "missing_library_permission"


def test_key_validation_403_is_invalid_api_key(zotero_config_factory) -> None:
    with ZoteroClient(
        zotero_config_factory(max_retries=0),
        transport=_transport(lambda _request: _response(403)),
    ) as client:
        with pytest.raises(ZoteroError) as error:
            client.verify_key()

    assert error.value.code == "invalid_api_key"
    assert "test-api-key" not in str(error.value)


def test_versions_supports_conditional_304(zotero_config_factory) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _response(304)

    with ZoteroClient(zotero_config_factory(), transport=_transport(handler)) as client:
        listing = client.versions("item", since=12, conditional_version=12)

    assert listing.not_modified
    assert listing.library_version == 12
    assert listing.versions == {}
    assert seen[0].headers["If-Modified-Since-Version"] == "12"
    assert seen[0].url.params["since"] == "12"
    assert seen[0].url.params["format"] == "versions"
    assert seen[0].url.params["includeTrashed"] == "1"


def test_versions_parses_object_versions_and_library_version(zotero_config_factory) -> None:
    transport = _transport(
        lambda _request: _response(
            200,
            json={"B": 4, "A": "3"},
            headers={"Last-Modified-Version": "9"},
        )
    )

    with ZoteroClient(zotero_config_factory(), transport=transport) as client:
        listing = client.versions("collection", since=2)

    assert listing.versions == {"B": 4, "A": 3}
    assert listing.library_version == 9
    assert not listing.not_modified


def test_fetch_objects_deduplicates_sorts_and_batches_at_fifty(zotero_config_factory) -> None:
    batch_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys = request.url.params["itemKey"].split(",")
        batch_sizes.append(len(keys))
        assert request.url.params["format"] == "json"
        assert request.url.params["includeTrashed"] == "1"
        return _response(
            200,
            json=[{"key": key, "version": 7, "data": {"key": key}} for key in reversed(keys)],
            headers={"Last-Modified-Version": "7"},
        )

    keys = [f"K{index:03d}" for index in range(105)] + ["K000", "K051"]
    with ZoteroClient(zotero_config_factory(), transport=_transport(handler)) as client:
        objects, versions = client.fetch_objects("item", reversed(keys))

    assert batch_sizes == [50, 50, 5]
    assert [value["key"] for value in objects] == sorted(set(keys))
    assert versions == {7}


def test_fetch_objects_empty_input_performs_no_request(zotero_config_factory) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("transport should not be called")

    with ZoteroClient(zotero_config_factory(), transport=_transport(handler)) as client:
        assert client.fetch_objects("item", []) == ([], set())


def test_fetch_objects_rejects_batch_missing_a_requested_key(zotero_config_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        requested = request.url.params["itemKey"].split(",")
        assert requested == ["A", "B"]
        return _response(
            200,
            json=[{"key": "A", "version": 3, "data": {"key": "A", "version": 3}}],
            headers={"Last-Modified-Version": "3"},
        )

    with ZoteroClient(
        zotero_config_factory(max_retries=0), transport=_transport(handler)
    ) as client:
        with pytest.raises(ZoteroError, match="exactly the requested keys") as error:
            client.fetch_objects("item", {"A": 3, "B": 3})

    assert error.value.code == "invalid_response"
    assert error.value.context == {"object_keys": ["A", "B"]}


def test_fetch_objects_rejects_version_mismatch_with_versions_listing(
    zotero_config_factory,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _response(
            200,
            json=[{"key": "A", "version": 4, "data": {"key": "A", "version": 4}}],
            headers={"Last-Modified-Version": "4"},
        )

    with ZoteroClient(
        zotero_config_factory(max_retries=0), transport=_transport(handler)
    ) as client:
        with pytest.raises(ZoteroError, match="does not match its versions listing") as error:
            client.fetch_objects("item", {"A": 3})

    assert error.value.code == "invalid_response"


def test_fetch_objects_rejects_missing_library_version_header(zotero_config_factory) -> None:
    transport = _transport(
        lambda _request: _response(
            200,
            json=[{"key": "A", "version": 3, "data": {"key": "A", "version": 3}}],
        )
    )

    with ZoteroClient(zotero_config_factory(max_retries=0), transport=transport) as client:
        with pytest.raises(ZoteroError, match="omitted Last-Modified-Version") as error:
            client.fetch_objects("item", {"A": 3})

    assert error.value.code == "invalid_response"


def test_fetch_objects_classifies_invalid_json_instead_of_leaking_decoder_error(
    zotero_config_factory,
) -> None:
    transport = _transport(
        lambda _request: httpx.Response(
            200,
            content=b"{not-json",
            headers={"Last-Modified-Version": "3"},
        )
    )

    with ZoteroClient(zotero_config_factory(max_retries=0), transport=transport) as client:
        with pytest.raises(ZoteroError, match="invalid JSON") as error:
            client.fetch_objects("item", {"A": 3})

    assert error.value.code == "invalid_response"


def test_fetch_objects_classifies_non_object_nested_data_as_invalid_response(
    zotero_config_factory,
) -> None:
    transport = _transport(
        lambda _request: _response(
            200,
            json=[{"key": "A", "data": None, "version": 3}],
            headers={"Last-Modified-Version": "3"},
        )
    )

    with ZoteroClient(zotero_config_factory(max_retries=0), transport=transport) as client:
        with pytest.raises(ZoteroError, match="batch object data") as error:
            client.fetch_objects("item", {"A": 3})

    assert error.value.code == "invalid_response"


def test_cross_origin_pagination_link_is_rejected_without_following_it(
    zotero_config_factory,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _response(
            200,
            json={"userID": 42, "access": {"user": True}},
            headers={"Link": '<https://attacker.example/next>; rel="next"'},
        )

    with ZoteroClient(
        zotero_config_factory(max_retries=0), transport=_transport(handler)
    ) as client:
        with pytest.raises(ZoteroError, match="cross-origin Zotero pagination link") as error:
            client.verify_key()

        assert error.value.code == "invalid_response"
    assert [request.url.host for request in seen] == ["api.zotero.org"]


def test_cross_origin_human_facing_alternate_link_is_allowed(zotero_config_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"A": 1},
            headers={
                "Link": '<https://www.zotero.org/users/123/items>; rel="alternate"',
                "Last-Modified-Version": "1",
            },
            request=request,
        )

    with ZoteroClient(
        zotero_config_factory(max_retries=0), transport=httpx.MockTransport(handler)
    ) as client:
        listing = client.versions("item", since=0)
    assert listing.library_version == 1


@pytest.mark.parametrize(
    ("status", "headers", "expected_delay"),
    [
        (429, {"Backoff": "7", "Retry-After": "99"}, 7.0),
        (503, {"Retry-After": "3"}, 3.0),
    ],
)
def test_retry_headers_take_precedence_over_exponential_backoff(
    zotero_config_factory,
    fake_clock,
    status: int,
    headers: dict[str, str],
    expected_delay: float,
) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _response(status, headers=headers)
        return _response(200, json={"userID": 42, "access": {"user": True}})

    with ZoteroClient(
        zotero_config_factory(max_retries=1),
        transport=_transport(handler),
        sleeper=fake_clock.sleep,
        monotonic=fake_clock.monotonic,
        random_fn=lambda: 0.25,
    ) as client:
        client.verify_key()

    assert attempts == 2
    assert fake_clock.sleeps == [expected_delay]


def test_transport_timeout_retries_with_bounded_exponential_delay(
    zotero_config_factory,
    fake_clock,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return _response(200, json={"userID": 42, "access": {"user": True}})

    with ZoteroClient(
        zotero_config_factory(max_retries=1),
        transport=_transport(handler),
        sleeper=fake_clock.sleep,
        monotonic=fake_clock.monotonic,
        random_fn=lambda: 0.25,
    ) as client:
        client.verify_key()

    assert attempts == 2
    assert fake_clock.sleeps == [1.25]


@pytest.mark.parametrize("failure", [429, 503])
def test_retryable_http_failure_is_classified_after_exhaustion(
    zotero_config_factory,
    fake_clock,
    failure: int,
) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _response(failure)

    with ZoteroClient(
        zotero_config_factory(max_retries=1),
        transport=_transport(handler),
        sleeper=fake_clock.sleep,
        monotonic=fake_clock.monotonic,
        random_fn=lambda: 0,
    ) as client:
        with pytest.raises(ZoteroError) as error:
            client.verify_key()

    assert attempts == 2
    assert error.value.code == "network_error"
    assert error.value.retryable
    assert error.value.context["status"] == failure


def test_non_retryable_http_error_is_not_retried(zotero_config_factory) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _response(404)

    with ZoteroClient(
        zotero_config_factory(max_retries=3),
        transport=_transport(handler),
    ) as client:
        with pytest.raises(ZoteroError) as error:
            client.versions("item", since=0)

    assert attempts == 1
    assert error.value.code == "http_error"


def test_deleted_normalizes_fields_and_checks_library_version(zotero_config_factory) -> None:
    transport = _transport(
        lambda request: _response(
            200,
            json={"items": ["B", "A"], "collections": ["C"], "searches": [], "tags": []},
            headers={"Last-Modified-Version": "14"},
        )
    )

    with ZoteroClient(zotero_config_factory(), transport=transport) as client:
        deleted, version = client.deleted(since=9)

    assert deleted == {"items": ["A", "B"], "collections": ["C"], "searches": [], "tags": []}
    assert version == 14


def test_target_version_checks_detect_remote_change(zotero_config_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["If-Modified-Since-Version"] == "9"
        return _response(200, json={"NEW": 10}, headers={"Last-Modified-Version": "10"})

    with ZoteroClient(zotero_config_factory(), transport=_transport(handler)) as client:
        with pytest.raises(RemoteVersionChanged) as error:
            client.ensure_target_unchanged(9)

    assert error.value.context == {"expected": 9, "observed": 10}
    with pytest.raises(RemoteVersionChanged):
        assert_target_versions(9, [9, 9, 10])
    assert_target_versions(9, [9, 9])


def test_invalid_response_shapes_and_version_header_are_rejected(zotero_config_factory) -> None:
    responses = iter(
        [
            _response(200, json=[]),
            _response(200, json={"A": 1}),
            _response(200, json={"A": 1}, headers={"Last-Modified-Version": "NaN"}),
            _response(200, json={"items": "not-a-list"}, headers={"Last-Modified-Version": "1"}),
        ]
    )
    transport = _transport(lambda _request: next(responses))

    with ZoteroClient(zotero_config_factory(), transport=transport) as client:
        with pytest.raises(ZoteroError, match="unexpected JSON value"):
            client.versions("item", since=0)
        with pytest.raises(ZoteroError, match="omitted Last-Modified-Version"):
            client.versions("item", since=0)
        with pytest.raises(ZoteroError, match="Last-Modified-Version"):
            client.versions("item", since=0)
        with pytest.raises(ZoteroError, match="Invalid deleted response field"):
            client.deleted(since=0)
