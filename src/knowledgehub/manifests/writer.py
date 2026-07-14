"""Deterministic, atomic writers for KnowledgeHub manifest files."""

from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Union

from knowledgehub.core.atomic import atomic_write_text
from knowledgehub.core.hashing import canonical_json_dumps
from knowledgehub.manifests.models import DeltaRecord, SnapshotRecord

Record = Union[Mapping[str, Any], SnapshotRecord, DeltaRecord]
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _mapping(record: Any) -> dict[str, Any]:
    to_dict = getattr(record, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
    elif isinstance(record, Mapping):
        value = dict(record)
    elif is_dataclass(record) and not isinstance(record, type):
        value = asdict(record)
    else:
        raise TypeError(f"manifest record {type(record).__name__} is not mapping-like")
    if not isinstance(value, Mapping):
        raise TypeError("record to_dict() must return a mapping")
    return dict(value)


def write_json(path: Union[str, Path], value: Any, *, mode: int = 0o644) -> Path:
    """Write one canonical JSON value followed by a newline."""

    return atomic_write_text(path, canonical_json_dumps(value) + "\n", mode=mode)


def write_jsonl(
    path: Union[str, Path],
    records: Iterable[Any],
    *,
    sort_key: Optional[Callable[[Mapping[str, Any]], Any]] = None,
    mode: int = 0o644,
) -> Path:
    """Write complete JSON records as a deterministic, atomically replaced JSONL file."""

    mapped = [_mapping(record) for record in records]
    if sort_key is not None:
        mapped.sort(key=sort_key)
    payload = "".join(f"{canonical_json_dumps(record)}\n" for record in mapped)
    return atomic_write_text(path, payload, mode=mode)


def _unique_by(records: Sequence[Mapping[str, Any]], field: str) -> None:
    seen: set[str] = set()
    for record in records:
        value = record.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"every manifest record requires a non-empty {field}")
        if value in seen:
            raise ValueError(f"duplicate {field}: {value}")
        seen.add(value)


def write_snapshot(path: Union[str, Path], records: Iterable[Record]) -> Path:
    """Write snapshot records uniquely and in ``document_id`` order."""

    mapped = [_mapping(record) for record in records]
    _unique_by(mapped, "document_id")
    mapped.sort(key=lambda value: str(value["document_id"]))
    return write_jsonl(path, mapped)


def write_delta(path: Union[str, Path], records: Iterable[Record]) -> Path:
    """Write at most one delta per document in stable order."""

    mapped = [_mapping(record) for record in records]
    _unique_by(mapped, "document_id")
    sync_ids = {value.get("sync_id") for value in mapped}
    if len(sync_ids) > 1:
        raise ValueError("a delta file cannot contain multiple sync_id values")
    mapped.sort(key=lambda value: str(value["document_id"]))
    return write_jsonl(path, mapped)


def _collection_sort_key(record: Mapping[str, Any]) -> tuple[str, str]:
    return str(record.get("path", "")), str(record.get("key", record.get("collection_key", "")))


class ManifestWriter:
    """Write the standard snapshot, delta, collection and summary locations."""

    def __init__(self, data_dir: Union[str, Path]) -> None:
        self.data_dir = Path(data_dir)
        self.manifests_dir = self.data_dir / "manifests"
        self.deltas_dir = self.manifests_dir / "deltas"

    @property
    def snapshot_path(self) -> Path:
        return self.manifests_dir / "documents.jsonl"

    @property
    def collections_path(self) -> Path:
        return self.manifests_dir / "collections.json"

    @property
    def summary_path(self) -> Path:
        return self.manifests_dir / "summary.json"

    def delta_path(self, sync_id: str) -> Path:
        if not _SAFE_COMPONENT.fullmatch(sync_id):
            raise ValueError("sync_id contains unsafe path characters")
        return self.deltas_dir / f"{sync_id}.jsonl"

    def write_snapshot(self, records: Iterable[Record]) -> Path:
        return write_snapshot(self.snapshot_path, records)

    def write_delta(self, sync_id: str, records: Iterable[Record]) -> Path:
        mapped = [_mapping(record) for record in records]
        for record in mapped:
            if record.get("sync_id") != sync_id:
                raise ValueError("delta record sync_id does not match output filename")
        return write_delta(self.delta_path(sync_id), mapped)

    def write_collections(self, collections: Iterable[Mapping[str, Any]]) -> Path:
        mapped = [dict(collection) for collection in collections]
        mapped.sort(key=_collection_sort_key)
        return write_json(self.collections_path, mapped)

    def write_summary(self, summary: Mapping[str, Any]) -> Path:
        return write_json(self.summary_path, dict(summary))
