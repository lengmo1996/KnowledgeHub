"""Coordinator for full, incremental, resumable and reconcile RAG runs."""

from __future__ import annotations

import json
import logging
import math
import uuid
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable, Mapping, Sequence

from knowledgehub.chunking.fingerprints import (
    document_chunk_fingerprint,
    document_embedding_fingerprint,
    document_parse_fingerprint,
)
from knowledgehub.chunking.structural import StructuralChunker
from knowledgehub.core.locking import FileLock
from knowledgehub.embeddings.endpoint_pool import EndpointPool
from knowledgehub.indexing.qdrant import QdrantIndex
from knowledgehub.indexing.sparse import SparseEncoder
from knowledgehub.manifests.catalog import read_delta_catalog, validate_delta_files
from knowledgehub.pipeline.artifacts import (
    read_chunks_parquet,
    read_parsed,
    safe_document_name,
    write_chunks_parquet,
)
from knowledgehub.pipeline.config import GPUPlan, RagConfig
from knowledgehub.pipeline.models import ChunkRecord, SourceDocument
from knowledgehub.pipeline.source import DeltaBatch, ZoteroManifestSource
from knowledgehub.pipeline.state import PipelineState
from knowledgehub.pipeline.workers import ParseWorkerResult, run_parse_workers, stable_partition

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PipelineSummary:
    run_id: str
    mode: str
    status: str = "running"
    selected: int = 0
    parsed: int = 0
    parse_failed: int = 0
    skipped: int = 0
    embedded: int = 0
    indexed: int = 0
    deleted: int = 0
    payload_updated: int = 0
    deltas_consumed: int = 0
    chunks: int = 0
    chunk_tokens: int = 0
    embedded_tokens: int = 0
    parse_p95_seconds: float = 0.0
    gpu_plan: dict[str, Any] = field(default_factory=dict)
    per_gpu_documents: dict[str, int] = field(default_factory=dict)
    per_gpu_pages: dict[str, int] = field(default_factory=dict)
    endpoint_stats: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeComponents:
    parser_runner: Callable[
        [RagConfig, GPUPlan, Sequence[SourceDocument]], list[ParseWorkerResult]
    ] = run_parse_workers
    endpoint_pool: Any | None = None
    sparse_encoder: Any | None = None
    index: Any | None = None


class PipelineOrchestrator:
    def __init__(
        self,
        config: RagConfig,
        *,
        components: RuntimeComponents | None = None,
        gpu_devices: tuple[Any, ...] | None = None,
        initialize: bool = True,
    ) -> None:
        self.config = config.validate()
        self.components = components or RuntimeComponents()
        self.state = PipelineState(config.data_dir)
        if initialize:
            self.config.prepare_runtime()
            self.state.initialize()
        self.source = ZoteroManifestSource(
            config.source_snapshot_path, config.source_delta_catalog_path
        )
        self.gpu_plan = config.resolve_gpu_plan(gpu_devices)
        self._pool: Any | None = self.components.endpoint_pool
        self._sparse: Any | None = self.components.sparse_encoder
        self._index: Any | None = self.components.index

    def close(self) -> None:
        if self._pool is not None and self._pool is not self.components.endpoint_pool:
            self._pool.close()

    def snapshot_index(self) -> str:
        """Create an explicit Qdrant snapshot for a bounded acceptance run."""

        index = self._qdrant_index()
        index.ensure_collection()
        return str(index.snapshot())

    def plan(
        self,
        *,
        limit: int | None = None,
        document_id: str | None = None,
        attachment_key: str | None = None,
    ) -> dict[str, Any]:
        documents = self.source.load_snapshot(
            limit=limit, document_id=document_id, attachment_key=attachment_key
        )
        existing = self.state.documents() if self.state.path.is_file() else {}
        return {
            "source": self.config.source,
            "snapshot": str(self.config.source_snapshot_path),
            "selected": len(documents),
            "known_documents": len(existing),
            "gpu_plan": self.gpu_plan.to_dict(),
            "collection": self.config.qdrant_collection,
            "embedding": {
                "model": self.config.embedding_model,
                "revision": self.config.embedding_revision,
                "dimension": self.config.embedding_dim,
            },
            "documents": [value.document_id for value in documents],
        }

    def ingest_full(
        self,
        *,
        limit: int | None = None,
        document_id: str | None = None,
        attachment_key: str | None = None,
        force: bool = False,
        prune: bool = False,
        parse_only: bool = False,
    ) -> PipelineSummary:
        entries = read_delta_catalog(self.config.source_delta_catalog_path)
        validate_delta_files(self.config.source_delta_catalog_path.parent, entries)
        documents = self.source.load_snapshot(
            limit=limit, document_id=document_id, attachment_key=attachment_key
        )
        summary = self._run_documents(documents, mode="full", force=force, parse_only=parse_only)
        unfiltered = limit is None and document_id is None and attachment_key is None
        if summary.status == "success" and unfiltered:
            if prune:
                current = {value.document_id for value in documents}
                stale = sorted(set(self.state.documents(active_only=True)) - current)
                with self.state.transaction() as connection:
                    for stale_id in stale:
                        self.state.mark_unavailable(
                            connection,
                            stale_id,
                            status="deleted",
                            reason="snapshot_prune",
                        )
                        summary.deleted += 1
                if stale and not parse_only:
                    self._apply_index_operations(summary)
            self._mark_all_catalog_consumed(summary)
        return summary

    def ingest_incremental(self, *, parse_only: bool = False) -> PipelineSummary:
        batches = self.source.pending_deltas(self.state)
        aggregate = PipelineSummary(
            run_id=_run_id(),
            mode="incremental",
            gpu_plan=self.gpu_plan.to_dict(),
        )
        self.state.begin_run(aggregate.run_id, aggregate.mode, aggregate.gpu_plan)
        try:
            with FileLock(self.config.data_dir / "state" / "rag.lock", sync_id=aggregate.run_id):
                for batch in batches:
                    documents, unavailable = self._apply_delta_source(batch)
                    current = self._process_documents(
                        documents,
                        aggregate,
                        force=False,
                        parse_only=parse_only,
                    )
                    if unavailable and not parse_only:
                        self._apply_index_operations(aggregate)
                    if current:
                        aggregate.selected += len(current)
                    with self.state.transaction() as connection:
                        self.state.mark_delta_consumed(
                            connection,
                            source="zotero",
                            sequence=batch.catalog.sequence,
                            sync_id=batch.catalog.sync_id,
                            delta_path=batch.catalog.delta_path,
                            delta_sha256=batch.catalog.delta_sha256,
                        )
                    aggregate.deltas_consumed += 1
            aggregate.status = "success" if not aggregate.errors else "partial"
            self.state.finish_run(
                aggregate.run_id, status=aggregate.status, summary=aggregate.to_dict()
            )
        except Exception as exc:
            aggregate.status = "failed"
            aggregate.errors.append({"stage": "incremental", "error": str(exc)})
            self.state.finish_run(
                aggregate.run_id,
                status="failed",
                summary=aggregate.to_dict(),
                error=str(exc),
            )
            raise
        return aggregate

    def reconcile(self, *, parse_only: bool = False) -> PipelineSummary:
        return self.ingest_full(force=False, prune=True, parse_only=parse_only)

    def resume(
        self,
        *,
        limit: int | None = None,
        document_id: str | None = None,
        attachment_key: str | None = None,
        parse_only: bool = False,
    ) -> PipelineSummary:
        return self._run_documents(
            self._active_documents(
                limit=limit,
                document_id=document_id,
                attachment_key=attachment_key,
            ),
            mode="resume",
            force=False,
            parse_only=parse_only,
        )

    def parse_pending(
        self,
        *,
        limit: int | None = None,
        document_id: str | None = None,
        attachment_key: str | None = None,
        force: bool = False,
    ) -> PipelineSummary:
        documents = self._active_documents(
            limit=limit, document_id=document_id, attachment_key=attachment_key
        )
        return self._run_documents(documents, mode="parse", force=force, parse_only=True)

    def embed_pending(
        self,
        *,
        limit: int | None = None,
        document_id: str | None = None,
        attachment_key: str | None = None,
        force: bool = False,
    ) -> PipelineSummary:
        documents: list[SourceDocument] = []
        for row in self.state.documents(active_only=True).values():
            if document_id and row.get("document_id") != document_id:
                continue
            if attachment_key and row.get("attachment_key") != attachment_key:
                continue
            expected = document_embedding_fingerprint(
                self.config, str(row.get("chunk_fingerprint") or "")
            )
            if row.get("chunk_status") != "ready":
                continue
            if not force and (
                row.get("embedding_status") == "ready"
                and row.get("embedding_fingerprint") == expected
                and row.get("dense_index_status") == "ready"
                and row.get("sparse_index_status") == "ready"
            ):
                continue
            metadata = json.loads(str(row["metadata_json"]))
            documents.append(SourceDocument.from_snapshot(metadata))
        documents.sort(key=lambda value: value.document_id)
        if limit is not None:
            documents = documents[:limit]
        summary = PipelineSummary(
            run_id=_run_id(),
            mode="embed",
            selected=len(documents),
            gpu_plan=self.gpu_plan.to_dict(),
        )
        self.state.begin_run(summary.run_id, summary.mode, summary.gpu_plan)
        try:
            with FileLock(self.config.data_dir / "state" / "rag.lock", sync_id=summary.run_id):
                self._embed_and_index(documents, summary)
            summary.status = "success" if not summary.errors else "partial"
            self.state.finish_run(summary.run_id, status=summary.status, summary=summary.to_dict())
        except Exception as exc:
            summary.status = "failed"
            self.state.finish_run(
                summary.run_id, status="failed", summary=summary.to_dict(), error=str(exc)
            )
            raise
        return summary

    def _run_documents(
        self,
        documents: Sequence[SourceDocument],
        *,
        mode: str,
        force: bool,
        parse_only: bool,
    ) -> PipelineSummary:
        summary = PipelineSummary(
            run_id=_run_id(), mode=mode, selected=len(documents), gpu_plan=self.gpu_plan.to_dict()
        )
        self.state.begin_run(summary.run_id, mode, summary.gpu_plan)
        try:
            with FileLock(self.config.data_dir / "state" / "rag.lock", sync_id=summary.run_id):
                self._process_documents(documents, summary, force=force, parse_only=parse_only)
            summary.status = "success" if not summary.errors else "partial"
            self.state.finish_run(summary.run_id, status=summary.status, summary=summary.to_dict())
        except Exception as exc:
            summary.status = "failed"
            summary.errors.append({"stage": mode, "error": str(exc)})
            self.state.finish_run(
                summary.run_id, status="failed", summary=summary.to_dict(), error=str(exc)
            )
            raise
        return summary

    def _process_documents(
        self,
        documents: Sequence[SourceDocument],
        summary: PipelineSummary,
        *,
        force: bool,
        parse_only: bool,
    ) -> list[SourceDocument]:
        if not documents:
            return []
        previous = self.state.documents()
        parse_candidates: list[SourceDocument] = []
        chunk_candidates: list[SourceDocument] = []
        embed_candidates: list[SourceDocument] = []
        with self.state.transaction() as connection:
            for document in documents:
                old = previous.get(document.document_id)
                self.state.upsert_source_document(connection, document)
                expected_parser = self.config.parser_name
                if old and old.get("parser_name") == self.config.parser_fallback:
                    expected_parser = self.config.parser_fallback
                expected_parse = document_parse_fingerprint(
                    document,
                    parser_name=expected_parser,
                    parser_version=_package_version(expected_parser),
                    ocr=self.config.ocr_enabled,
                )
                needs_parse = (
                    force
                    or old is None
                    or old.get("source_content_fingerprint") != document.source_content_fingerprint
                    or old.get("parse_status") != "ready"
                    or old.get("parse_fingerprint") != expected_parse
                )
                if needs_parse:
                    self.state.invalidate_document_content(
                        connection,
                        document.document_id,
                        reason="content_or_parser_changed",
                    )
                    parse_candidates.append(document)
                else:
                    # ``old is None`` is one of the conditions that makes
                    # ``needs_parse`` true; spell that invariant out for the
                    # type checker before reading the persisted projection.
                    assert old is not None
                    if (
                        old.get("source_metadata_fingerprint")
                        != document.source_metadata_fingerprint
                    ):
                        self.state.enqueue_index_operation(
                            connection, document.document_id, "payload", "metadata_changed"
                        )
                    expected_chunk = document_chunk_fingerprint(
                        self.config, str(old.get("parse_fingerprint") or "")
                    )
                    chunk_path = (
                        self.config.data_dir
                        / "chunks"
                        / f"{safe_document_name(document.document_id)}.parquet"
                    )
                    needs_chunk = (
                        old.get("chunk_status") != "ready"
                        or old.get("chunk_fingerprint") != expected_chunk
                        or not chunk_path.is_file()
                    )
                    if needs_chunk:
                        self.state.invalidate_document_chunks(
                            connection,
                            document.document_id,
                            reason="chunker_or_artifact_changed",
                        )
                        chunk_candidates.append(document)
                        continue
                    summary.skipped += 1
                    expected_embedding = document_embedding_fingerprint(
                        self.config, str(old.get("chunk_fingerprint") or "")
                    )
                    if (
                        force
                        or old.get("embedding_status") != "ready"
                        or old.get("embedding_fingerprint") != expected_embedding
                        or old.get("dense_index_status") != "ready"
                        or old.get("sparse_index_status") != "ready"
                    ):
                        embed_candidates.append(document)

        if parse_candidates:
            with self.state.transaction() as connection:
                self.state.prepare_work(
                    connection,
                    stage="parse",
                    assignments=[
                        (
                            value.document_id,
                            stable_partition(value.document_id, self.gpu_plan.parser_workers),
                        )
                        for value in parse_candidates
                    ],
                )
                for value in parse_candidates:
                    partition = stable_partition(value.document_id, self.gpu_plan.parser_workers)
                    self.state.claim_work(
                        connection,
                        document_id=value.document_id,
                        stage="parse",
                        worker_id=f"parser-{partition}",
                    )
        results = self.components.parser_runner(self.config, self.gpu_plan, parse_candidates)
        parse_durations = [value.duration_seconds for value in results]
        if parse_durations:
            ordered = sorted(parse_durations)
            summary.parse_p95_seconds = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]
        successful_ids: set[str] = set()
        with self.state.transaction() as connection:
            for result in results:
                if result.status != "success":
                    summary.parse_failed += 1
                    summary.errors.append(
                        {
                            "document_id": result.document_id,
                            "stage": "parse",
                            "error": result.error or "",
                        }
                    )
                    self.state.update_stage(
                        connection,
                        result.document_id,
                        "parse",
                        status="failed",
                        error=result.error,
                    )
                    self.state.finish_work(
                        connection,
                        document_id=result.document_id,
                        stage="parse",
                        success=False,
                        error=result.error,
                    )
                    continue
                successful_ids.add(result.document_id)
                summary.parsed += 1
                summary.chunks += len(result.chunks)
                summary.chunk_tokens += sum(value.token_count for value in result.chunks)
                gpu = str(result.gpu_id) if result.gpu_id is not None else "cpu"
                summary.per_gpu_documents[gpu] = summary.per_gpu_documents.get(gpu, 0) + 1
                summary.per_gpu_pages[gpu] = summary.per_gpu_pages.get(gpu, 0) + result.page_count
                self.state.update_stage(
                    connection,
                    result.document_id,
                    "parse",
                    status="ready",
                    fingerprint=result.parse_fingerprint,
                    values={
                        "parser_name": result.parser_name,
                        "parser_version": result.parser_version,
                        "assigned_parse_worker": result.worker_id,
                    },
                )
                self.state.replace_chunks(connection, result.document_id, result.chunks)
                self.state.update_stage(
                    connection,
                    result.document_id,
                    "chunk",
                    status="ready",
                    fingerprint=result.chunk_fingerprint,
                    values={"chunk_count": len(result.chunks)},
                )
                self.state.enqueue_index_operation(
                    connection, result.document_id, "replace", "parsed_content"
                )
                self.state.finish_work(
                    connection,
                    document_id=result.document_id,
                    stage="parse",
                    success=True,
                )
        embed_candidates.extend(
            value for value in parse_candidates if value.document_id in successful_ids
        )
        rechunked_ids = self._rechunk_documents(chunk_candidates, summary)
        embed_candidates.extend(
            value for value in chunk_candidates if value.document_id in rechunked_ids
        )
        if not parse_only:
            self._embed_and_index(embed_candidates, summary)
            self._apply_index_operations(summary, exclude_replace=True)
        return list(documents)

    def _rechunk_documents(
        self, documents: Sequence[SourceDocument], summary: PipelineSummary
    ) -> set[str]:
        """Rebuild canonical Parquet from persisted parse artifacts."""

        if not documents:
            return set()
        chunker = StructuralChunker(self.config)
        successful: set[str] = set()
        for document in sorted(documents, key=lambda value: value.document_id):
            try:
                parsed = read_parsed(self.config.data_dir, document.document_id)
                chunks = chunker.chunk(document, parsed)
                write_chunks_parquet(self.config.data_dir, document.document_id, chunks)
                fingerprint = document_chunk_fingerprint(self.config, parsed.parse_fingerprint)
                with self.state.transaction() as connection:
                    self.state.replace_chunks(connection, document.document_id, chunks)
                    self.state.update_stage(
                        connection,
                        document.document_id,
                        "chunk",
                        status="ready",
                        fingerprint=fingerprint,
                        values={"chunk_count": len(chunks)},
                    )
                    self.state.enqueue_index_operation(
                        connection, document.document_id, "replace", "chunker_changed"
                    )
            except Exception as exc:
                summary.errors.append(
                    {
                        "document_id": document.document_id,
                        "stage": "chunk",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                with self.state.transaction() as connection:
                    self.state.update_stage(
                        connection,
                        document.document_id,
                        "chunk",
                        status="failed",
                        error=str(exc),
                    )
                continue
            successful.add(document.document_id)
            summary.chunks += len(chunks)
            summary.chunk_tokens += sum(value.token_count for value in chunks)
        return successful

    def _embed_and_index(
        self, documents: Sequence[SourceDocument], summary: PipelineSummary
    ) -> None:
        if not documents:
            return
        pool = self._endpoint_pool()
        sparse_encoder = self._sparse_encoder()
        index = self._qdrant_index()
        index.ensure_collection()
        for document in sorted(documents, key=lambda value: value.document_id):
            row = self.state.document(document.document_id)
            if row is None or row.get("chunk_status") != "ready":
                continue
            path = (
                self.config.data_dir
                / "chunks"
                / f"{safe_document_name(document.document_id)}.parquet"
            )
            if not path.is_file():
                summary.errors.append(
                    {
                        "document_id": document.document_id,
                        "stage": "embed",
                        "error": "chunk artifact missing",
                    }
                )
                continue
            raw_chunks = read_chunks_parquet(path)
            chunks = [ChunkRecord(**value) for value in raw_chunks]
            dense: list[Sequence[float]] = []
            for offset in range(0, len(chunks), self.config.embedding_batch_size):
                batch = chunks[offset : offset + self.config.embedding_batch_size]
                dense.extend(pool.embed([value.text for value in batch]).vectors)
            sparse = sparse_encoder.encode([value.text for value in chunks])
            index.replace_document(document.document_id, chunks, dense, sparse)
            embedding_fingerprint = document_embedding_fingerprint(
                self.config, str(row.get("chunk_fingerprint") or "")
            )
            with self.state.transaction() as connection:
                self.state.update_stage(
                    connection,
                    document.document_id,
                    "embedding",
                    status="ready",
                    fingerprint=embedding_fingerprint,
                    values={
                        "embedding_model": self.config.embedding_model,
                        "embedding_revision": self.config.embedding_revision,
                        "embedding_dim": self.config.embedding_dim,
                    },
                )
                self.state.update_stage(
                    connection, document.document_id, "dense_index", status="ready"
                )
                self.state.update_stage(
                    connection, document.document_id, "sparse_index", status="ready"
                )
                self.state.complete_index_operation(connection, document.document_id)
            summary.embedded += 1
            summary.indexed += 1
            summary.chunks += len(chunks) if summary.parsed == 0 else 0
            summary.embedded_tokens += sum(value.token_count for value in chunks)
        summary.endpoint_stats = pool.stats()

    def _apply_delta_source(self, batch: DeltaBatch) -> tuple[list[SourceDocument], list[str]]:
        documents: list[SourceDocument] = []
        unavailable: list[str] = []
        with self.state.transaction() as connection:
            for event in batch.events:
                if event.operation == "delete" or event.document is None:
                    self.state.mark_unavailable(
                        connection,
                        event.document_id,
                        status="deleted" if event.operation == "delete" else "unavailable",
                        reason=event.reason,
                    )
                    unavailable.append(event.document_id)
                else:
                    documents.append(event.document)
        return documents, unavailable

    def _apply_index_operations(
        self, summary: PipelineSummary, *, exclude_replace: bool = False
    ) -> None:
        operations = self.state.pending_index_operations()
        if not operations:
            return
        index = self._qdrant_index()
        index.ensure_collection()
        for operation in operations:
            kind = str(operation["operation"])
            document_id = str(operation["document_id"])
            if kind == "replace" and exclude_replace:
                continue
            if kind == "delete":
                index.delete_document(document_id)
                summary.deleted += 1
            elif kind == "payload":
                row = self.state.document(document_id)
                if row:
                    metadata = json.loads(str(row["metadata_json"]))
                    index.update_payload(document_id, _payload_metadata(metadata))
                    summary.payload_updated += 1
            else:
                continue
            with self.state.transaction() as connection:
                self.state.complete_index_operation(connection, document_id)

    def _active_documents(
        self,
        *,
        limit: int | None = None,
        document_id: str | None = None,
        attachment_key: str | None = None,
    ) -> list[SourceDocument]:
        documents: list[SourceDocument] = []
        for row in self.state.documents(active_only=True).values():
            if document_id and row.get("document_id") != document_id:
                continue
            if attachment_key and row.get("attachment_key") != attachment_key:
                continue
            metadata = json.loads(str(row["metadata_json"]))
            documents.append(SourceDocument.from_snapshot(metadata))
        documents.sort(key=lambda value: value.document_id)
        return documents[:limit] if limit is not None else documents

    def _mark_all_catalog_consumed(self, summary: PipelineSummary) -> None:
        entries = read_delta_catalog(self.config.source_delta_catalog_path)
        validate_delta_files(self.config.source_delta_catalog_path.parent, entries)
        with self.state.transaction() as connection:
            for entry in entries:
                self.state.mark_delta_consumed(
                    connection,
                    source="zotero",
                    sequence=entry.sequence,
                    sync_id=entry.sync_id,
                    delta_path=entry.delta_path,
                    delta_sha256=entry.delta_sha256,
                )
        summary.deltas_consumed = len(entries)

    def _endpoint_pool(self) -> Any:
        if self._pool is None:
            self._pool = EndpointPool.create(
                self.gpu_plan.embedding_endpoints,
                output_dim=self.config.embedding_dim,
                normalize=self.config.embedding_normalize,
                timeout_seconds=self.config.embedding_timeout_seconds,
                strategy=self.config.embedding_request_strategy,
            )
        return self._pool

    def _sparse_encoder(self) -> Any:
        if self._sparse is None:
            self._sparse = SparseEncoder(self.config)
        return self._sparse

    def _qdrant_index(self) -> Any:
        if self._index is None:
            self._index = QdrantIndex(
                self.config.qdrant_url,
                self.config.qdrant_collection,
                dense_dim=self.config.embedding_dim,
            )
        return self._index


def _package_version(parser_name: str) -> str:
    package = "PyMuPDF" if parser_name == "pymupdf" else parser_name
    try:
        return version(package)
    except PackageNotFoundError as exc:
        raise RuntimeError(f"parser package is not installed: {package}") from exc


def _payload_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    collections = value.get("collections") or []
    return {
        "attachment_key": value.get("attachment_key"),
        "collection_keys": [item.get("key") for item in collections],
        "collection_paths": [item.get("path") for item in collections],
        "doi": value.get("doi"),
        "document_id": value.get("document_id"),
        "source": value.get("source"),
        "tags": value.get("tags") or [],
        "title": value.get("title") or "",
        "year": value.get("year"),
    }


def _run_id() -> str:
    return f"rag-{uuid.uuid4().hex}"
