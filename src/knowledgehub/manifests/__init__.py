"""Stable wire contracts consumed by downstream KnowledgeHub pipelines."""

from knowledgehub.manifests.catalog import (
    DELTA_CATALOG_SCHEMA_VERSION,
    DeltaCatalogEntry,
    append_delta_catalog,
    read_delta_catalog,
    validate_delta_files,
)
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
    "DELTA_CATALOG_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "AttachmentManifest",
    "CollectionReference",
    "Creator",
    "DeltaCatalogEntry",
    "DeltaOperation",
    "DeltaReason",
    "DeltaRecord",
    "ManifestWriter",
    "SnapshotRecord",
    "append_delta_catalog",
    "read_delta_catalog",
    "validate_delta_files",
    "write_delta",
    "write_json",
    "write_jsonl",
    "write_snapshot",
]
