"""Internal models shared by the Zotero source modules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class SyncMode(str, Enum):
    INCREMENTAL = "incremental"
    FULL = "full"
    ATTACHMENTS = "attachments"


class ZoteroError(RuntimeError):
    """A sanitized, classified Zotero source failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.context = context or {}


class RemoteVersionChanged(ZoteroError):
    def __init__(self, expected: int, observed: int) -> None:
        super().__init__(
            "remote_version_changed",
            f"Remote library changed during synchronization: expected {expected}, observed {observed}",
            retryable=True,
            context={"expected": expected, "observed": observed},
        )


@dataclass(frozen=True)
class KeyAccess:
    user_id: int
    library_type: str
    library_id: int


@dataclass(frozen=True)
class VersionListing:
    versions: dict[str, int]
    library_version: int
    not_modified: bool = False


@dataclass
class SyncSummary:
    sync_id: str
    mode: str
    status: str
    from_version: int = 0
    target_version: int | None = None
    committed_version: int | None = None
    added: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    attachments_ready: int = 0
    attachments_missing: int = 0
    attachments_unstable: int = 0
    attachments_error: int = 0
    delta_upserts: int = 0
    delta_deletes: int = 0
    duration_seconds: float = 0.0
    error_code: str | None = None
    error_message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeDependencies:
    """Injectable boundaries used to make retries and HTTP deterministic in tests."""

    http_transport: Any | None = None
    sleeper: Callable[[float], None] | None = None
    monotonic: Callable[[], float] | None = None
    random: Callable[[], float] | None = None


@dataclass(frozen=True)
class Publication:
    target: Path
    staged: Path
    backup: Path | None = None
