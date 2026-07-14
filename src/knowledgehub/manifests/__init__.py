"""Stable wire contracts consumed by downstream KnowledgeHub pipelines."""

from knowledgehub.manifests.models import (
    MANIFEST_SCHEMA_VERSION,
    AttachmentManifest,
    CollectionReference,
    Creator,
    DeltaOperation,
    DeltaReason,
    DeltaRecord,
    SnapshotRecord,
)
from knowledgehub.manifests.writer import (
    ManifestWriter,
    write_delta,
    write_json,
    write_jsonl,
    write_snapshot,
)

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "AttachmentManifest",
    "CollectionReference",
    "Creator",
    "DeltaOperation",
    "DeltaReason",
    "DeltaRecord",
    "ManifestWriter",
    "SnapshotRecord",
    "write_delta",
    "write_json",
    "write_jsonl",
    "write_snapshot",
]
