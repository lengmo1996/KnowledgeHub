"""Transactional SQLite state for derived RAG artifacts."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from knowledgehub.core.hashing import canonical_json_dumps
from knowledgehub.pipeline.models import ChunkRecord, SourceDocument

PIPELINE_DB_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PipelineState:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = data_dir / "state" / "pipeline.sqlite3"

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > PIPELINE_DB_VERSION:
                raise RuntimeError(f"pipeline database version {version} is newer than supported")
            if version == 0:
                connection.executescript(_SCHEMA)
                connection.execute(f"PRAGMA user_version={PIPELINE_DB_VERSION}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def begin_run(self, run_id: str, mode: str, gpu_plan: Mapping[str, Any]) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO pipeline_runs(run_id, mode, status, started_at, gpu_plan_json)
                VALUES (?, ?, 'running', ?, ?)
                """,
                (run_id, mode, utc_now(), canonical_json_dumps(gpu_plan)),
            )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        summary: Mapping[str, Any],
        error: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE pipeline_runs
                SET status=?, finished_at=?, summary_json=?, error=?
                WHERE run_id=?
                """,
                (status, utc_now(), canonical_json_dumps(summary), error, run_id),
            )

    def document(self, document_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_documents WHERE document_id=?", (document_id,)
            ).fetchone()
        return dict(row) if row else None

    def documents(self, *, active_only: bool = False) -> dict[str, dict[str, Any]]:
        query = "SELECT * FROM pipeline_documents"
        if active_only:
            query += " WHERE source_status='ready'"
        with self.connect() as connection:
            rows = connection.execute(query).fetchall()
        return {str(row["document_id"]): dict(row) for row in rows}

    def upsert_source_document(
        self, connection: sqlite3.Connection, document: SourceDocument
    ) -> None:
        connection.execute(
            """
            INSERT INTO pipeline_documents(
                source, document_id, attachment_key, source_document_fingerprint,
                source_metadata_fingerprint, source_content_fingerprint, source_status,
                pdf_path, pdf_sha256, metadata_json, last_processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                attachment_key=excluded.attachment_key,
                source_document_fingerprint=excluded.source_document_fingerprint,
                source_metadata_fingerprint=excluded.source_metadata_fingerprint,
                source_content_fingerprint=excluded.source_content_fingerprint,
                source_status=excluded.source_status,
                pdf_path=excluded.pdf_path,
                pdf_sha256=excluded.pdf_sha256,
                metadata_json=excluded.metadata_json,
                last_processed_at=excluded.last_processed_at,
                last_error=NULL
            """,
            (
                document.source,
                document.document_id,
                document.attachment_key,
                document.source_document_fingerprint,
                document.source_metadata_fingerprint,
                document.source_content_fingerprint,
                document.source_status,
                str(document.pdf_path),
                document.pdf_sha256,
                canonical_json_dumps(document.metadata),
                utc_now(),
            ),
        )

    def mark_unavailable(
        self,
        connection: sqlite3.Connection,
        document_id: str,
        *,
        status: str,
        reason: str,
    ) -> None:
        connection.execute(
            """
            UPDATE pipeline_documents SET
                source_status=?, parse_status='stale', chunk_status='stale',
                embedding_status='stale', dense_index_status='stale',
                sparse_index_status='stale', last_error=?, last_processed_at=?
            WHERE document_id=?
            """,
            (status, reason, utc_now(), document_id),
        )
        connection.execute("UPDATE chunks SET active=0 WHERE document_id=?", (document_id,))
        self.enqueue_index_operation(connection, document_id, "delete", reason)

    def update_stage(
        self,
        connection: sqlite3.Connection,
        document_id: str,
        stage: str,
        *,
        status: str,
        fingerprint: str | None = None,
        values: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        allowed = {
            "parse": ("parse_status", "parse_fingerprint"),
            "chunk": ("chunk_status", "chunk_fingerprint"),
            "embedding": ("embedding_status", "embedding_fingerprint"),
            "dense_index": ("dense_index_status", None),
            "sparse_index": ("sparse_index_status", None),
        }
        if stage not in allowed:
            raise ValueError(f"unsupported pipeline stage: {stage}")
        status_column, fingerprint_column = allowed[stage]
        updates = [f"{status_column}=?", "last_processed_at=?", "last_error=?"]
        parameters: list[Any] = [status, utc_now(), error]
        if fingerprint_column is not None:
            updates.append(f"{fingerprint_column}=?")
            parameters.append(fingerprint)
        extra_allowed = {
            "parser_name",
            "parser_version",
            "chunk_count",
            "embedding_model",
            "embedding_revision",
            "embedding_dim",
            "assigned_parse_worker",
        }
        for key, value in (values or {}).items():
            if key not in extra_allowed:
                raise ValueError(f"unsupported pipeline document field: {key}")
            updates.append(f"{key}=?")
            parameters.append(value)
        parameters.append(document_id)
        connection.execute(
            f"UPDATE pipeline_documents SET {', '.join(updates)} WHERE document_id=?",
            parameters,
        )

    def replace_chunks(
        self,
        connection: sqlite3.Connection,
        document_id: str,
        chunks: Sequence[ChunkRecord],
    ) -> None:
        connection.execute("UPDATE chunks SET active=0 WHERE document_id=?", (document_id,))
        now = utc_now()
        for chunk in chunks:
            connection.execute(
                """
                INSERT INTO chunks(
                    chunk_id, document_id, attachment_key, chunk_index, text_sha256,
                    chunk_fingerprint, page_start, page_end, section_path_json,
                    token_count, active, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    text_sha256=excluded.text_sha256,
                    chunk_fingerprint=excluded.chunk_fingerprint,
                    page_start=excluded.page_start,
                    page_end=excluded.page_end,
                    section_path_json=excluded.section_path_json,
                    token_count=excluded.token_count,
                    active=1,
                    updated_at=excluded.updated_at
                """,
                (
                    chunk.chunk_id,
                    document_id,
                    chunk.attachment_key,
                    chunk.chunk_index,
                    chunk.text_sha256,
                    chunk.chunk_fingerprint,
                    chunk.page_start,
                    chunk.page_end,
                    canonical_json_dumps(list(chunk.section_path)),
                    chunk.token_count,
                    now,
                ),
            )

    def last_consumed_delta(self, source: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM consumed_deltas
                WHERE source=? AND status='success' ORDER BY sequence DESC LIMIT 1
                """,
                (source,),
            ).fetchone()
        return dict(row) if row else None

    def mark_delta_consumed(
        self,
        connection: sqlite3.Connection,
        *,
        source: str,
        sequence: int,
        sync_id: str,
        delta_path: str,
        delta_sha256: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO consumed_deltas(
                source, sequence, sync_id, delta_path, delta_sha256, consumed_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'success')
            ON CONFLICT(source, sync_id) DO UPDATE SET
                sequence=excluded.sequence,
                delta_path=excluded.delta_path,
                delta_sha256=excluded.delta_sha256,
                consumed_at=excluded.consumed_at,
                status='success'
            """,
            (source, sequence, sync_id, delta_path, delta_sha256, utc_now()),
        )

    def enqueue_index_operation(
        self,
        connection: sqlite3.Connection,
        document_id: str,
        operation: str,
        reason: str,
    ) -> None:
        if operation not in {"delete", "replace", "payload"}:
            raise ValueError("invalid index operation")
        connection.execute(
            """
            INSERT INTO index_operations(document_id, operation, reason, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            ON CONFLICT(document_id) DO UPDATE SET
                operation=excluded.operation, reason=excluded.reason,
                status='pending', created_at=excluded.created_at, last_error=NULL
            """,
            (document_id, operation, reason, utc_now()),
        )

    def pending_index_operations(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM index_operations WHERE status='pending' ORDER BY document_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def complete_index_operation(
        self, connection: sqlite3.Connection, document_id: str
    ) -> None:
        connection.execute(
            "UPDATE index_operations SET status='success', completed_at=? WHERE document_id=?",
            (utc_now(), document_id),
        )


_SCHEMA = """
CREATE TABLE pipeline_documents (
    source TEXT NOT NULL,
    document_id TEXT PRIMARY KEY,
    attachment_key TEXT NOT NULL,
    source_document_fingerprint TEXT NOT NULL,
    source_metadata_fingerprint TEXT NOT NULL,
    source_content_fingerprint TEXT NOT NULL,
    source_status TEXT NOT NULL,
    pdf_path TEXT NOT NULL,
    pdf_sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    parse_status TEXT NOT NULL DEFAULT 'pending',
    parse_fingerprint TEXT,
    parser_name TEXT,
    parser_version TEXT,
    chunk_status TEXT NOT NULL DEFAULT 'pending',
    chunk_fingerprint TEXT,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    embedding_status TEXT NOT NULL DEFAULT 'pending',
    embedding_fingerprint TEXT,
    embedding_model TEXT,
    embedding_revision TEXT,
    embedding_dim INTEGER,
    dense_index_status TEXT NOT NULL DEFAULT 'pending',
    sparse_index_status TEXT NOT NULL DEFAULT 'pending',
    assigned_parse_worker TEXT,
    last_processed_at TEXT NOT NULL,
    last_error TEXT
);
CREATE INDEX pipeline_documents_attachment_idx ON pipeline_documents(attachment_key);

CREATE TABLE chunks (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES pipeline_documents(document_id),
    attachment_key TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    text_sha256 TEXT NOT NULL,
    chunk_fingerprint TEXT NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    section_path_json TEXT NOT NULL DEFAULT '[]',
    token_count INTEGER NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    updated_at TEXT NOT NULL,
    UNIQUE(document_id, chunk_index, chunk_fingerprint)
);
CREATE INDEX chunks_document_idx ON chunks(document_id, active);

CREATE TABLE pipeline_runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    gpu_plan_json TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);

CREATE TABLE work_queue (
    document_id TEXT NOT NULL REFERENCES pipeline_documents(document_id),
    stage TEXT NOT NULL,
    partition_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    worker_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    last_error TEXT,
    PRIMARY KEY(document_id, stage)
);

CREATE TABLE consumed_deltas (
    source TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    sync_id TEXT NOT NULL,
    delta_path TEXT NOT NULL,
    delta_sha256 TEXT NOT NULL,
    consumed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(source, sync_id),
    UNIQUE(source, sequence)
);

CREATE TABLE index_operations (
    document_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    last_error TEXT
);
"""
