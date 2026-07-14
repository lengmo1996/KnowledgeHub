"""Read-only adapter for the current KnowledgeHub snapshot/delta contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from knowledgehub.core.hashing import sha256_file
from knowledgehub.manifests.catalog import (
    DeltaCatalogEntry,
    read_delta_catalog,
    validate_delta_files,
)
from knowledgehub.pipeline.models import SourceDocument
from knowledgehub.pipeline.state import PipelineState


class SourceContractError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeltaEvent:
    operation: str
    document_id: str
    reason: str
    document: SourceDocument | None
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DeltaBatch:
    catalog: DeltaCatalogEntry
    events: tuple[DeltaEvent, ...]


class ZoteroManifestSource:
    def __init__(self, snapshot_path: Path, catalog_path: Path) -> None:
        self.snapshot_path = snapshot_path
        self.catalog_path = catalog_path
        self.manifests_dir = catalog_path.parent

    def load_snapshot(
        self,
        *,
        limit: int | None = None,
        document_id: str | None = None,
        attachment_key: str | None = None,
    ) -> list[SourceDocument]:
        if not self.snapshot_path.is_file():
            raise SourceContractError(f"source snapshot is missing: {self.snapshot_path}")
        documents: list[SourceDocument] = []
        with self.snapshot_path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SourceContractError(
                        f"invalid snapshot JSON at line {line_number}"
                    ) from exc
                if not isinstance(value, Mapping):
                    raise SourceContractError(f"snapshot line {line_number} is not an object")
                if document_id and value.get("document_id") != document_id:
                    continue
                if attachment_key and value.get("attachment_key") != attachment_key:
                    continue
                try:
                    documents.append(SourceDocument.from_snapshot(value))
                except ValueError as exc:
                    if value.get("status") == "ready":
                        raise SourceContractError(
                            f"invalid ready snapshot record at line {line_number}: {exc}"
                        ) from exc
                    continue
                if limit is not None and len(documents) >= limit:
                    break
        documents.sort(key=lambda value: value.document_id)
        return documents

    def pending_deltas(self, state: PipelineState) -> list[DeltaBatch]:
        if not self.catalog_path.is_file():
            raise SourceContractError("source delta catalog is missing; run full/reconcile")
        entries = read_delta_catalog(self.catalog_path)
        validate_delta_files(self.manifests_dir, entries)
        last = state.last_consumed_delta("zotero")
        next_sequence = int(last["sequence"]) + 1 if last else 1
        if last:
            matching = [entry for entry in entries if entry.sequence == int(last["sequence"])]
            if not matching or matching[0].sync_id != last["sync_id"]:
                raise SourceContractError("consumed delta is not present in the current catalog")
            if matching[0].delta_sha256 != last["delta_sha256"]:
                raise SourceContractError("previously consumed delta hash changed")
        available = [entry for entry in entries if entry.sequence >= next_sequence]
        if available and available[0].sequence != next_sequence:
            raise SourceContractError("delta sequence gap; run reconcile")
        return [self._load_delta(entry) for entry in available]

    def _load_delta(self, entry: DeltaCatalogEntry) -> DeltaBatch:
        path = self.manifests_dir / entry.delta_path
        if sha256_file(path) != entry.delta_sha256:
            raise SourceContractError(f"delta hash mismatch: {entry.sync_id}")
        events: list[DeltaEvent] = []
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                value = json.loads(line)
                if not isinstance(value, Mapping) or value.get("sync_id") != entry.sync_id:
                    raise SourceContractError(
                        f"invalid delta record in {entry.sync_id} line {line_number}"
                    )
                operation = str(value.get("operation") or "")
                document_id = str(value.get("document_id") or "")
                if not document_id:
                    raise SourceContractError(
                        f"delta record has no document_id in {entry.sync_id} line {line_number}"
                    )
                document: SourceDocument | None = None
                if operation == "upsert":
                    manifest = value.get("manifest_record")
                    if isinstance(manifest, Mapping):
                        try:
                            document = SourceDocument.from_snapshot(manifest)
                        except ValueError as exc:
                            if manifest.get("status") == "ready":
                                raise SourceContractError(
                                    f"invalid ready delta record in {entry.sync_id} "
                                    f"line {line_number}: {exc}"
                                ) from exc
                            document = None
                elif operation != "delete":
                    raise SourceContractError(f"unsupported delta operation: {operation}")
                events.append(
                    DeltaEvent(
                        operation=operation,
                        document_id=document_id,
                        reason=str(value.get("reason") or ""),
                        document=document,
                        raw=dict(value),
                    )
                )
        if len(events) != entry.row_count:
            raise SourceContractError(f"delta row count mismatch: {entry.sync_id}")
        return DeltaBatch(catalog=entry, events=tuple(events))
