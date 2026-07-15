"""Paginated Nutstore WebDAV mirror refresh.

Nutstore paginates large ``Depth: 1`` PROPFIND responses with an HTTP
``Link: <...>; rel="next"`` header.  This client follows that extension while
keeping pagination and object downloads on the configured HTTPS collection.
Files are streamed to sibling temporary files and atomically replaced only
after their advertised size has been verified.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import stat
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx

from knowledgehub.core.atomic import atomic_write_json, fsync_directory, safe_unlink
from knowledgehub.core.locking import FileLock
from knowledgehub.core.retry import RetryPolicy, compute_retry_delay, is_retryable_status

from .config import ZoteroConfig
from .models import ZoteroError

LOGGER = logging.getLogger(__name__)

_DAV = "{DAV:}"
_CACHE_INDEX = ".knowledgehub-webdav-index.json"
_CACHE_PROGRESS_INDEX = ".knowledgehub-webdav-progress.json"
_CACHE_LOCK = ".knowledgehub-webdav-cache.lock"
_OBJECT_NAME = re.compile(r"^[A-Za-z0-9_-]+\.(?:zip|prop)$", re.IGNORECASE)
_PROPFIND_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:getcontentlength/><d:getlastmodified/>
<d:getetag/><d:resourcetype/></d:prop></d:propfind>"""


@dataclass(frozen=True, slots=True)
class RemoteObject:
    """One supported direct child of the configured WebDAV collection."""

    name: str
    url: str
    size: int | None
    etag: str | None
    last_modified: str | None

    def index_value(self) -> dict[str, Any]:
        return {
            "etag": self.etag,
            "last_modified": self.last_modified,
            "size": self.size,
            "url": self.url,
        }


@dataclass(frozen=True, slots=True)
class CacheRefreshSummary:
    pages: int
    remote_objects: int
    downloaded: int
    adopted: int
    unchanged: int
    resumed: int
    deleted: int
    bytes_downloaded: int
    cache_dir: str
    prune: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "bytes_downloaded": self.bytes_downloaded,
            "cache_dir": self.cache_dir,
            "deleted": self.deleted,
            "downloaded": self.downloaded,
            "adopted": self.adopted,
            "pages": self.pages,
            "prune": self.prune,
            "remote_objects": self.remote_objects,
            "resumed": self.resumed,
            "status": "success",
            "unchanged": self.unchanged,
        }


class NutstoreWebDAVClient:
    """Read-only HTTP client for Nutstore's paginated WebDAV collection."""

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
        self.base_url = config.webdav_url
        self._sleep = sleeper
        self._monotonic = monotonic
        self._random = random_fn
        self._last_request_started_at: float | None = None
        self._policy = RetryPolicy(
            max_retries=config.max_retries,
            max_delay_seconds=config.webdav_max_retry_delay_seconds,
        )
        self._client = httpx.Client(
            auth=httpx.BasicAuth(
                config.webdav_username.get_secret_value(),
                config.webdav_password.get_secret_value(),
            ),
            timeout=httpx.Timeout(config.http_timeout_seconds),
            headers={"User-Agent": "KnowledgeHub/0.1 NutstoreWebDAVPaginator"},
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self) -> "NutstoreWebDAVClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self._client.close()

    def list_objects(self) -> tuple[dict[str, RemoteObject], int]:
        """Follow every same-collection ``rel=next`` page and merge objects."""

        objects: dict[str, RemoteObject] = {}
        seen_pages: set[str] = set()
        page_url: str | None = self.base_url
        pages = 0
        while page_url is not None:
            if page_url in seen_pages:
                raise ZoteroError("invalid_response", "WebDAV pagination cycle detected")
            if pages >= self.config.webdav_page_limit:
                raise ZoteroError(
                    "invalid_response",
                    f"WebDAV pagination exceeded {self.config.webdav_page_limit} pages",
                )
            seen_pages.add(page_url)
            response = self._request(
                "PROPFIND",
                page_url,
                headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
                content=_PROPFIND_BODY,
                endpoint="listing",
                expected_status=207,
            )
            pages += 1
            for remote in self._parse_multistatus(response.content):
                previous = objects.get(remote.name)
                if previous is not None and previous != remote:
                    raise ZoteroError(
                        "invalid_response",
                        f"Conflicting duplicate WebDAV object: {remote.name}",
                    )
                objects[remote.name] = remote
            page_url = self._next_page(response)
        return objects, pages

    def download(self, remote: RemoteObject, destination: Path) -> int:
        """Stream one object and atomically publish it below the cache root."""

        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
        for attempt in range(self.config.max_retries + 1):
            try:
                self._wait_for_request_slot()
                with self._client.stream("GET", remote.url) as response:
                    if response.status_code == 200:
                        _validate_download_headers(remote, response.headers)
                        written = self._write_stream(response, temporary)
                        if remote.size is not None and written != remote.size:
                            raise ZoteroError(
                                "invalid_response",
                                f"WebDAV object size changed while downloading {remote.name}",
                                context={
                                    "object": remote.name,
                                    "expected_size": remote.size,
                                    "received_size": written,
                                },
                            )
                        os.replace(temporary, destination)
                        fsync_directory(destination.parent)
                        return written
                    if response.status_code in {401, 403}:
                        raise ZoteroError(
                            "webdav_auth_error",
                            f"WebDAV rejected download of {remote.name} with HTTP {response.status_code}",
                        )
                    if not is_retryable_status(response.status_code, self._policy):
                        raise ZoteroError(
                            "http_error",
                            f"WebDAV download of {remote.name} failed with HTTP {response.status_code}",
                        )
                    if attempt >= self.config.max_retries:
                        raise ZoteroError(
                            "network_error",
                            f"WebDAV download of {remote.name} remained unavailable after {attempt + 1} attempts",
                            retryable=True,
                        )
                    self._sleep(
                        self._retry_delay(
                            response.headers,
                            attempt + 1,
                            status_code=response.status_code,
                        )
                    )
            except httpx.TransportError as exc:
                if attempt >= self.config.max_retries:
                    raise ZoteroError(
                        "network_error",
                        f"WebDAV download of {remote.name} failed after {attempt + 1} attempts",
                        retryable=True,
                        context={"object": remote.name, "error_type": type(exc).__name__},
                    ) from exc
                self._sleep(self._retry_delay({}, attempt + 1))
            finally:
                temporary.unlink(missing_ok=True)
        raise AssertionError("unreachable WebDAV download retry state")

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        content: bytes,
        endpoint: str,
        expected_status: int,
    ) -> httpx.Response:
        for attempt in range(self.config.max_retries + 1):
            try:
                self._wait_for_request_slot()
                response = self._client.request(method, url, headers=headers, content=content)
            except httpx.TransportError as exc:
                if attempt >= self.config.max_retries:
                    raise ZoteroError(
                        "network_error",
                        f"WebDAV {endpoint} failed after {attempt + 1} attempts",
                        retryable=True,
                        context={"endpoint": endpoint, "error_type": type(exc).__name__},
                    ) from exc
                self._sleep(self._retry_delay({}, attempt + 1))
                continue
            if response.status_code == expected_status:
                return response
            if response.status_code in {401, 403}:
                raise ZoteroError(
                    "webdav_auth_error",
                    f"WebDAV rejected {endpoint} with HTTP {response.status_code}",
                )
            if not is_retryable_status(response.status_code, self._policy):
                raise ZoteroError(
                    "http_error",
                    f"WebDAV {endpoint} failed with HTTP {response.status_code}",
                    context={"endpoint": endpoint},
                )
            if attempt >= self.config.max_retries:
                raise ZoteroError(
                    "network_error",
                    f"WebDAV {endpoint} remained unavailable after {attempt + 1} attempts",
                    retryable=True,
                    context={"endpoint": endpoint, "status": response.status_code},
                )
            self._sleep(
                self._retry_delay(
                    response.headers,
                    attempt + 1,
                    status_code=response.status_code,
                )
            )
        raise AssertionError("unreachable WebDAV request retry state")

    def _retry_delay(
        self,
        headers: Mapping[str, str],
        retry_number: int,
        *,
        status_code: int | None = None,
    ) -> float:
        delay = compute_retry_delay(
            headers,
            retry_number,
            policy=self._policy,
            random_value=self._random(),
        )
        if status_code in {429, 503}:
            delay = max(delay, self.config.webdav_retry_cooldown_seconds)
            LOGGER.warning(
                "WebDAV HTTP %d triggered %.1f-second cooldown before retry %d/%d",
                status_code,
                min(delay, self.config.webdav_max_retry_delay_seconds),
                retry_number,
                self.config.max_retries,
            )
        return min(delay, self.config.webdav_max_retry_delay_seconds)

    def _wait_for_request_slot(self) -> None:
        interval = self.config.webdav_request_interval_seconds
        now = self._monotonic()
        if self._last_request_started_at is not None:
            remaining = interval - (now - self._last_request_started_at)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last_request_started_at = now

    def _write_stream(self, response: httpx.Response, temporary: Path) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        written = 0
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                for chunk in response.iter_bytes():
                    stream.write(chunk)
                    written += len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        return written

    def _parse_multistatus(self, payload: bytes) -> tuple[RemoteObject, ...]:
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise ZoteroError("invalid_response", "WebDAV returned malformed XML") from exc
        if root.tag != f"{_DAV}multistatus":
            raise ZoteroError("invalid_response", "WebDAV response is not DAV:multistatus")
        result: list[RemoteObject] = []
        for item in root.findall(f"{_DAV}response"):
            href = item.findtext(f"{_DAV}href")
            if not href:
                raise ZoteroError("invalid_response", "WebDAV response omitted DAV:href")
            prop = _successful_prop(item)
            if prop is None or prop.find(f"{_DAV}resourcetype/{_DAV}collection") is not None:
                continue
            resolved = self._object_from_href(href, prop)
            if resolved is not None:
                result.append(resolved)
        return tuple(result)

    def _object_from_href(self, href: str, prop: ET.Element) -> RemoteObject | None:
        absolute = urljoin(self.base_url, href)
        parsed = urlsplit(absolute)
        if _origin(absolute) != _origin(self.base_url) or parsed.username or parsed.password:
            raise ZoteroError("invalid_response", "Refusing cross-origin WebDAV object URL")
        if parsed.query or parsed.fragment:
            raise ZoteroError("invalid_response", "WebDAV object URL contains query or fragment")
        base_path = unquote(urlsplit(self.base_url).path)
        object_path = unquote(parsed.path)
        if object_path.rstrip("/") == base_path.rstrip("/"):
            return None
        if not object_path.startswith(base_path):
            raise ZoteroError("invalid_response", "WebDAV object escaped the configured collection")
        name = object_path[len(base_path) :]
        if not name or "/" in name or "\\" in name or not _OBJECT_NAME.fullmatch(name):
            return None
        raw_size = prop.findtext(f"{_DAV}getcontentlength")
        try:
            size = int(raw_size) if raw_size is not None and raw_size != "" else None
        except ValueError as exc:
            raise ZoteroError(
                "invalid_response", f"Invalid WebDAV content length for {name}"
            ) from exc
        if size is not None and size < 0:
            raise ZoteroError("invalid_response", f"Invalid WebDAV content length for {name}")
        return RemoteObject(
            name=name,
            url=absolute,
            size=size,
            etag=_clean_property(prop.findtext(f"{_DAV}getetag")),
            last_modified=_clean_property(prop.findtext(f"{_DAV}getlastmodified")),
        )

    def _next_page(self, response: httpx.Response) -> str | None:
        candidates: list[str] = []
        for header in response.headers.get_list("Link"):
            for match in re.finditer(r"<([^>]*)>\s*((?:;[^,<]*)*)", header):
                rel = re.search(
                    r"(?:^|;)\s*rel\s*=\s*(?:\"([^\"]*)\"|([^;\s,]+))",
                    match.group(2),
                    re.IGNORECASE,
                )
                relations = (rel.group(1) or rel.group(2)).lower().split() if rel else []
                if "next" in relations:
                    candidates.append(match.group(1))
        if not candidates:
            return None
        if len(set(candidates)) != 1:
            raise ZoteroError("invalid_response", "WebDAV returned conflicting next links")
        absolute = urljoin(str(response.url), candidates[0])
        parsed = urlsplit(absolute)
        if (
            _origin(absolute) != _origin(self.base_url)
            or parsed.username
            or parsed.password
            or parsed.fragment
            or unquote(parsed.path) != unquote(urlsplit(self.base_url).path)
        ):
            raise ZoteroError(
                "invalid_response", "Refusing WebDAV pagination outside configured collection"
            )
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) != {"mk"} or len(query["mk"]) != 1 or not query["mk"][0]:
            raise ZoteroError("invalid_response", "Invalid Nutstore WebDAV pagination marker")
        return absolute


def refresh_webdav_cache(
    config: ZoteroConfig,
    *,
    prune: bool = True,
    adopt_existing: bool = False,
    transport: httpx.BaseTransport | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    random_fn: Callable[[], float] = random.random,
) -> CacheRefreshSummary:
    """Refresh the local ZIP/PROP mirror after a complete paginated listing."""

    config.require_webdav_credentials()
    cache_root = config.prepare_webdav_cache()
    with FileLock(cache_root / _CACHE_LOCK, sync_id=f"webdav-{uuid.uuid4().hex}"):
        progress_path = cache_root / _CACHE_PROGRESS_INDEX
        progress = _load_index(
            progress_path,
            config.webdav_url,
            index_description="WebDAV cache progress index",
            warn_if_missing=False,
        )
        previous = _load_index(
            cache_root / _CACHE_INDEX,
            config.webdav_url,
            warn_if_missing=not progress,
        )
        if progress:
            LOGGER.info(
                "resuming WebDAV cache refresh from %d checkpointed objects",
                len(progress),
            )
        if config.webdav_request_interval_seconds > 0:
            LOGGER.info(
                "pacing WebDAV request starts at a minimum %.3f-second interval",
                config.webdav_request_interval_seconds,
            )
        with NutstoreWebDAVClient(
            config,
            transport=transport,
            sleeper=sleeper,
            monotonic=monotonic,
            random_fn=random_fn,
        ) as client:
            try:
                remote_objects, pages = client.list_objects()
            except ZoteroError as exc:
                _add_progress_context(exc, progress_path, progress)
                raise
            total_objects = len(remote_objects)
            LOGGER.info(
                "listed %d remote objects across %d WebDAV page(s)",
                total_objects,
                pages,
            )
            downloaded = 0
            adopted = 0
            unchanged = 0
            resumed = 0
            bytes_downloaded = 0
            current_index: dict[str, dict[str, Any]] = {}
            for processed, name in enumerate(sorted(remote_objects), start=1):
                remote = remote_objects[name]
                destination = cache_root / name
                if _is_unchanged(destination, remote, previous.get(name)):
                    unchanged += 1
                elif _is_unchanged(destination, remote, progress.get(name)):
                    resumed += 1
                elif (
                    adopt_existing
                    and previous.get(name) is None
                    and progress.get(name) is None
                    and _can_adopt_existing(destination, remote)
                ):
                    adopted += 1
                else:
                    try:
                        received = client.download(remote, destination)
                    except ZoteroError as exc:
                        _add_progress_context(exc, progress_path, progress)
                        raise
                    bytes_downloaded += received
                    downloaded += 1
                    progress[name] = remote.index_value()
                    _write_index(progress_path, config.webdav_url, progress)
                current_index[name] = remote.index_value()
                LOGGER.info(
                    "WebDAV cache progress %d/%d downloaded=%d adopted=%d resumed=%d "
                    "unchanged=%d object=%s",
                    processed,
                    total_objects,
                    downloaded,
                    adopted,
                    resumed,
                    unchanged,
                    name,
                )

        deleted = _prune_cache(cache_root, set(remote_objects)) if prune else 0
        _write_index(cache_root / _CACHE_INDEX, config.webdav_url, current_index)
        safe_unlink(progress_path, root=cache_root, missing_ok=True)
        return CacheRefreshSummary(
            pages=pages,
            remote_objects=len(remote_objects),
            downloaded=downloaded,
            adopted=adopted,
            unchanged=unchanged,
            resumed=resumed,
            deleted=deleted,
            bytes_downloaded=bytes_downloaded,
            cache_dir=str(cache_root),
            prune=prune,
        )


def _successful_prop(response: ET.Element) -> ET.Element | None:
    for propstat in response.findall(f"{_DAV}propstat"):
        status_text = (propstat.findtext(f"{_DAV}status") or "").upper()
        if " 200 " in status_text:
            return propstat.find(f"{_DAV}prop")
    return None


def _clean_property(value: str | None) -> str | None:
    cleaned = value.strip() if value else ""
    return cleaned or None


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


def _load_index(
    path: Path,
    expected_base_url: str,
    *,
    index_description: str = "WebDAV cache index",
    warn_if_missing: bool = True,
) -> dict[str, Mapping[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if warn_if_missing:
            LOGGER.warning("ignoring absent %s", index_description, extra={"path": str(path)})
        return {}
    except (OSError, UnicodeError, ValueError, TypeError):
        LOGGER.warning("ignoring invalid %s", index_description, extra={"path": str(path)})
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("base_url") != expected_base_url
        or not isinstance(payload.get("objects"), dict)
    ):
        LOGGER.warning("ignoring invalid %s", index_description, extra={"path": str(path)})
        return {}
    return {
        str(name): value
        for name, value in payload["objects"].items()
        if _OBJECT_NAME.fullmatch(str(name)) and isinstance(value, Mapping)
    }


def _write_index(
    path: Path,
    base_url: str,
    objects: Mapping[str, Mapping[str, Any]],
) -> None:
    atomic_write_json(
        path,
        {
            "base_url": base_url,
            "objects": objects,
            "schema_version": 1,
        },
        mode=0o600,
    )


def _add_progress_context(
    error: ZoteroError,
    path: Path,
    objects: Mapping[str, Mapping[str, Any]],
) -> None:
    if not objects:
        return
    error.context.setdefault("checkpointed_objects", len(objects))
    error.context.setdefault("progress_index", str(path))


def _is_unchanged(path: Path, remote: RemoteObject, previous: Mapping[str, Any] | None) -> bool:
    if previous is None or dict(previous) != remote.index_value():
        return False
    if remote.etag is None and remote.last_modified is None:
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return False
    return remote.size is None or info.st_size == remote.size


def _can_adopt_existing(path: Path, remote: RemoteObject) -> bool:
    """Return whether an explicitly trusted unindexed local object is shape-compatible."""

    if remote.size is None:
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_size == remote.size
    )


def _validate_download_headers(remote: RemoteObject, headers: Mapping[str, str]) -> None:
    response_etag = _clean_property(headers.get("ETag"))
    response_modified = _clean_property(headers.get("Last-Modified"))
    if remote.etag is not None and response_etag is not None and remote.etag != response_etag:
        raise ZoteroError(
            "invalid_response", f"WebDAV object changed before download: {remote.name}"
        )
    if (
        remote.last_modified is not None
        and response_modified is not None
        and remote.last_modified != response_modified
    ):
        raise ZoteroError(
            "invalid_response", f"WebDAV object changed before download: {remote.name}"
        )


def _prune_cache(cache_root: Path, remote_names: set[str]) -> int:
    deleted = 0
    for entry in cache_root.iterdir():
        if not _OBJECT_NAME.fullmatch(entry.name) or entry.name in remote_names:
            continue
        mode = entry.lstat().st_mode
        if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
            raise ZoteroError(
                "cache_error", f"Refusing to prune directory from WebDAV cache: {entry.name}"
            )
        safe_unlink(entry, root=cache_root)
        deleted += 1
    return deleted


__all__ = [
    "CacheRefreshSummary",
    "NutstoreWebDAVClient",
    "RemoteObject",
    "refresh_webdav_cache",
]
