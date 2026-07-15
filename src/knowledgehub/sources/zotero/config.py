"""Configuration loading and validation for the Zotero source."""

from __future__ import annotations

import math
import os
import stat
import uuid
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import yaml

from .models import ZoteroError


class SecretValue:
    """A small secret wrapper whose string representations never reveal the value."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __str__(self) -> str:
        return "********"

    def __repr__(self) -> str:
        return "SecretValue('********')"


@dataclass(frozen=True)
class ZoteroConfig:
    api_key: SecretValue = field(default_factory=lambda: SecretValue(""))
    library_type: str = "user"
    library_id: int | None = None
    api_base_url: str = "https://api.zotero.org"
    webdav_url: str = "https://dav.jianguoyun.com/dav/zotero/"
    webdav_username: SecretValue = field(default_factory=lambda: SecretValue(""))
    webdav_password: SecretValue = field(default_factory=lambda: SecretValue(""))
    webdav_page_limit: int = 10_000
    webdav_request_interval_seconds: float = 2.0
    webdav_retry_cooldown_seconds: float = 900.0
    webdav_max_retry_delay_seconds: float = 1800.0
    webdav_adopt_existing: bool = False
    webdav_prune: bool = True
    webdav_dir: Path = Path("/data/KnowledgeHub/zotero_cache")
    data_dir: Path = Path("/data/KnowledgeHub/zotero")
    http_timeout_seconds: float = 30.0
    max_retries: int = 5
    sync_max_retries: int = 3
    api_concurrency: int = 2
    zip_stability_interval_seconds: float = 10.0
    zip_stability_check_count: int = 2
    enable_streaming: bool = False
    poll_interval_seconds: int = 300
    mapping_validation_sample_size: int = 20
    attachment_scan_on_304: bool = True
    metadata_changes_require_chunking: bool = False
    log_level: str = "INFO"

    @classmethod
    def load(
        cls,
        config_path: Path | str | None = None,
        *,
        default_path: Path | str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "ZoteroConfig":
        values: dict[str, Any] = {}
        if default_path is not None and Path(default_path).exists():
            values.update(_read_yaml(Path(default_path)))
        if config_path is not None:
            path = Path(config_path)
            if not path.is_file():
                raise ZoteroError("config_error", f"Configuration file does not exist: {path}")
            values.update(_read_yaml(path))

        env = os.environ if environ is None else environ
        for env_name, field_name in _ENV_FIELDS.items():
            if env_name in env:
                values[field_name] = env[env_name]

        allowed = {item.name for item in fields(cls)}
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ZoteroError(
                "config_error", f"Unknown Zotero configuration keys: {', '.join(unknown)}"
            )
        converted = {name: _convert(name, value) for name, value in values.items()}
        return cls(**converted).validate_static()

    def with_library_id(self, library_id: int) -> "ZoteroConfig":
        return replace(self, library_id=library_id)

    def validate_static(self) -> "ZoteroConfig":
        if self.library_type not in {"user", "group"}:
            raise ZoteroError(
                "unsupported_library_type", f"Unsupported library type: {self.library_type}"
            )
        if self.library_id is not None and self.library_id <= 0:
            raise ZoteroError("config_error", "ZOTERO_LIBRARY_ID must be a positive integer")
        if self.library_type == "group" and self.library_id is None:
            raise ZoteroError("config_error", "ZOTERO_LIBRARY_ID is required for group libraries")
        if not self.api_key:
            raise ZoteroError("config_error", "ZOTERO_API_KEY is required")
        try:
            parsed = urlparse(self.api_base_url)
            # Accessing port performs urllib's range and syntax validation.
            port = parsed.port
        except ValueError as exc:
            raise ZoteroError(
                "config_error",
                "ZOTERO_API_BASE_URL must be a valid HTTPS origin",
            ) from exc
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ZoteroError(
                "config_error",
                "ZOTERO_API_BASE_URL must be an HTTPS origin without credentials or query",
            )
        try:
            webdav = urlparse(self.webdav_url)
            webdav_port = webdav.port
        except ValueError as exc:
            raise ZoteroError(
                "config_error", "ZOTERO_WEBDAV_URL must be a valid HTTPS collection URL"
            ) from exc
        if (
            webdav.scheme != "https"
            or not webdav.netloc
            or webdav.username
            or webdav.password
            or webdav.query
            or webdav.fragment
            or not webdav.path.endswith("/")
            or (webdav_port is not None and not 1 <= webdav_port <= 65535)
        ):
            raise ZoteroError(
                "config_error",
                "ZOTERO_WEBDAV_URL must be an HTTPS collection URL ending in / without credentials or query",
            )
        if not math.isfinite(self.http_timeout_seconds) or self.http_timeout_seconds <= 0:
            raise ZoteroError("config_error", "HTTP timeout must be positive")
        if self.max_retries < 0 or self.sync_max_retries < 0:
            raise ZoteroError("config_error", "Retry counts cannot be negative")
        if self.webdav_page_limit <= 0:
            raise ZoteroError("config_error", "ZOTERO_WEBDAV_PAGE_LIMIT must be positive")
        if (
            not math.isfinite(self.webdav_request_interval_seconds)
            or self.webdav_request_interval_seconds < 0
        ):
            raise ZoteroError(
                "config_error", "ZOTERO_WEBDAV_REQUEST_INTERVAL_SECONDS must be non-negative"
            )
        if (
            not math.isfinite(self.webdav_retry_cooldown_seconds)
            or self.webdav_retry_cooldown_seconds < 0
        ):
            raise ZoteroError(
                "config_error", "ZOTERO_WEBDAV_RETRY_COOLDOWN_SECONDS must be non-negative"
            )
        if (
            not math.isfinite(self.webdav_max_retry_delay_seconds)
            or self.webdav_max_retry_delay_seconds < 0
        ):
            raise ZoteroError(
                "config_error", "ZOTERO_WEBDAV_MAX_RETRY_DELAY_SECONDS must be non-negative"
            )
        if self.webdav_retry_cooldown_seconds > self.webdav_max_retry_delay_seconds:
            raise ZoteroError(
                "config_error",
                "ZOTERO_WEBDAV_RETRY_COOLDOWN_SECONDS must not exceed "
                "ZOTERO_WEBDAV_MAX_RETRY_DELAY_SECONDS",
            )
        if not 1 <= self.api_concurrency <= 4:
            raise ZoteroError("config_error", "ZOTERO_API_CONCURRENCY must be between 1 and 4")
        if (
            self.zip_stability_check_count < 2
            or not math.isfinite(self.zip_stability_interval_seconds)
            or self.zip_stability_interval_seconds < 0
        ):
            raise ZoteroError(
                "config_error", "ZIP stability requires at least two non-negative interval checks"
            )
        if self.poll_interval_seconds <= 0 or self.mapping_validation_sample_size <= 0:
            raise ZoteroError(
                "config_error", "Polling interval and mapping sample size must be positive"
            )
        if self.log_level.upper() not in {
            "CRITICAL",
            "ERROR",
            "WARNING",
            "INFO",
            "DEBUG",
            "NOTSET",
        }:
            raise ZoteroError("config_error", f"Invalid ZOTERO_LOG_LEVEL: {self.log_level}")
        if self.enable_streaming:
            raise ZoteroError(
                "streaming_not_implemented", "Zotero Streaming API is not implemented in v1"
            )
        return self

    def require_webdav_credentials(self) -> None:
        """Validate secrets required only by the remote cache refresh command."""

        if not self.webdav_username or not self.webdav_password:
            raise ZoteroError(
                "config_error",
                "ZOTERO_WEBDAV_USERNAME and ZOTERO_WEBDAV_PASSWORD are required for refresh-cache",
            )

    def prepare_webdav_cache(self) -> Path:
        """Create and validate the writable, disposable WebDAV mirror root."""

        cache = self.webdav_dir.expanduser()
        try:
            if os.path.lexists(cache) and (
                cache.is_symlink() or not stat.S_ISDIR(cache.lstat().st_mode)
            ):
                raise ZoteroError("config_error", f"WebDAV cache is not a real directory: {cache}")
            cache.mkdir(parents=True, exist_ok=True, mode=0o700)
            cache_real = cache.resolve(strict=True)
            data_real = self.data_dir.expanduser().resolve(strict=False)
            if (
                cache_real == data_real
                or cache_real in data_real.parents
                or data_real in cache_real.parents
            ):
                raise ZoteroError(
                    "config_error",
                    "ZOTERO_DATA_DIR must not overlap the writable WebDAV cache",
                )
            probe = cache / f".knowledgehub-cache-write-probe-{uuid.uuid4().hex}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(probe, flags, 0o600)
            try:
                os.write(descriptor, b"ok")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            probe.unlink()
            return cache_real
        except OSError as exc:
            raise ZoteroError(
                "config_error", f"WebDAV cache directory is not writable: {cache}"
            ) from exc

    def prepare_runtime(self) -> None:
        webdav = self.webdav_dir.expanduser()
        try:
            webdav_readable = webdav.is_dir() and os.access(webdav, os.R_OK | os.X_OK)
        except OSError as exc:
            raise ZoteroError(
                "config_error", f"WebDAV directory cannot be accessed: {webdav}"
            ) from exc
        if not webdav_readable:
            raise ZoteroError("config_error", f"WebDAV directory is not readable: {webdav}")
        data = self.data_dir.expanduser()
        try:
            source_real = webdav.resolve(strict=True)
            # Resolve existing parents before creating anything.  In particular,
            # a bad DATA_DIR beneath the immutable WebDAV tree must not create
            # even an otherwise harmless directory there.
            data_real = data.resolve(strict=False)
            if (
                data_real == source_real
                or source_real in data_real.parents
                or data_real in source_real.parents
            ):
                raise ZoteroError(
                    "config_error",
                    "ZOTERO_DATA_DIR must not be inside the WebDAV source, and the source must not be inside the data directory",
                )
            if os.path.lexists(data) and (
                data.is_symlink() or not stat.S_ISDIR(data.lstat().st_mode)
            ):
                raise ZoteroError("config_error", f"Data directory is not a real directory: {data}")
            data.mkdir(parents=True, exist_ok=True, mode=0o700)
            data_real = data.resolve(strict=True)
            if (
                data_real == source_real
                or source_real in data_real.parents
                or data_real in source_real.parents
            ):
                raise ZoteroError(
                    "config_error",
                    "ZOTERO_DATA_DIR must not be inside the WebDAV source, and the source must not be inside the data directory",
                )
            for relative in (
                "state",
                "raw/items",
                "raw/collections",
                "raw/deleted",
                "extracted",
                "manifests/deltas",
                "runs",
                "logs",
                ".staging",
                ".rebuild",
            ):
                _ensure_runtime_directory(data, Path(relative), data_real)
            probe = data / f".knowledgehub-write-probe-{uuid.uuid4().hex}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(probe, flags, 0o600)
            try:
                os.write(descriptor, b"ok")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            probe.unlink()
        except OSError as exc:
            raise ZoteroError("config_error", f"Data directory is not writable: {data}") from exc


def _ensure_runtime_directory(root: Path, relative: Path, resolved_root: Path) -> None:
    current = root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(current):
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ZoteroError(
                    "config_error", f"Runtime path is not a real directory: {current}"
                )
        else:
            current.mkdir(mode=0o700)
        try:
            current.resolve(strict=True).relative_to(resolved_root)
        except ValueError as exc:
            raise ZoteroError(
                "config_error", f"Runtime path escapes data directory: {current}"
            ) from exc


_ENV_FIELDS = {
    "ZOTERO_API_KEY": "api_key",
    "ZOTERO_LIBRARY_TYPE": "library_type",
    "ZOTERO_LIBRARY_ID": "library_id",
    "ZOTERO_API_BASE_URL": "api_base_url",
    "ZOTERO_WEBDAV_URL": "webdav_url",
    "ZOTERO_WEBDAV_USERNAME": "webdav_username",
    "ZOTERO_WEBDAV_PASSWORD": "webdav_password",
    "ZOTERO_WEBDAV_PAGE_LIMIT": "webdav_page_limit",
    "ZOTERO_WEBDAV_REQUEST_INTERVAL_SECONDS": "webdav_request_interval_seconds",
    "ZOTERO_WEBDAV_RETRY_COOLDOWN_SECONDS": "webdav_retry_cooldown_seconds",
    "ZOTERO_WEBDAV_MAX_RETRY_DELAY_SECONDS": "webdav_max_retry_delay_seconds",
    "ZOTERO_WEBDAV_ADOPT_EXISTING": "webdav_adopt_existing",
    "ZOTERO_WEBDAV_PRUNE": "webdav_prune",
    "ZOTERO_WEBDAV_DIR": "webdav_dir",
    "ZOTERO_DATA_DIR": "data_dir",
    "ZOTERO_HTTP_TIMEOUT_SECONDS": "http_timeout_seconds",
    "ZOTERO_MAX_RETRIES": "max_retries",
    "ZOTERO_SYNC_MAX_RETRIES": "sync_max_retries",
    "ZOTERO_API_CONCURRENCY": "api_concurrency",
    "ZOTERO_ZIP_STABILITY_INTERVAL_SECONDS": "zip_stability_interval_seconds",
    "ZOTERO_ZIP_STABILITY_CHECK_COUNT": "zip_stability_check_count",
    "ZOTERO_ENABLE_STREAMING": "enable_streaming",
    "ZOTERO_POLL_INTERVAL_SECONDS": "poll_interval_seconds",
    "ZOTERO_MAPPING_VALIDATION_SAMPLE_SIZE": "mapping_validation_sample_size",
    "ZOTERO_ATTACHMENT_SCAN_ON_304": "attachment_scan_on_304",
    "ZOTERO_METADATA_CHANGES_REQUIRE_CHUNKING": "metadata_changes_require_chunking",
    "ZOTERO_LOG_LEVEL": "log_level",
}


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ZoteroError("config_error", f"Cannot load YAML configuration: {path}") from exc
    if not isinstance(loaded, dict):
        raise ZoteroError("config_error", f"Configuration root must be a mapping: {path}")
    current: Any = loaded
    for key in ("sources", "zotero"):
        if isinstance(current, dict) and key in current:
            current = current[key]
    if not isinstance(current, dict):
        raise ZoteroError("config_error", f"Zotero configuration must be a mapping: {path}")
    return {str(key): value for key, value in current.items()}


def _convert(name: str, value: Any) -> Any:
    if name in {"api_key", "webdav_username", "webdav_password"}:
        return value if isinstance(value, SecretValue) else SecretValue(str(value).strip())
    if name in {"webdav_dir", "data_dir"}:
        return Path(str(value)).expanduser()
    if name in {
        "max_retries",
        "sync_max_retries",
        "api_concurrency",
        "zip_stability_check_count",
        "poll_interval_seconds",
        "mapping_validation_sample_size",
        "webdav_page_limit",
    }:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ZoteroError("config_error", f"{name} must be an integer") from exc
    if name == "library_id":
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ZoteroError("config_error", "library_id must be numeric") from exc
    if name in {
        "http_timeout_seconds",
        "webdav_request_interval_seconds",
        "webdav_retry_cooldown_seconds",
        "webdav_max_retry_delay_seconds",
        "zip_stability_interval_seconds",
    }:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ZoteroError("config_error", f"{name} must be numeric") from exc
    if name in {
        "enable_streaming",
        "attachment_scan_on_304",
        "metadata_changes_require_chunking",
        "webdav_adopt_existing",
        "webdav_prune",
    }:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ZoteroError("config_error", f"{name} must be a boolean")
    if name == "log_level":
        return str(value).strip().upper()
    return str(value).strip() if name in {"library_type", "api_base_url", "webdav_url"} else value
