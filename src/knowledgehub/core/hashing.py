"""SHA-256 and canonical JSON utilities."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Union

PathLike = Union[str, os.PathLike[str]]


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        converted: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON object keys must be strings")
            converted[key] = _json_value(item)
        return converted
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"value of type {type(value).__name__} is not JSON serializable")


def canonical_json_dumps(value: Any) -> str:
    """Return deterministic, compact UTF-8 JSON text.

    Mapping keys are sorted, insignificant whitespace is omitted, non-finite
    floats are rejected, and Unicode is emitted directly rather than escaped.
    """

    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Encode :func:`canonical_json_dumps` as UTF-8 bytes."""

    return canonical_json_dumps(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of *data*."""

    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str, *, encoding: str = "utf-8") -> str:
    """Return the SHA-256 digest of encoded text."""

    return sha256_bytes(text.encode(encoding))


def sha256_json(value: Any) -> str:
    """Return the SHA-256 digest of a canonical JSON representation."""

    return sha256_bytes(canonical_json_bytes(value))


def canonical_json_hash(value: Any) -> str:
    """Compatibility name for the SHA-256 of canonical JSON."""

    return sha256_json(value)


def sha256_file(path: PathLike, *, chunk_size: int = 1024 * 1024) -> str:
    """Stream a file and return its SHA-256 digest without loading it in memory."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
