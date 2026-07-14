"""Linux advisory file locks used to serialize source synchronisation."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Union

from knowledgehub.core.atomic import fsync_directory
from knowledgehub.core.hashing import canonical_json_bytes

PathLike = Union[str, os.PathLike[str]]


@dataclass(frozen=True, slots=True)
class LockMetadata:
    """Human-readable metadata stored in the persistent lock inode."""

    pid: int
    sync_id: str
    started_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "started_at": self.started_at, "sync_id": self.sync_id}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LockMetadata":
        return cls(
            pid=int(value["pid"]),
            sync_id=str(value["sync_id"]),
            started_at=str(value["started_at"]),
        )


class LockBusyError(RuntimeError):
    """Raised when another live process owns a non-blocking file lock."""

    def __init__(self, path: Path, holder: Optional[LockMetadata] = None) -> None:
        self.path = path
        self.holder = holder
        detail = f" (pid={holder.pid}, sync_id={holder.sync_id})" if holder else ""
        super().__init__(f"lock is already held: {path}{detail}")


def read_lock_metadata(path: PathLike) -> Optional[LockMetadata]:
    """Read best-effort metadata; it is informational, not proof of ownership."""

    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
        if not raw:
            return None
        value = json.loads(raw)
        if not isinstance(value, Mapping):
            return None
        return LockMetadata.from_mapping(value)
    except (FileNotFoundError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


class FileLock:
    """An ``flock``-backed context manager.

    The lock file is deliberately never deleted.  Kernel ownership, rather than
    inode presence or PID probing, determines whether a lock is stale.
    """

    def __init__(
        self,
        path: PathLike,
        *,
        sync_id: str,
        timeout_seconds: Optional[float] = 0.0,
        poll_interval_seconds: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds cannot be negative")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.path = Path(path)
        self.sync_id = sync_id
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._descriptor: Optional[int] = None
        self.metadata: Optional[LockMetadata] = None

    @property
    def acquired(self) -> bool:
        return self._descriptor is not None

    def acquire(self) -> "FileLock":
        if self.acquired:
            raise RuntimeError(f"lock instance is already acquired: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = os.path.lexists(self.path)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise RuntimeError(f"lock path is not a regular file: {self.path}")
        deadline = None if self.timeout_seconds is None else self._clock() + self.timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    os.close(descriptor)
                    raise
                if deadline is not None and self._clock() >= deadline:
                    os.close(descriptor)
                    raise LockBusyError(self.path, read_lock_metadata(self.path)) from exc
                self._sleeper(self.poll_interval_seconds)

        metadata = LockMetadata(
            pid=os.getpid(),
            sync_id=self.sync_id,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        payload = canonical_json_bytes(metadata) + b"\n"
        try:
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
            if not existed:
                fsync_directory(self.path.parent)
        except BaseException:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        self.metadata = metadata
        return self

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        self.metadata = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()
