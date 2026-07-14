"""SQLite state store for the Zotero source."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .models import SyncSummary, ZoteroError

SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_raw(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ZoteroStateStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = data_dir / "state" / "zotero.sqlite3"

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(self.path):
            mode = self.path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ZoteroError(
                    "state_error", f"State database is not a regular non-symlink file: {self.path}"
                )
        with self.connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise ZoteroError(
                    "schema_error",
                    f"State schema {version} is newer than supported {SCHEMA_VERSION}",
                )
            if version == 0:
                connection.executescript(_SCHEMA_V1)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        os.chmod(self.path, 0o600)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def connect_readonly(self) -> sqlite3.Connection:
        """Open an existing state database without creating WAL/SHM side files."""

        if not os.path.lexists(self.path):
            raise ZoteroError("state_error", f"State database does not exist: {self.path}")
        mode = self.path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ZoteroError(
                "state_error", f"State database is not a regular non-symlink file: {self.path}"
            )
        # immutable=1 is appropriate for completed source checkpoints and is
        # required to prevent SQLite from creating WAL/SHM files beside a
        # database inspected by read-only commands such as `status`.
        uri = self.path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

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

    def library_state(self, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        owned = connection is None
        conn = connection or self.connect()
        try:
            row = conn.execute("SELECT * FROM library_state WHERE singleton = 1").fetchone()
            return dict(row) if row else None
        finally:
            if owned:
                conn.close()

    def bind_library(self, library_type: str, library_id: int) -> None:
        now = utc_now()
        with self.transaction() as connection:
            current = self.library_state(connection)
            if current is not None:
                if (
                    current["library_type"] != library_type
                    or int(current["library_id"]) != library_id
                ):
                    raise ZoteroError(
                        "library_binding_mismatch",
                        "The data directory is already bound to a different Zotero library",
                    )
                return
            connection.execute(
                """
                INSERT INTO library_state(
                    singleton, library_type, library_id, library_version, schema_version,
                    last_attempted_sync_at, last_successful_sync_at, active_sync_id
                ) VALUES (1, ?, ?, 0, ?, NULL, NULL, NULL)
                """,
                (library_type, library_id, SCHEMA_VERSION),
            )
            connection.execute(
                "INSERT OR IGNORE INTO mapping_validation(singleton, status, updated_at) VALUES (1, 'unverified', ?)",
                (now,),
            )

    def start_run(self, summary: SyncSummary) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE library_state SET last_attempted_sync_at = ? WHERE singleton = 1",
                (now,),
            )
            connection.execute(
                """
                INSERT INTO sync_runs(
                    sync_id, mode, started_at, from_version, target_version, committed_version, status,
                    added_count, updated_count, deleted_count, unchanged_count, attachments_ready,
                    attachments_missing, attachments_unstable, attachments_error, delta_upserts,
                    delta_deletes, duration_seconds, error_code, error_message
                ) VALUES (?, ?, ?, ?, NULL, NULL, 'running', 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, NULL, NULL)
                """,
                (summary.sync_id, summary.mode, now, summary.from_version),
            )

    def finish_run(self, summary: SyncSummary) -> None:
        with self.transaction() as connection:
            self.finish_run_in_transaction(connection, summary)

    def finish_run_in_transaction(
        self, connection: sqlite3.Connection, summary: SyncSummary
    ) -> None:
        connection.execute(
            """
            UPDATE sync_runs SET
                finished_at = ?, target_version = ?, committed_version = ?, status = ?,
                added_count = ?, updated_count = ?, deleted_count = ?, unchanged_count = ?,
                attachments_ready = ?, attachments_missing = ?, attachments_unstable = ?,
                attachments_error = ?, delta_upserts = ?, delta_deletes = ?, duration_seconds = ?,
                error_code = ?, error_message = ?
            WHERE sync_id = ?
            """,
            (
                utc_now(),
                summary.target_version,
                summary.committed_version,
                summary.status,
                summary.added,
                summary.updated,
                summary.deleted,
                summary.unchanged,
                summary.attachments_ready,
                summary.attachments_missing,
                summary.attachments_unstable,
                summary.attachments_error,
                summary.delta_upserts,
                summary.delta_deletes,
                summary.duration_seconds,
                summary.error_code,
                summary.error_message,
                summary.sync_id,
            ),
        )

    def load_objects(
        self,
        object_type: str | None = None,
        *,
        include_deleted: bool = False,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, dict[str, Any]]:
        owned = connection is None
        conn = connection or self.connect()
        where: list[str] = []
        params: list[Any] = []
        if object_type is not None:
            where.append("object_type = ?")
            params.append(object_type)
        if not include_deleted:
            where.append("deleted = 0")
        clause = " WHERE " + " AND ".join(where) if where else ""
        try:
            rows = conn.execute(
                f"SELECT * FROM objects{clause} ORDER BY object_key", params
            ).fetchall()
            return {str(row["object_key"]): dict(row) for row in rows}
        finally:
            if owned:
                conn.close()

    def load_collections(
        self,
        *,
        include_deleted: bool = False,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, dict[str, Any]]:
        owned = connection is None
        conn = connection or self.connect()
        clause = "" if include_deleted else " WHERE deleted = 0"
        try:
            rows = conn.execute(
                f"SELECT * FROM collections{clause} ORDER BY collection_key"
            ).fetchall()
            return {str(row["collection_key"]): dict(row) for row in rows}
        finally:
            if owned:
                conn.close()

    def load_attachments(
        self, connection: sqlite3.Connection | None = None
    ) -> dict[str, dict[str, Any]]:
        owned = connection is None
        conn = connection or self.connect()
        try:
            rows = conn.execute("SELECT * FROM attachments ORDER BY attachment_key").fetchall()
            result: dict[str, dict[str, Any]] = {}
            for row in rows:
                value = dict(row)
                value["pdf_candidates"] = json.loads(value.pop("pdf_candidates_json") or "[]")
                result[str(row["attachment_key"])] = value
            return result
        finally:
            if owned:
                conn.close()

    def load_documents(
        self,
        *,
        include_deleted: bool = True,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, dict[str, Any]]:
        owned = connection is None
        conn = connection or self.connect()
        clause = "" if include_deleted else " WHERE deleted = 0"
        try:
            rows = conn.execute(f"SELECT * FROM documents{clause} ORDER BY document_id").fetchall()
            return {str(row["document_id"]): dict(row) for row in rows}
        finally:
            if owned:
                conn.close()

    def upsert_remote_object(
        self,
        connection: sqlite3.Connection,
        object_type: str,
        payload: Mapping[str, Any],
    ) -> tuple[str, bool]:
        data_value = payload.get("data")
        data: Mapping[str, Any] = data_value if isinstance(data_value, Mapping) else payload
        key = str(payload.get("key") or data.get("key") or "")
        if not key:
            raise ZoteroError("invalid_response", f"Remote {object_type} has no key")
        try:
            version = int(payload.get("version") or data.get("version") or 0)
        except (TypeError, ValueError) as exc:
            raise ZoteroError(
                "invalid_response", f"Remote object {key} has invalid version"
            ) from exc
        parent = data.get("parentItem") if object_type == "item" else None
        raw = canonical_raw(payload)
        row = connection.execute(
            "SELECT object_version, raw_json, deleted FROM objects WHERE object_type = ? AND object_key = ?",
            (object_type, key),
        ).fetchone()
        changed = bool(
            row is None
            or int(row["object_version"]) != version
            or row["raw_json"] != raw
            or row["deleted"]
        )
        now = utc_now()
        connection.execute(
            """
            INSERT INTO objects(object_key, object_type, object_version, parent_item_key, deleted, raw_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, ?)
            ON CONFLICT(object_type, object_key) DO UPDATE SET
                object_version = excluded.object_version,
                parent_item_key = excluded.parent_item_key,
                deleted = 0,
                raw_json = excluded.raw_json,
                updated_at = CASE WHEN objects.raw_json != excluded.raw_json OR objects.deleted != 0
                                  THEN excluded.updated_at ELSE objects.updated_at END
            """,
            (key, object_type, version, parent, raw, now, now),
        )
        return key, changed

    def upsert_collection(
        self, connection: sqlite3.Connection, payload: Mapping[str, Any]
    ) -> tuple[str, bool]:
        data_value = payload.get("data")
        data: Mapping[str, Any] = data_value if isinstance(data_value, Mapping) else payload
        key = str(payload.get("key") or data.get("key") or "")
        if not key:
            raise ZoteroError("invalid_response", "Remote collection has no key")
        version = int(payload.get("version") or data.get("version") or 0)
        parent = data.get("parentCollection") or None
        name = str(data.get("name") or "")
        raw = canonical_raw(payload)
        row = connection.execute(
            "SELECT collection_version, raw_json, deleted FROM collections WHERE collection_key = ?",
            (key,),
        ).fetchone()
        changed = bool(
            row is None
            or int(row["collection_version"]) != version
            or row["raw_json"] != raw
            or row["deleted"]
        )
        now = utc_now()
        connection.execute(
            """
            INSERT INTO collections(collection_key, collection_version, parent_collection_key, name, path, deleted, raw_json, updated_at)
            VALUES (?, ?, ?, ?, '', 0, ?, ?)
            ON CONFLICT(collection_key) DO UPDATE SET
                collection_version = excluded.collection_version,
                parent_collection_key = excluded.parent_collection_key,
                name = excluded.name,
                deleted = 0,
                raw_json = excluded.raw_json,
                updated_at = CASE WHEN collections.raw_json != excluded.raw_json OR collections.deleted != 0
                                  THEN excluded.updated_at ELSE collections.updated_at END
            """,
            (key, version, parent, name, raw, now),
        )
        return key, changed

    def update_collection_paths(
        self, connection: sqlite3.Connection, paths: Mapping[str, str]
    ) -> None:
        for key, path in paths.items():
            connection.execute(
                "UPDATE collections SET path = ? WHERE collection_key = ?", (path, key)
            )

    def mark_deleted(
        self,
        connection: sqlite3.Connection,
        object_type: str,
        key: str,
        *,
        sync_id: str,
    ) -> bool:
        table = "collections" if object_type == "collection" else "objects"
        key_column = "collection_key" if object_type == "collection" else "object_key"
        extra = "" if object_type == "collection" else " AND object_type = ?"
        params: tuple[Any, ...] = (key,) if object_type == "collection" else (key, object_type)
        row = connection.execute(
            f"SELECT deleted FROM {table} WHERE {key_column} = ?{extra}", params
        ).fetchone()
        if row is None or row["deleted"]:
            return False
        connection.execute(
            f"UPDATE {table} SET deleted = 1, updated_at = ? WHERE {key_column} = ?{extra}",
            (utc_now(), *params),
        )
        connection.execute(
            "INSERT INTO deletion_events(sync_id, object_type, object_key, deleted_at) VALUES (?, ?, ?, ?)",
            (sync_id, object_type, key, utc_now()),
        )
        return True

    def upsert_attachment(self, connection: sqlite3.Connection, value: Mapping[str, Any]) -> None:
        columns = (
            "attachment_key",
            "parent_item_key",
            "attachment_version",
            "link_mode",
            "mime_type",
            "api_filename",
            "archive_path",
            "prop_path",
            "prop_exists",
            "archive_sha256",
            "archive_size_bytes",
            "archive_mtime_ns",
            "pdf_path",
            "pdf_sha256",
            "pdf_size_bytes",
            "resolver_status",
            "resolver_error",
            "pdf_candidates_json",
            "updated_at",
        )
        normalized = dict(value)
        normalized["pdf_candidates_json"] = json.dumps(
            normalized.pop("pdf_candidates", []),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        normalized.setdefault("updated_at", utc_now())
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(f"{name}=excluded.{name}" for name in columns[1:])
        connection.execute(
            f"INSERT INTO attachments({','.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(attachment_key) DO UPDATE SET {updates}",
            tuple(normalized.get(name) for name in columns),
        )

    def retain_attachment_projections(
        self, connection: sqlite3.Connection, attachment_keys: Sequence[str]
    ) -> None:
        """Remove derived projections that no longer correspond to a current attachment item."""

        keys = sorted(set(attachment_keys))
        if not keys:
            connection.execute("DELETE FROM attachments")
            return
        connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS current_attachment_keys (attachment_key TEXT PRIMARY KEY)"
        )
        connection.execute("DELETE FROM current_attachment_keys")
        connection.executemany(
            "INSERT INTO current_attachment_keys(attachment_key) VALUES (?)",
            ((key,) for key in keys),
        )
        connection.execute(
            "DELETE FROM attachments WHERE NOT EXISTS "
            "(SELECT 1 FROM current_attachment_keys current "
            "WHERE current.attachment_key = attachments.attachment_key)"
        )
        connection.execute("DROP TABLE current_attachment_keys")

    def upsert_document(self, connection: sqlite3.Connection, value: Mapping[str, Any]) -> None:
        now = utc_now()
        connection.execute(
            """
            INSERT INTO documents(
                document_id, parent_item_key, attachment_key, pdf_index, metadata_fingerprint,
                content_fingerprint, document_fingerprint, status, deleted, delete_reason,
                manifest_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                parent_item_key=excluded.parent_item_key, attachment_key=excluded.attachment_key,
                pdf_index=excluded.pdf_index, metadata_fingerprint=excluded.metadata_fingerprint,
                content_fingerprint=excluded.content_fingerprint, document_fingerprint=excluded.document_fingerprint,
                status=excluded.status, deleted=excluded.deleted, delete_reason=excluded.delete_reason,
                manifest_json=excluded.manifest_json,
                updated_at=CASE WHEN documents.document_fingerprint != excluded.document_fingerprint
                                  OR documents.deleted != excluded.deleted
                                THEN excluded.updated_at ELSE documents.updated_at END
            """,
            (
                value["document_id"],
                value["parent_item_key"],
                value["attachment_key"],
                int(value.get("pdf_index", 0)),
                value["metadata_fingerprint"],
                value.get("content_fingerprint"),
                value["document_fingerprint"],
                value["status"],
                int(bool(value.get("deleted", False))),
                value.get("delete_reason"),
                value["manifest_json"],
                value.get("updated_at", now),
            ),
        )

    def mark_document_deleted(
        self, connection: sqlite3.Connection, document_id: str, reason: str
    ) -> None:
        connection.execute(
            "UPDATE documents SET deleted = 1, delete_reason = ?, updated_at = ? WHERE document_id = ?",
            (reason, utc_now(), document_id),
        )

    def set_mapping_validation(
        self,
        connection: sqlite3.Connection,
        *,
        status: str,
        library_type: str,
        library_id: int,
        webdav_realpath: str,
        sample_count: int,
        passed_count: int,
        summary: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO mapping_validation(
                singleton, status, library_type, library_id, webdav_realpath, sample_count,
                passed_count, summary_json, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET status=excluded.status, library_type=excluded.library_type,
                library_id=excluded.library_id, webdav_realpath=excluded.webdav_realpath,
                sample_count=excluded.sample_count, passed_count=excluded.passed_count,
                summary_json=excluded.summary_json, updated_at=excluded.updated_at
            """,
            (
                status,
                library_type,
                library_id,
                webdav_realpath,
                sample_count,
                passed_count,
                json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                utc_now(),
            ),
        )

    def mapping_validation(
        self, connection: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        owned = connection is None
        conn = connection or self.connect()
        try:
            row = conn.execute("SELECT * FROM mapping_validation WHERE singleton = 1").fetchone()
            return dict(row) if row else None
        finally:
            if owned:
                conn.close()

    def set_success_version(
        self,
        connection: sqlite3.Connection,
        *,
        version: int,
        sync_id: str,
    ) -> None:
        connection.execute(
            """
            UPDATE library_state SET library_version = ?, last_successful_sync_at = ?, active_sync_id = ?
            WHERE singleton = 1
            """,
            (version, utc_now(), sync_id),
        )

    def quick_check(self, connection: sqlite3.Connection | None = None) -> list[str]:
        owned = connection is None
        conn = connection or self.connect_readonly()
        try:
            return [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
        finally:
            if owned:
                conn.close()

    def recent_runs(
        self,
        limit: int = 10,
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        owned = connection is None
        conn = connection or self.connect_readonly()
        try:
            rows = conn.execute(
                "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            if owned:
                conn.close()


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS library_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    library_type TEXT NOT NULL,
    library_id INTEGER NOT NULL,
    library_version INTEGER NOT NULL DEFAULT 0,
    schema_version INTEGER NOT NULL,
    last_attempted_sync_at TEXT,
    last_successful_sync_at TEXT,
    active_sync_id TEXT
);

CREATE TABLE IF NOT EXISTS objects (
    object_key TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_version INTEGER NOT NULL,
    parent_item_key TEXT,
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0,1)),
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (object_type, object_key)
);
CREATE INDEX IF NOT EXISTS idx_objects_parent ON objects(parent_item_key);

CREATE TABLE IF NOT EXISTS collections (
    collection_key TEXT PRIMARY KEY,
    collection_version INTEGER NOT NULL,
    parent_collection_key TEXT,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0,1)),
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    attachment_key TEXT PRIMARY KEY,
    parent_item_key TEXT,
    attachment_version INTEGER NOT NULL,
    link_mode TEXT,
    mime_type TEXT,
    api_filename TEXT,
    archive_path TEXT,
    prop_path TEXT,
    prop_exists INTEGER NOT NULL DEFAULT 0,
    archive_sha256 TEXT,
    archive_size_bytes INTEGER,
    archive_mtime_ns INTEGER,
    pdf_path TEXT,
    pdf_sha256 TEXT,
    pdf_size_bytes INTEGER,
    resolver_status TEXT NOT NULL,
    resolver_error TEXT,
    pdf_candidates_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_parent ON attachments(parent_item_key);

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    parent_item_key TEXT NOT NULL,
    attachment_key TEXT NOT NULL,
    pdf_index INTEGER NOT NULL,
    metadata_fingerprint TEXT NOT NULL,
    content_fingerprint TEXT,
    document_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0,1)),
    delete_reason TEXT,
    manifest_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_parent ON documents(parent_item_key);
CREATE INDEX IF NOT EXISTS idx_documents_attachment ON documents(attachment_key);

CREATE TABLE IF NOT EXISTS sync_runs (
    sync_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    from_version INTEGER NOT NULL,
    target_version INTEGER,
    committed_version INTEGER,
    status TEXT NOT NULL,
    added_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    deleted_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    attachments_ready INTEGER NOT NULL DEFAULT 0,
    attachments_missing INTEGER NOT NULL DEFAULT 0,
    attachments_unstable INTEGER NOT NULL DEFAULT 0,
    attachments_error INTEGER NOT NULL DEFAULT 0,
    delta_upserts INTEGER NOT NULL DEFAULT 0,
    delta_deletes INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS mapping_validation (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    status TEXT NOT NULL,
    library_type TEXT,
    library_id INTEGER,
    webdav_realpath TEXT,
    sample_count INTEGER NOT NULL DEFAULT 0,
    passed_count INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deletion_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_key TEXT NOT NULL,
    deleted_at TEXT NOT NULL
);
"""
