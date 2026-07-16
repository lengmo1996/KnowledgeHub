"""Shared idempotent Chunk-to-vector indexing for derived knowledge bases."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from knowledgehub.core.atomic import atomic_write_jsonl
from knowledgehub.core.hashing import sha256_json
from knowledgehub.core.models import KnowledgeDocument
from knowledgehub.embeddings.endpoint_pool import EndpointPool
from knowledgehub.indexing.qdrant import QdrantIndex
from knowledgehub.indexing.sparse import SparseEncoder
from knowledgehub.pipeline.config import RagConfig
from knowledgehub.pipeline.models import ChunkRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class IndexInput:
    document: KnowledgeDocument
    chunks: tuple[ChunkRecord, ...]
    processor_version: str


@dataclass(slots=True)
class IndexBuildSummary:
    knowledge_base: str
    selected: int = 0
    indexed: int = 0
    skipped: int = 0
    tombstoned: int = 0
    chunks: int = 0
    dry_run: bool = False
    failures: list[dict[str, str]] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "failed" if self.failures else "success"

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunks": self.chunks,
            "dry_run": self.dry_run,
            "failures": self.failures,
            "indexed": self.indexed,
            "knowledge_base": self.knowledge_base,
            "selected": self.selected,
            "skipped": self.skipped,
            "status": self.status,
            "tombstoned": self.tombstoned,
        }


class DomainIndexState:
    def __init__(self, data_dir: Path, *, initialize: bool = True) -> None:
        self.path = data_dir / "state" / "index.sqlite3"
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS documents (
                        document_id TEXT PRIMARY KEY,
                        content_hash TEXT NOT NULL,
                        metadata_hash TEXT NOT NULL,
                        processor_version TEXT NOT NULL,
                        embedding_fingerprint TEXT NOT NULL,
                        active INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS tombstones (
                        document_id TEXT PRIMARY KEY,
                        deleted_at TEXT NOT NULL,
                        reason TEXT NOT NULL
                    );
                    """
                )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def documents(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM documents").fetchall()
        return {str(row["document_id"]): dict(row) for row in rows}

    def upsert(
        self,
        document_id: str,
        *,
        content_hash: str,
        metadata_hash: str,
        processor_version: str,
        embedding_fingerprint: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO documents
                   (document_id, content_hash, metadata_hash, processor_version,
                    embedding_fingerprint, active, updated_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(document_id) DO UPDATE SET
                     content_hash=excluded.content_hash,
                     metadata_hash=excluded.metadata_hash,
                     processor_version=excluded.processor_version,
                     embedding_fingerprint=excluded.embedding_fingerprint,
                     active=1,
                     updated_at=excluded.updated_at""",
                (
                    document_id,
                    content_hash,
                    metadata_hash,
                    processor_version,
                    embedding_fingerprint,
                    _now(),
                ),
            )
            connection.execute("DELETE FROM tombstones WHERE document_id = ?", (document_id,))

    def tombstone(self, document_id: str, reason: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE documents SET active=0, updated_at=? WHERE document_id=?",
                (_now(), document_id),
            )
            connection.execute(
                """INSERT INTO tombstones(document_id, deleted_at, reason) VALUES (?, ?, ?)
                   ON CONFLICT(document_id) DO UPDATE SET
                     deleted_at=excluded.deleted_at, reason=excluded.reason""",
                (document_id, _now(), reason),
            )


class IncrementalChunkIndexer:
    def __init__(
        self,
        config: RagConfig,
        *,
        endpoint_pool: Any | None = None,
        sparse_encoder: Any | None = None,
        index: Any | None = None,
        initialize: bool = True,
    ) -> None:
        self.config = config
        self.data_dir = config.data_dir
        self.state = DomainIndexState(self.data_dir, initialize=initialize)
        self.endpoint_pool = endpoint_pool
        self.sparse_encoder = sparse_encoder
        self.index = index

    def _components(self) -> tuple[Any, Any, Any]:
        if self.endpoint_pool is None:
            self.endpoint_pool = EndpointPool.create(
                self.config.embedding_endpoints,
                output_dim=self.config.embedding_dim,
                normalize=self.config.embedding_normalize,
                timeout_seconds=self.config.embedding_timeout_seconds,
                strategy=self.config.embedding_request_strategy,
                api_key=self.config.embedding_api_key.get_secret_value(),
            )
        if self.sparse_encoder is None:
            self.sparse_encoder = SparseEncoder(self.config)
        if self.index is None:
            self.index = QdrantIndex(
                self.config.qdrant_url,
                self.config.qdrant_collection,
                dense_dim=self.config.embedding_dim,
                upsert_batch_size=self.config.qdrant_upsert_batch_size,
            )
        return self.endpoint_pool, self.sparse_encoder, self.index

    def close(self) -> None:
        if self.endpoint_pool is not None and hasattr(self.endpoint_pool, "close"):
            self.endpoint_pool.close()

    def build(
        self,
        values: Sequence[IndexInput],
        *,
        knowledge_base: str,
        dry_run: bool = False,
        prune: bool = False,
    ) -> IndexBuildSummary:
        summary = IndexBuildSummary(
            knowledge_base=knowledge_base, selected=len(values), dry_run=dry_run
        )
        existing = self.state.documents()
        fingerprint = sha256_json(
            {
                "model": self.config.embedding_model,
                "revision": self.config.embedding_revision,
                "dimension": self.config.embedding_dim,
                "sparse": self.config.sparse_model,
            }
        )
        planned: list[tuple[IndexInput, str]] = []
        for value in values:
            value.document.validate()
            metadata_hash = sha256_json(value.document.metadata)
            old = existing.get(value.document.document_id)
            unchanged = bool(
                old
                and old["active"]
                and old["content_hash"] == value.document.content_hash
                and old["metadata_hash"] == metadata_hash
                and old["processor_version"] == value.processor_version
                and old["embedding_fingerprint"] == fingerprint
            )
            if unchanged:
                summary.skipped += 1
            else:
                planned.append((value, metadata_hash))
        current_ids = {value.document.document_id for value in values}
        stale = sorted(
            document_id
            for document_id, row in existing.items()
            if row["active"] and document_id not in current_ids
        ) if prune else []
        if dry_run:
            summary.indexed = len(planned)
            summary.tombstoned = len(stale)
            summary.chunks = sum(len(value.chunks) for value, _ in planned)
            return summary
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.data_dir / "chunks").mkdir(parents=True, exist_ok=True, mode=0o700)
        pool, sparse_encoder, index = self._components()
        index.ensure_collection()
        for value, metadata_hash in planned:
            try:
                chunks = value.chunks
                texts = [chunk.text for chunk in chunks]
                dense: list[Sequence[float]] = []
                for offset in range(0, len(texts), self.config.embedding_batch_size):
                    result = pool.embed(texts[offset : offset + self.config.embedding_batch_size])
                    dense.extend(result.vectors)
                sparse = sparse_encoder.encode(texts)
                atomic_write_jsonl(
                    self.data_dir / "chunks" / f"{sha256_json(value.document.document_id)[:32]}.jsonl",
                    [chunk.to_dict() for chunk in chunks],
                )
                index.replace_document(
                    value.document.document_id,
                    chunks,
                    dense,
                    sparse,
                    embedding_metadata={
                        "embedding_model": self.config.embedding_model,
                        "embedding_revision": self.config.embedding_revision,
                    },
                )
                self.state.upsert(
                    value.document.document_id,
                    content_hash=value.document.content_hash,
                    metadata_hash=metadata_hash,
                    processor_version=value.processor_version,
                    embedding_fingerprint=fingerprint,
                )
                summary.indexed += 1
                summary.chunks += len(chunks)
            except Exception as exc:
                summary.failures.append(
                    {"document_id": value.document.document_id, "error": str(exc)}
                )
        for document_id in stale:
            try:
                index.delete_document(document_id)
                self.state.tombstone(document_id, "missing_from_complete_build")
                summary.tombstoned += 1
            except Exception as exc:
                summary.failures.append({"document_id": document_id, "error": str(exc)})
        manifest = {
            **summary.to_dict(),
            "collection": self.config.qdrant_collection,
            "embedding_model": self.config.embedding_model,
            "embedding_revision": self.config.embedding_revision,
            "finished_at": _now(),
        }
        atomic_write_jsonl(
            self.data_dir / "runs" / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl",
            [manifest],
        )
        return summary
