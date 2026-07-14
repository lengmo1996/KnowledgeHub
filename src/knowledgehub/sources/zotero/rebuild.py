"""Safe, staged recovery of the complete Zotero runtime state."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from knowledgehub.core.atomic import atomic_write_json, atomic_write_text, safe_remove
from knowledgehub.core.hashing import canonical_json_dumps
from knowledgehub.core.locking import FileLock

from .config import ZoteroConfig
from .models import RuntimeDependencies, SyncMode, ZoteroError
from .state import ZoteroStateStore
from .sync import _PublicationSession, recover_publications, sync_once
from .validation import validate_source

LOGGER = logging.getLogger(__name__)


def rebuild_source(
    config: ZoteroConfig,
    *,
    confirmed: bool = False,
    dependencies: RuntimeDependencies | None = None,
) -> dict[str, Any]:
    """Build from version zero in a candidate root and promote only after validation."""

    targets = [
        config.data_dir / "state" / "zotero.sqlite3",
        config.data_dir / "extracted",
        config.data_dir / "manifests",
    ]
    result: dict[str, Any] = {
        "dry_run": not confirmed,
        "data_dir": str(config.data_dir.resolve(strict=False)),
        "webdav_dir": str(config.webdav_dir.resolve(strict=False)),
        "replace_targets": [str(value.resolve(strict=False)) for value in targets],
        "preserved": [str(config.data_dir / "logs"), str(config.data_dir / "runs")],
    }
    if not confirmed:
        return result

    config.prepare_runtime()
    rebuild_id = f"rebuild-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex}"
    candidate = config.data_dir / ".rebuild" / rebuild_id
    if candidate.exists():
        raise ZoteroError(
            "rebuild_error", f"Candidate rebuild directory already exists: {candidate}"
        )
    candidate_config = replace(config, data_dir=candidate)
    lock = FileLock(config.data_dir / "state" / "zotero.lock", sync_id=rebuild_id)
    with lock:
        try:
            if any((config.data_dir / "runs").glob("*/publish-intent.json")):
                existing_store = ZoteroStateStore(config.data_dir)
                existing_store.initialize()
                recover_publications(config.data_dir, existing_store)
            summary = sync_once(candidate_config, mode=SyncMode.FULL, dependencies=dependencies)
            candidate_report = validate_source(candidate_config)
            if not candidate_report.valid:
                raise ZoteroError("rebuild_validation_failed", "Candidate state failed validation")
            _rewrite_candidate_prefix(candidate, config.data_dir)
            _checkpoint(candidate / "state" / "zotero.sqlite3")
            _prepare_existing_database(
                config.data_dir / "state" / "zotero.sqlite3", config.data_dir
            )

            run_dir = config.data_dir / "runs" / summary.sync_id
            run_dir.mkdir(parents=True, exist_ok=True)
            candidate_run = candidate / "runs" / summary.sync_id / "summary.json"
            if candidate_run.is_file():
                shutil.copy2(candidate_run, run_dir / "summary.json")

            entries = [
                _entry(candidate / "extracted", config.data_dir / "extracted", summary.sync_id),
                _entry(candidate / "manifests", config.data_dir / "manifests", summary.sync_id),
                _entry(
                    candidate / "state" / "zotero.sqlite3",
                    config.data_dir / "state" / "zotero.sqlite3",
                    summary.sync_id,
                ),
            ]
            intent = run_dir / "publish-intent.json"
            atomic_write_json(
                intent,
                {
                    "schema_version": 1,
                    "sync_id": summary.sync_id,
                    "target_version": summary.committed_version,
                    "status": "prepared",
                    "entries": entries,
                },
                mode=0o600,
            )
            publication = _PublicationSession(
                config.data_dir,
                summary.sync_id,
                entries,
                intent,
                summary.committed_version,
            )
            try:
                publication.publish()
                report = validate_source(config, ignore_publish_intent=intent)
                if not report.valid:
                    raise ZoteroError(
                        "rebuild_validation_failed", "Promoted state failed validation"
                    )
            except BaseException:
                publication.rollback()
                raise
            try:
                publication.commit()
            except BaseException:
                LOGGER.exception(
                    "rebuild publication cleanup deferred to startup recovery",
                    extra={"sync_id": summary.sync_id},
                )
            result.update(
                {
                    "dry_run": False,
                    "sync_id": summary.sync_id,
                    "library_version": summary.committed_version,
                    "validation": report.to_dict(),
                }
            )
            return result
        finally:
            if candidate.exists():
                safe_remove(candidate, root=config.data_dir)


def _entry(staged: Path, target: Path, sync_id: str) -> dict[str, Any]:
    return {
        "staged": str(staged),
        "target": str(target),
        "backup": str(target.parent / f".{target.name}.backup-{sync_id}"),
        "had_target": target.exists() or target.is_symlink(),
    }


def _checkpoint(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _prepare_existing_database(path: Path, data_dir: Path) -> None:
    sidecars = [Path(f"{path}{suffix}") for suffix in ("-wal", "-shm")]
    if path.exists():
        try:
            _checkpoint(path)
        except sqlite3.DatabaseError as exc:
            # A corrupt database is a primary reason to invoke rebuild.  Its
            # main file can still be backed up, but an uncheckpointed WAL must
            # never be discarded or left beside the replacement database.
            if any(sidecar.exists() for sidecar in sidecars):
                raise ZoteroError(
                    "rebuild_error",
                    "Cannot safely rebuild while the existing database has an uncheckpointed WAL",
                ) from exc
    elif any(sidecar.exists() for sidecar in sidecars):
        raise ZoteroError(
            "rebuild_error", "SQLite sidecars exist without the state database; refusing rebuild"
        )
    for sidecar in sidecars:
        if sidecar.exists():
            safe_remove(sidecar, root=data_dir)


def _rewrite_candidate_prefix(candidate: Path, target: Path) -> None:
    old = str(candidate.resolve(strict=True))
    new = str(target.resolve(strict=True))
    database = candidate / "state" / "zotero.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT document_id, manifest_json FROM documents").fetchall()
        for row in rows:
            value = _replace_prefix(json.loads(row["manifest_json"]), old, new)
            connection.execute(
                "UPDATE documents SET manifest_json = ? WHERE document_id = ?",
                (canonical_json_dumps(value), row["document_id"]),
            )
        connection.execute(
            "UPDATE attachments SET pdf_path = replace(pdf_path, ?, ?) WHERE pdf_path IS NOT NULL",
            (old, new),
        )
        connection.commit()

    manifests = candidate / "manifests"
    for path in sorted(manifests.rglob("*.json")):
        value = _replace_prefix(json.loads(path.read_text(encoding="utf-8")), old, new)
        atomic_write_json(path, value)
    for path in sorted(manifests.rglob("*.jsonl")):
        values = [
            _replace_prefix(json.loads(line), old, new)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        payload = "".join(f"{canonical_json_dumps(value)}\n" for value in values)
        atomic_write_text(path, payload)


def _replace_prefix(value: Any, old: str, new: str) -> Any:
    if isinstance(value, str):
        return new + value[len(old) :] if value.startswith(old) else value
    if isinstance(value, list):
        return [_replace_prefix(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: _replace_prefix(item, old, new) for key, item in value.items()}
    return value
