"""Durable unified task states, idempotency keys and expiring locks."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from os import environ
from pathlib import Path
from threading import Event, Thread
from typing import Any, Mapping

from knowledgehub.core.hashing import canonical_json_dumps, sha256_json

STATUSES = {"pending", "running", "completed", "partial", "failed", "cancelled"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def default_task_store_path() -> Path:
    return Path(environ.get("KH_STATE_ROOT", "/data/KnowledgeHub/state")) / "tasks.sqlite3"


class TaskConflictError(RuntimeError):
    """Raised when an equivalent task or protected resource is already running."""


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
                CREATE TABLE IF NOT EXISTS attempts (
                  attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                  attempt_number INTEGER NOT NULL, status TEXT NOT NULL,
                  started_at TEXT NOT NULL, ended_at TEXT,
                  output_manifest TEXT, error_summary TEXT, result_json TEXT,
                  UNIQUE(task_id, attempt_number)
                );
                CREATE INDEX IF NOT EXISTS attempts_task_idx
                  ON attempts(task_id, attempt_number DESC);
                """
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(tasks)")
            }
            if "result_json" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN result_json TEXT")

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
        input_manifest: str | None = None,
        reuse_completed: bool = True,
        stale_after_seconds: int = 21600,
    ) -> dict[str, Any]:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        key = sha256_json(
            {"task_type": task_type, "knowledge_base": knowledge_base, "library": library, "version": version, "inputs": inputs or {}}
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM tasks WHERE idempotency_key=?", (key,)
            ).fetchone()
            if (
                existing
                and existing["status"] == "running"
                and self._is_stale(str(existing["started_at"]), stale_after_seconds)
                and not self._has_live_lock(connection, str(existing["task_id"]))
            ):
                recovered_at = _now().isoformat()
                connection.execute(
                    """UPDATE tasks SET status='failed',ended_at=?,
                       error_summary='stale_task_recovered' WHERE task_id=?""",
                    (recovered_at, existing["task_id"]),
                )
                connection.execute(
                    """UPDATE attempts SET status='failed',ended_at=?,
                       error_summary='stale_task_recovered'
                       WHERE task_id=? AND status='running'""",
                    (recovered_at, existing["task_id"]),
                )
                connection.execute(
                    "DELETE FROM locks WHERE task_id=?", (existing["task_id"],)
                )
                existing = connection.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (existing["task_id"],)
                ).fetchone()
            if existing and (
                existing["status"] == "running"
                or (existing["status"] == "completed" and reuse_completed)
            ):
                return self._execution(dict(existing), required=False, reused=True)
            task_id = str(existing["task_id"]) if existing else str(uuid.uuid4())
            previous_status = str(existing["status"]) if existing else None
            retries = int(existing["retry_count"]) if existing else 0
            if previous_status in {"failed", "partial"}:
                retries += 1
            connection.execute(
                """INSERT INTO tasks(
                     task_id,idempotency_key,task_type,status,knowledge_base,library,
                     version,started_at,input_manifest,retry_count,metadata_json,result_json
                   ) VALUES(?,?,?,'running',?,?,?,?,?,?,?,NULL)
                   ON CONFLICT(task_id) DO UPDATE SET
                     status='running',started_at=excluded.started_at,ended_at=NULL,
                     input_manifest=excluded.input_manifest,output_manifest=NULL,
                     error_summary=NULL,retry_count=excluded.retry_count,
                     metadata_json=excluded.metadata_json,result_json=NULL""",
                (
                    task_id,
                    key,
                    task_type,
                    knowledge_base,
                    library,
                    version,
                    _now().isoformat(),
                    input_manifest,
                    retries,
                    canonical_json_dumps(inputs or {}),
                ),
            )
            attempt_number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM attempts WHERE task_id=?",
                    (task_id,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO attempts VALUES(?,?,?,'running',?,NULL,NULL,NULL,NULL)",
                (str(uuid.uuid4()), task_id, attempt_number, _now().isoformat()),
            )
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            return self._execution(dict(row), required=True, reused=existing is not None)

    def finish(
        self,
        task_id: str,
        status: str,
        *,
        output_manifest: str | None = None,
        error: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        if status not in STATUSES - {"pending", "running"}:
            raise ValueError("invalid terminal task status")
        with self.connect() as connection:
            result_json = canonical_json_dumps(result) if result is not None else None
            changed = connection.execute(
                """UPDATE tasks SET status=?,ended_at=?,output_manifest=?,
                   error_summary=?,result_json=? WHERE task_id=?""",
                (status, _now().isoformat(), output_manifest, error, result_json, task_id),
            ).rowcount
            if not changed:
                raise ValueError(f"unknown task: {task_id}")
            connection.execute(
                """UPDATE attempts SET status=?,ended_at=?,output_manifest=?,
                   error_summary=?,result_json=?
                   WHERE attempt_id=(
                     SELECT attempt_id FROM attempts
                     WHERE task_id=? AND status='running'
                     ORDER BY attempt_number DESC LIMIT 1
                   )""",
                (
                    status,
                    _now().isoformat(),
                    output_manifest,
                    error,
                    result_json,
                    task_id,
                ),
            )

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

    def renew(
        self, lock_keys: Sequence[str], task_id: str, *, ttl_seconds: int = 3600
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        keys = sorted(set(lock_keys))
        if not keys:
            return
        now = _now()
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            renewed = 0
            for lock_key in keys:
                renewed += connection.execute(
                    """UPDATE locks SET expires_at=?
                       WHERE lock_key=? AND task_id=? AND expires_at>?""",
                    (expires_at, lock_key, task_id, now.isoformat()),
                ).rowcount
            if renewed != len(keys):
                raise TaskConflictError(f"task lock lease was lost: {task_id}")

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT task_id,idempotency_key,task_type,status,knowledge_base,
                   library,version,started_at,ended_at,input_manifest,output_manifest,
                   error_summary,retry_count,metadata_json
                   FROM tasks ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def result(self, task_id: str) -> dict[str, Any] | None:
        row = self.get(task_id)
        if row is None or not row.get("result_json"):
            return None
        value = json.loads(str(row["result_json"]))
        return dict(value) if isinstance(value, Mapping) else None

    def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM attempts WHERE task_id=? ORDER BY attempt_number",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _execution(
        row: dict[str, Any], *, required: bool, reused: bool
    ) -> dict[str, Any]:
        row["execution_required"] = required
        row["reused"] = reused
        return row

    @staticmethod
    def _is_stale(started_at: str, stale_after_seconds: int) -> bool:
        try:
            started = datetime.fromisoformat(started_at)
        except ValueError:
            return True
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return _now() - started > timedelta(seconds=stale_after_seconds)

    @staticmethod
    def _has_live_lock(connection: sqlite3.Connection, task_id: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM locks WHERE task_id=? AND expires_at>? LIMIT 1",
            (task_id, _now().isoformat()),
        ).fetchone()
        return row is not None


class TaskExecutor:
    """Execute one operation under a durable task record and expiring locks."""

    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def execute(
        self,
        task_type: str,
        operation: Callable[[], dict[str, Any]],
        *,
        knowledge_base: str,
        library: str | None = None,
        version: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        input_manifest: str | None = None,
        lock_keys: Sequence[str] = (),
        output_manifest: Callable[[Mapping[str, Any]], str | None] | None = None,
        ttl_seconds: int = 21600,
        heartbeat_interval_seconds: float | None = None,
    ) -> dict[str, Any]:
        task = self.store.begin(
            task_type,
            knowledge_base=knowledge_base,
            library=library,
            version=version,
            inputs=inputs,
            input_manifest=input_manifest,
            reuse_completed=False,
            stale_after_seconds=ttl_seconds,
        )
        task_id = str(task["task_id"])
        if not task["execution_required"]:
            raise TaskConflictError(f"equivalent task is already running: {task_id}")
        acquired: list[str] = []
        heartbeat_stop = Event()
        heartbeat_errors: list[Exception] = []
        heartbeat: Thread | None = None
        try:
            for lock_key in sorted(set(lock_keys)):
                self.store.acquire(lock_key, task_id, ttl_seconds=ttl_seconds)
                acquired.append(lock_key)
            if acquired:
                interval = (
                    heartbeat_interval_seconds
                    if heartbeat_interval_seconds is not None
                    else max(0.1, min(60.0, ttl_seconds / 3))
                )
                if interval <= 0 or interval >= ttl_seconds:
                    raise ValueError(
                        "heartbeat interval must be positive and shorter than lock TTL"
                    )
                heartbeat = Thread(
                    target=self._heartbeat,
                    args=(
                        heartbeat_stop,
                        heartbeat_errors,
                        tuple(acquired),
                        task_id,
                        ttl_seconds,
                        interval,
                    ),
                    name=f"knowledgehub-task-{task_id[:8]}",
                    daemon=True,
                )
                heartbeat.start()
            result = operation()
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=max(1.0, heartbeat_interval_seconds or 1.0) + 1.0)
            if heartbeat_errors:
                raise TaskConflictError(str(heartbeat_errors[0]))
            terminal = self._terminal_status(result)
            manifest = output_manifest(result) if output_manifest else None
            self.store.finish(
                task_id,
                terminal,
                output_manifest=manifest,
                result=result,
            )
            current = self.store.get(task_id) or task
            return {
                **result,
                "task": self._task_summary(current, acquired, reused=bool(task["reused"])),
            }
        except Exception as error:
            failure_task = self.store.get(task_id)
            if failure_task and failure_task["status"] == "running":
                self.store.finish(task_id, "failed", error=str(error)[:2000])
            raise
        finally:
            heartbeat_stop.set()
            if heartbeat is not None and heartbeat.is_alive():
                heartbeat.join(timeout=2.0)
            for lock_key in reversed(acquired):
                self.store.release(lock_key, task_id=task_id)

    @staticmethod
    def _terminal_status(result: Mapping[str, Any]) -> str:
        status = str(result.get("status") or "success")
        if status == "partial":
            return "partial"
        if status in {"success", "planned", "skipped", "completed", "available"}:
            return "completed"
        return "failed"

    @staticmethod
    def _task_summary(
        row: Mapping[str, Any], lock_keys: Sequence[str], *, reused: bool
    ) -> dict[str, Any]:
        return {
            "task_id": row.get("task_id"),
            "status": row.get("status"),
            "retry_count": row.get("retry_count"),
            "reused": reused,
            "lock_keys": list(lock_keys),
        }

    def _heartbeat(
        self,
        stop: Event,
        errors: list[Exception],
        lock_keys: Sequence[str],
        task_id: str,
        ttl_seconds: int,
        interval_seconds: float,
    ) -> None:
        while not stop.wait(interval_seconds):
            try:
                self.store.renew(lock_keys, task_id, ttl_seconds=ttl_seconds)
            except Exception as error:
                errors.append(error)
                stop.set()
                return
