"""Crash-resistant file writes and guarded deletion helpers.

The atomic write helpers create temporary files in the destination directory,
``fsync`` their contents, replace the destination, and finally ``fsync`` the
directory entry.  The containment helpers resolve symlinks before authorising a
deletion so a caller cannot escape its declared runtime-data root.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union

PathLike = Union[str, os.PathLike[str]]


class PathOutsideRootError(ValueError):
    """Raised when a potentially destructive path is outside its allowed root."""


def _resolved_candidate(path: PathLike, root: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve(strict=False)


def ensure_path_within(path: PathLike, root: PathLike, *, allow_root: bool = False) -> Path:
    """Resolve *path* and prove it is a descendant of *root*.

    Relative paths are interpreted relative to ``root``.  By default the root
    itself is rejected, which makes the function safe as a deletion guard.
    """

    resolved_root = Path(root).expanduser().resolve(strict=False)
    resolved_path = _resolved_candidate(path, resolved_root)
    if resolved_path == resolved_root:
        if allow_root:
            return resolved_path
        raise PathOutsideRootError(f"refusing to operate on the root directory: {resolved_root}")
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise PathOutsideRootError(
            f"path {resolved_path} is outside allowed root {resolved_root}"
        ) from exc
    return resolved_path


def is_path_within(path: PathLike, root: PathLike, *, allow_root: bool = False) -> bool:
    """Return whether *path* satisfies :func:`ensure_path_within`."""

    try:
        ensure_path_within(path, root, allow_root=allow_root)
    except PathOutsideRootError:
        return False
    return True


def ensure_within(path: PathLike, root: PathLike, *, allow_root: bool = False) -> Path:
    """Compatibility spelling for :func:`ensure_path_within`."""

    return ensure_path_within(path, root, allow_root=allow_root)


def fsync_directory(path: PathLike) -> None:
    """Synchronise a directory entry to durable storage on POSIX."""

    directory = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: PathLike, data: bytes, *, mode: int = 0o644) -> Path:
    """Atomically and durably replace *path* with *data*."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        fsync_directory(destination.parent)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return destination


def atomic_write_text(
    path: PathLike,
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int = 0o644,
) -> Path:
    """Atomically and durably replace *path* with encoded text."""

    return atomic_write_bytes(path, text.encode(encoding), mode=mode)


def atomic_write_json(path: PathLike, value: Any, *, mode: int = 0o644) -> Path:
    """Atomically write one canonical JSON value followed by a newline."""

    # Imported lazily so the low-level hashing module remains usable without a
    # dependency cycle through this file.
    from knowledgehub.core.hashing import canonical_json_dumps

    return atomic_write_text(path, canonical_json_dumps(value) + "\n", mode=mode)


def atomic_write_jsonl(
    path: PathLike,
    records: Iterable[Any],
    *,
    sort_key: Optional[Callable[[Any], Any]] = None,
    mode: int = 0o644,
) -> Path:
    """Atomically write complete canonical JSON lines (an empty iterable is valid)."""

    from knowledgehub.core.hashing import canonical_json_dumps

    materialized = list(records)
    if sort_key is not None:
        materialized.sort(key=sort_key)
    payload = "".join(f"{canonical_json_dumps(record)}\n" for record in materialized)
    return atomic_write_text(path, payload, mode=mode)


def atomic_replace(source: PathLike, destination: PathLike) -> Path:
    """Replace *destination* with a fully staged file or directory.

    The source and destination must reside on the same filesystem for
    ``os.replace`` semantics.  Callers are responsible for fsyncing staged file
    contents before invoking this helper.
    """

    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source_path, destination_path)
    fsync_directory(destination_path.parent)
    if source_path.parent != destination_path.parent and source_path.parent.exists():
        fsync_directory(source_path.parent)
    return destination_path


def safe_unlink(path: PathLike, *, root: PathLike, missing_ok: bool = True) -> None:
    """Unlink a non-directory only after enforcing runtime-root containment."""

    candidate = Path(path).expanduser()
    resolved_root = Path(root).expanduser().resolve(strict=False)
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    # Keep lexical symlink identity: resolving it and then unlinking would
    # delete its referent instead of the directory entry selected by the caller.
    if candidate.is_symlink():
        ensure_path_within(candidate.parent, resolved_root, allow_root=True)
        candidate.unlink()
        fsync_directory(candidate.parent)
        return
    target = ensure_path_within(candidate, resolved_root)
    if target.is_dir() and not target.is_symlink():
        raise IsADirectoryError(str(target))
    target.unlink(missing_ok=missing_ok)
    fsync_directory(target.parent)


def safe_rmtree(path: PathLike, *, root: PathLike, missing_ok: bool = True) -> None:
    """Remove a directory tree only after enforcing runtime-root containment."""

    original = Path(path).expanduser()
    root_path = Path(root).expanduser().resolve(strict=False)
    if not original.is_absolute():
        original = root_path / original
    if not original.exists() and not original.is_symlink():
        if missing_ok:
            return
        raise FileNotFoundError(str(original))
    if original.is_symlink():
        ensure_path_within(original.parent, root_path, allow_root=True)
        original.unlink()
        fsync_directory(original.parent)
        return
    target = ensure_path_within(original, root_path)
    if not target.is_dir():
        raise NotADirectoryError(str(target))
    shutil.rmtree(target)
    fsync_directory(target.parent)


def safe_remove(path: PathLike, *, root: PathLike, missing_ok: bool = True) -> None:
    """Remove a file, symlink, or directory under *root* without escaping it."""

    candidate = Path(path).expanduser()
    resolved_root = Path(root).expanduser().resolve(strict=False)
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    if candidate.is_symlink():
        ensure_path_within(candidate.parent, resolved_root, allow_root=True)
        safe_unlink(candidate, root=resolved_root, missing_ok=missing_ok)
        return
    ensure_path_within(candidate, resolved_root)
    if not candidate.is_dir():
        safe_unlink(candidate, root=resolved_root, missing_ok=missing_ok)
    else:
        safe_rmtree(candidate, root=resolved_root, missing_ok=missing_ok)
