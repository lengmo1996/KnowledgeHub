"""Shared, source-agnostic infrastructure."""

from knowledgehub.core.atomic import (
    PathOutsideRootError,
    atomic_replace,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_jsonl,
    atomic_write_text,
    ensure_path_within,
    ensure_within,
    fsync_directory,
    is_path_within,
    safe_remove,
    safe_rmtree,
    safe_unlink,
)
from knowledgehub.core.hashing import (
    canonical_json_bytes,
    canonical_json_dumps,
    canonical_json_hash,
    sha256_bytes,
    sha256_file,
    sha256_json,
    sha256_text,
)
from knowledgehub.core.locking import FileLock, LockBusyError, LockMetadata
from knowledgehub.core.retry import RetryPolicy, compute_retry_delay, is_retryable_status

__all__ = [
    "FileLock",
    "LockBusyError",
    "LockMetadata",
    "PathOutsideRootError",
    "RetryPolicy",
    "atomic_replace",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_jsonl",
    "atomic_write_text",
    "canonical_json_bytes",
    "canonical_json_dumps",
    "canonical_json_hash",
    "compute_retry_delay",
    "ensure_path_within",
    "ensure_within",
    "fsync_directory",
    "is_path_within",
    "is_retryable_status",
    "safe_remove",
    "safe_rmtree",
    "safe_unlink",
    "sha256_bytes",
    "sha256_file",
    "sha256_json",
    "sha256_text",
]
