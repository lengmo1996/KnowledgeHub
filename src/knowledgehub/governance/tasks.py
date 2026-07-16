"""Durable unified task states, idempotency keys and expiring locks."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from knowledgehub.core.hashing import canonical_json_dumps, sha256_json

STATUSES = {"pending", "running", "completed", "partial", "failed", "cancelled"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                  task_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                  task_type TEXT NOT NULL, status TEXT NOT NULL, knowledge_base TEXT,
                  library TEXT, version TEXT, started_at TEXT NOT NULL, ended_at TEXT,
                  input_manifest TEXT, output_manifest TEXT, error_summary TEXT,
                  retry_count INTEGER NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS locks (
                  lock_key TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                  acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL
                );
                """
            )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def begin(
        self,
        task_type: str,
        *,
        knowledge_base: str | None = None,
        library: str | None = None,
        version: str | None = None,
        inputs: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = sha256_json(
            {"task_type": task_type, "knowledge_base": knowledge_base, "library": library, "version": version, "inputs": inputs or {}}
        )
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM tasks WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing and existing["status"] in {"running", "completed"}:
                return dict(existing)
            task_id = str(existing["task_id"]) if existing else str(uuid.uuid4())
            retries = int(existing["retry_count"]) + 1 if existing else 0
            connection.execute(
                """INSERT INTO tasks(task_id,idempotency_key,task_type,status,knowledge_base,library,version,started_at,retry_count,metadata_json)
                   VALUES(?,?,?,'running',?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET status='running',started_at=excluded.started_at,ended_at=NULL,error_summary=NULL,retry_count=excluded.retry_count""",
                (task_id, key, task_type, knowledge_base, library, version, _now().isoformat(), retries, canonical_json_dumps(inputs or {})),
            )
            return dict(connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())

    def finish(
        self,
        task_id: str,
        status: str,
        *,
        output_manifest: str | None = None,
        error: str | None = None,
    ) -> None:
        if status not in STATUSES - {"pending", "running"}:
            raise ValueError("invalid terminal task status")
        with self.connect() as connection:
            changed = connection.execute(
                "UPDATE tasks SET status=?,ended_at=?,output_manifest=?,error_summary=? WHERE task_id=?",
                (status, _now().isoformat(), output_manifest, error, task_id),
            ).rowcount
            if not changed:
                raise ValueError(f"unknown task: {task_id}")

    def acquire(self, lock_key: str, task_id: str, *, ttl_seconds: int = 3600) -> None:
        now = _now()
        with self.connect() as connection:
            connection.execute("DELETE FROM locks WHERE expires_at <= ?", (now.isoformat(),))
            try:
                connection.execute(
                    "INSERT INTO locks VALUES(?,?,?,?)",
                    (lock_key, task_id, now.isoformat(), (now + timedelta(seconds=ttl_seconds)).isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeError(f"lock is held: {lock_key}") from exc

    def release(self, lock_key: str, *, task_id: str | None = None, force: bool = False) -> None:
        with self.connect() as connection:
            if force:
                connection.execute("DELETE FROM locks WHERE lock_key=?", (lock_key,))
            elif task_id:
                connection.execute("DELETE FROM locks WHERE lock_key=? AND task_id=?", (lock_key, task_id))
            else:
                raise ValueError("task_id is required unless force is true")

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
