"""Durable ordering metadata for source delta manifests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from knowledgehub.core.hashing import sha256_file
from knowledgehub.manifests.writer import write_jsonl

DELTA_CATALOG_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class DeltaCatalogEntry:
    sequence: int
    sync_id: str
    previous_sync_id: str | None
    from_version: int
    target_version: int
    delta_path: str
    delta_sha256: str
    row_count: int
    created_at: str
    schema_version: int = DELTA_CATALOG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DELTA_CATALOG_SCHEMA_VERSION:
            raise ValueError("unsupported delta catalog schema version")
        if self.sequence <= 0 or not self.sync_id or self.from_version < 0:
            raise ValueError("invalid delta catalog entry")
        if self.target_version < self.from_version or self.row_count < 0:
            raise ValueError("invalid delta catalog version or row count")
        if len(self.delta_sha256) != 64:
            raise ValueError("invalid delta SHA-256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "delta_path": self.delta_path,
            "delta_sha256": self.delta_sha256,
            "from_version": self.from_version,
            "previous_sync_id": self.previous_sync_id,
            "row_count": self.row_count,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "sync_id": self.sync_id,
            "target_version": self.target_version,
        }


def read_delta_catalog(path: Path) -> list[DeltaCatalogEntry]:
    """Read and validate a complete catalog, including sequence continuity."""

    if not path.exists():
        return []
    entries: list[DeltaCatalogEntry] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                raise ValueError(f"blank delta catalog line {line_number}")
            payload = json.loads(line)
            if not isinstance(payload, Mapping):
                raise ValueError(f"delta catalog line {line_number} is not an object")
            entry = DeltaCatalogEntry(**dict(payload))
            expected = len(entries) + 1
            if entry.sequence != expected:
                raise ValueError(
                    f"delta catalog sequence gap: expected {expected}, got {entry.sequence}"
                )
            predecessor = entries[-1].sync_id if entries else None
            if entry.previous_sync_id != predecessor:
                raise ValueError(f"delta catalog predecessor mismatch at sequence {expected}")
            if entries and entry.from_version != entries[-1].target_version:
                raise ValueError(f"delta catalog version gap at sequence {expected}")
            entries.append(entry)
    return entries


def append_delta_catalog(
    *,
    current_path: Path,
    output_path: Path,
    sync_id: str,
    from_version: int,
    target_version: int,
    staged_delta_path: Path,
    row_count: int,
    created_at: str | None = None,
) -> DeltaCatalogEntry:
    """Stage a catalog containing the existing entries plus one delta."""

    entries = read_delta_catalog(current_path)
    if any(entry.sync_id == sync_id for entry in entries):
        raise ValueError(f"duplicate delta catalog sync_id: {sync_id}")
    entry = DeltaCatalogEntry(
        sequence=len(entries) + 1,
        sync_id=sync_id,
        previous_sync_id=entries[-1].sync_id if entries else None,
        from_version=from_version,
        target_version=target_version,
        delta_path=f"deltas/{sync_id}.jsonl",
        delta_sha256=sha256_file(staged_delta_path),
        row_count=row_count,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
    )
    write_jsonl(output_path, [*entries, entry], sort_key=lambda value: int(value["sequence"]))
    return entry


def validate_delta_files(manifests_dir: Path, entries: Iterable[DeltaCatalogEntry]) -> None:
    """Validate every catalog path, hash, and JSONL row count."""

    root = manifests_dir.resolve(strict=True)
    for entry in entries:
        candidate = (manifests_dir / entry.delta_path).resolve(strict=True)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"delta path escapes manifests directory: {entry.delta_path}") from exc
        if sha256_file(candidate) != entry.delta_sha256:
            raise ValueError(f"delta hash mismatch: {entry.sync_id}")
        with candidate.open("r", encoding="utf-8") as stream:
            count = sum(1 for line in stream if line.strip())
        if count != entry.row_count:
            raise ValueError(f"delta row count mismatch: {entry.sync_id}")
