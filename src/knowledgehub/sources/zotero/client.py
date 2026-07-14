"""Read-only Zotero Web API v3 client."""

from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import islice
from typing import Any, Callable, Iterable, Iterator, Mapping
from urllib.parse import urljoin, urlparse

import httpx

from knowledgehub.core.retry import is_retryable_status, parse_retry_after

from .config import ZoteroConfig
from .models import KeyAccess, RemoteVersionChanged, VersionListing, ZoteroError

LOGGER = logging.getLogger(__name__)


class ZoteroClient:
    """A GET-only Zotero client with bounded retries and shared backoff."""

    def __init__(
        self,
        config: ZoteroConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        self.config = config
        self._sleep = sleeper
        self._monotonic = monotonic
        self._random = random_fn
        self._gate_lock = threading.Lock()
        self._not_before = 0.0
        self._origin = _origin(config.api_base_url)
        self._client = httpx.Client(
            base_url=config.api_base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(config.http_timeout_seconds),
            headers={
                "Zotero-API-Key": config.api_key.get_secret_value(),
                "Zotero-API-Version": "3",
                "Accept": "application/json",
                "User-Agent": "KnowledgeHub/0.1 ZoteroSource",
            },
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self) -> "ZoteroClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @property
    def library_prefix(self) -> str:
        if self.config.library_id is None:
            raise ZoteroError("config_error", "Library ID has not been resolved")
        plural = "users" if self.config.library_type == "user" else "groups"
        return f"/{plural}/{self.config.library_id}"

    def verify_key(self) -> KeyAccess:
        response = self._get("/keys/current", endpoint="key_validation")
        payload = _json_object(response, "key_validation")
        try:
            user_id = int(payload["userID"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ZoteroError(
                "invalid_api_key", "Zotero key response did not contain a valid userID"
            ) from exc
        access = payload.get("access")
        if not isinstance(access, Mapping):
            raise ZoteroError("missing_library_permission", "Zotero key has no library access map")

        if self.config.library_type == "user":
            configured = self.config.library_id
            if configured is not None and configured != user_id:
                raise ZoteroError(
                    "user_id_mismatch",
                    f"Configured user library ID {configured} does not match API key user ID {user_id}",
                )
            if not _library_allowed(access.get("user")):
                raise ZoteroError(
                    "missing_library_permission", "API key cannot read the target user library"
                )
            return KeyAccess(user_id=user_id, library_type="user", library_id=user_id)

        group_id = self.config.library_id
        if group_id is None:
            raise ZoteroError("config_error", "Group library ID is required")
        groups = access.get("groups")
        allowed = False
        if isinstance(groups, Mapping):
            allowed = _library_allowed(groups.get(str(group_id))) or _library_allowed(
                groups.get(group_id)
            )
            allowed = allowed or _library_allowed(groups.get("all"))
        if not allowed:
            raise ZoteroError(
                "missing_library_permission", f"API key cannot read group library {group_id}"
            )
        return KeyAccess(user_id=user_id, library_type="group", library_id=group_id)

    def versions(
        self,
        object_type: str,
        *,
        since: int,
        conditional_version: int | None = None,
    ) -> VersionListing:
        endpoint, _key_param = self._object_endpoint(object_type)
        headers = {}
        if conditional_version is not None:
            headers["If-Modified-Since-Version"] = str(conditional_version)
        params: dict[str, str | int] = {"since": since, "format": "versions"}
        if object_type == "item":
            params["includeTrashed"] = 1
        response = self._get(
            f"{self.library_prefix}/{endpoint}",
            params=params,
            headers=headers,
            endpoint=f"{object_type}_versions",
            allowed_status={304},
        )
        if response.status_code == 304:
            version = conditional_version if conditional_version is not None else since
            return VersionListing({}, int(version), not_modified=True)
        payload = _json_object(response, f"{object_type}_versions")
        versions: dict[str, int] = {}
        try:
            for key, value in payload.items():
                versions[str(key)] = int(value)
        except (TypeError, ValueError) as exc:
            raise ZoteroError(
                "invalid_response", f"Invalid {object_type} versions response"
            ) from exc
        return VersionListing(versions, self._library_version(response))

    def fetch_objects(
        self, object_type: str, keys: Iterable[str] | Mapping[str, int]
    ) -> tuple[list[dict[str, Any]], set[int]]:
        expected_versions = dict(keys) if isinstance(keys, Mapping) else None
        key_values = expected_versions if expected_versions is not None else keys
        batches = list(_batched(sorted(set(key_values)), 50))
        if not batches:
            return [], set()
        results: list[dict[str, Any]] = []
        versions: set[int] = set()
        if self.config.api_concurrency == 1 or len(batches) == 1:
            for batch in batches:
                objects, version = self._fetch_batch(object_type, batch)
                results.extend(objects)
                versions.add(version)
        else:
            with ThreadPoolExecutor(max_workers=min(2, self.config.api_concurrency)) as executor:
                futures = [
                    executor.submit(self._fetch_batch, object_type, batch) for batch in batches
                ]
                for future in as_completed(futures):
                    objects, version = future.result()
                    results.extend(objects)
                    versions.add(version)
        results.sort(key=_object_key)
        if expected_versions is not None:
            for value in results:
                data_value = value.get("data")
                data = data_value if isinstance(data_value, Mapping) else value
                key = str(value.get("key") or data.get("key") or "")
                raw_version = value.get("version") or data.get("version")
                if raw_version is None:
                    raise ZoteroError(
                        "invalid_response", f"Remote {object_type} {key} has invalid version"
                    )
                try:
                    object_version = int(raw_version)
                except (TypeError, ValueError) as exc:
                    raise ZoteroError(
                        "invalid_response", f"Remote {object_type} {key} has invalid version"
                    ) from exc
                if object_version != expected_versions[key]:
                    raise ZoteroError(
                        "invalid_response",
                        f"Remote {object_type} {key} version does not match its versions listing",
                    )
        return results, versions

    def deleted(self, *, since: int) -> tuple[dict[str, list[str]], int]:
        response = self._get(
            f"{self.library_prefix}/deleted",
            params={"since": since},
            endpoint="deleted",
        )
        payload = _json_object(response, "deleted")
        result: dict[str, list[str]] = {}
        for kind in ("items", "collections", "searches", "tags"):
            value = payload.get(kind, [])
            if not isinstance(value, list):
                raise ZoteroError("invalid_response", f"Invalid deleted response field: {kind}")
            result[kind] = sorted(str(item) for item in value)
        return result, self._library_version(response)

    def ensure_target_unchanged(self, target_version: int) -> None:
        listing = self.versions(
            "collection", since=target_version, conditional_version=target_version
        )
        if not listing.not_modified and listing.library_version != target_version:
            raise RemoteVersionChanged(target_version, listing.library_version)

    def _fetch_batch(self, object_type: str, keys: list[str]) -> tuple[list[dict[str, Any]], int]:
        endpoint, key_param = self._object_endpoint(object_type)
        params: dict[str, str | int] = {
            key_param: ",".join(keys),
            "format": "json",
            "limit": 50,
        }
        if object_type == "item":
            params["includeTrashed"] = 1
        response = self._get(
            f"{self.library_prefix}/{endpoint}",
            params=params,
            endpoint=f"{object_type}_batch",
            object_keys=keys,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ZoteroError(
                "invalid_response", f"Zotero {object_type} batch returned invalid JSON"
            ) from exc
        if not isinstance(payload, list) or not all(isinstance(value, dict) for value in payload):
            raise ZoteroError("invalid_response", f"Invalid {object_type} batch response")
        if any("data" in value and not isinstance(value["data"], Mapping) for value in payload):
            raise ZoteroError("invalid_response", f"Invalid {object_type} batch object data")
        returned_keys = [_object_key(value) for value in payload]
        if (
            any(not key for key in returned_keys)
            or len(returned_keys) != len(set(returned_keys))
            or set(returned_keys) != set(keys)
        ):
            raise ZoteroError(
                "invalid_response",
                f"Remote {object_type} batch did not return exactly the requested keys",
                context={"object_keys": keys},
            )
        return payload, self._library_version(response)

    @staticmethod
    def _object_endpoint(object_type: str) -> tuple[str, str]:
        if object_type == "item":
            return "items", "itemKey"
        if object_type == "collection":
            return "collections", "collectionKey"
        if object_type == "search":
            return "searches", "searchKey"
        raise ValueError(f"Unsupported object type: {object_type}")

    def _get(
        self,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
        endpoint: str,
        object_keys: Iterable[str] = (),
        allowed_status: set[int] | None = None,
    ) -> httpx.Response:
        if not path.startswith("/"):
            raise ZoteroError("invalid_endpoint", "Zotero API paths must be absolute")
        url = urljoin(self.config.api_base_url.rstrip("/") + "/", path.lstrip("/"))
        if _origin(url) != self._origin:
            raise ZoteroError(
                "invalid_endpoint", "Refusing to send the API key to a different origin"
            )
        allowed = allowed_status or set()
        last_error: BaseException | None = None
        for attempt in range(self.config.max_retries + 1):
            self._wait_for_gate()
            try:
                response = self._client.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                self._sleep(self._exponential_delay(attempt))
                continue

            self._record_backoff(response)
            if 200 <= response.status_code < 300 or response.status_code in allowed:
                self._validate_pagination_links(response)
                return response
            if response.status_code in {401, 403}:
                code = (
                    "invalid_api_key"
                    if endpoint == "key_validation"
                    else "missing_library_permission"
                )
                raise ZoteroError(code, f"Zotero rejected {endpoint} with HTTP 403")
            if not is_retryable_status(response.status_code):
                raise ZoteroError(
                    "http_error",
                    f"Zotero {endpoint} request failed with HTTP {response.status_code}",
                    context={"endpoint": endpoint, "object_keys": list(object_keys)},
                )
            if attempt >= self.config.max_retries:
                raise ZoteroError(
                    "network_error",
                    f"Zotero {endpoint} remained unavailable after {attempt + 1} attempts",
                    retryable=True,
                    context={"endpoint": endpoint, "status": response.status_code},
                )
            header_delay = _header_delay(response)
            delay = header_delay if header_delay is not None else self._exponential_delay(attempt)
            self._sleep(delay)
        raise ZoteroError(
            "network_error",
            f"Zotero {endpoint} request failed after retries",
            retryable=True,
            context={"endpoint": endpoint, "error_type": type(last_error).__name__},
        ) from last_error

    def _library_version(self, response: httpx.Response) -> int:
        raw = response.headers.get("Last-Modified-Version")
        if raw is None:
            raise ZoteroError("invalid_response", "Zotero response omitted Last-Modified-Version")
        try:
            version = int(raw)
        except ValueError as exc:
            raise ZoteroError("invalid_response", "Invalid Last-Modified-Version header") from exc
        if version < 0:
            raise ZoteroError("invalid_response", "Invalid Last-Modified-Version header")
        return version

    def _wait_for_gate(self) -> None:
        while True:
            with self._gate_lock:
                delay = max(0.0, self._not_before - self._monotonic())
            if not delay:
                return
            self._sleep(delay)

    def _record_backoff(self, response: httpx.Response) -> None:
        delay = _header_delay(response)
        if delay is None:
            return
        with self._gate_lock:
            self._not_before = max(self._not_before, self._monotonic() + delay)

    def _exponential_delay(self, attempt: int) -> float:
        return float(min(60.0, (2**attempt) + float(self._random())))

    def _validate_pagination_links(self, response: httpx.Response) -> None:
        try:
            links = response.links.items()
        except (KeyError, ValueError) as exc:
            raise ZoteroError("invalid_response", "Invalid Zotero Link header") from exc
        for relation, link in links:
            # Zotero legitimately emits a cross-origin ``alternate`` link to
            # the human-facing www.zotero.org item page.  Only navigation
            # relations can influence API pagination and therefore need the
            # same-origin restriction.
            if (relation or "").lower() not in {"next", "prev", "first", "last"}:
                continue
            raw_url = link.get("url")
            if not raw_url:
                continue
            absolute = urljoin(str(response.url), str(raw_url))
            if _origin(absolute) != self._origin:
                raise ZoteroError(
                    "invalid_response", "Refusing cross-origin Zotero pagination link"
                )


def assert_target_versions(target: int, observed: Iterable[int]) -> None:
    for version in observed:
        if version != target:
            raise RemoteVersionChanged(target, version)


def _library_allowed(value: Any) -> bool:
    if value is True:
        return True
    return bool(isinstance(value, Mapping) and value.get("library") is True)


def _object_key(value: Mapping[str, Any]) -> str:
    data = value.get("data")
    nested_key = data.get("key") if isinstance(data, Mapping) else None
    return str(value.get("key") or nested_key or "")


def _json_object(response: httpx.Response, endpoint: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ZoteroError("invalid_response", f"Zotero {endpoint} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ZoteroError(
            "invalid_response", f"Zotero {endpoint} returned an unexpected JSON value"
        )
    return payload


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


def _header_delay(response: httpx.Response) -> float | None:
    raw_backoff = response.headers.get("Backoff")
    if raw_backoff:
        try:
            return max(0.0, float(raw_backoff))
        except ValueError:
            pass
    return parse_retry_after(response.headers.get("Retry-After"))


def _batched(values: Iterable[str], size: int) -> Iterator[list[str]]:
    iterator = iter(values)
    while batch := list(islice(iterator, size)):
        yield batch
