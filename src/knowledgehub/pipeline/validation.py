"""Cross-check source contract, pipeline state and local artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from knowledgehub.core.hashing import sha256_file
from knowledgehub.manifests.catalog import read_delta_catalog, validate_delta_files
from knowledgehub.pipeline.artifacts import read_chunks_parquet, safe_document_name
from knowledgehub.pipeline.config import RagConfig
from knowledgehub.pipeline.source import ZoteroManifestSource
from knowledgehub.pipeline.state import PipelineState


@dataclass(slots=True)
class ValidationReport:
    valid: bool = True
    checks: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.valid = False
        self.errors.append(message)

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "checks": self.checks, "errors": self.errors}


def validate_pipeline(config: RagConfig, *, check_qdrant: bool = False) -> ValidationReport:
    report = ValidationReport()
    try:
        entries = read_delta_catalog(config.source_delta_catalog_path)
        validate_delta_files(config.source_delta_catalog_path.parent, entries)
        report.checks["delta_catalog_entries"] = len(entries)
    except Exception as exc:
        report.fail(f"delta catalog: {exc}")
        entries = []
    try:
        source_documents = ZoteroManifestSource(
            config.source_snapshot_path, config.source_delta_catalog_path
        ).load_snapshot()
        report.checks["ready_source_documents"] = len(source_documents)
        for document in source_documents:
            if not document.pdf_path.is_file():
                report.fail(f"ready PDF missing: {document.document_id}")
            elif sha256_file(document.pdf_path) != document.pdf_sha256:
                report.fail(f"ready PDF hash mismatch: {document.document_id}")
    except Exception as exc:
        report.fail(f"source snapshot: {exc}")
        source_documents = []
    state = PipelineState(config.data_dir)
    try:
        state.initialize()
        rows = state.documents()
        report.checks["pipeline_documents"] = len(rows)
        active_chunks = 0
        for document_id, row in rows.items():
            if row.get("chunk_status") != "ready":
                continue
            artifact = config.data_dir / "chunks" / f"{safe_document_name(document_id)}.parquet"
            if not artifact.is_file():
                report.fail(f"chunk artifact missing: {document_id}")
                continue
            values = read_chunks_parquet(artifact)
            if len(values) != int(row.get("chunk_count") or 0):
                report.fail(f"chunk count mismatch: {document_id}")
            if any(value["document_id"] != document_id for value in values):
                report.fail(f"foreign chunk in artifact: {document_id}")
            active_chunks += len(values)
        report.checks["chunk_artifact_rows"] = active_chunks
        last = state.last_consumed_delta("zotero")
        report.checks["last_consumed_delta"] = last["sync_id"] if last else None
        if last and entries and int(last["sequence"]) > entries[-1].sequence:
            report.fail("pipeline consumed sequence exceeds source catalog")
    except Exception as exc:
        report.fail(f"pipeline state: {exc}")
    if check_qdrant:
        try:
            from knowledgehub.indexing.qdrant import QdrantIndex

            QdrantIndex(
                config.qdrant_url,
                config.qdrant_collection,
                dense_dim=config.embedding_dim,
            ).ensure_collection()
            report.checks["qdrant_schema"] = "valid"
        except Exception as exc:
            report.fail(f"qdrant: {exc}")
    return report
