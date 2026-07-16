"""Explicit synchronization plans and confirmation-gated runtime cleanup."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Mapping, Sequence

from knowledgehub.code_rag.registry import CodeSourceRegistry
from knowledgehub.core.atomic import (
    atomic_write_json,
    ensure_path_within,
    safe_rmtree,
    safe_unlink,
)
from knowledgehub.core.hashing import sha256_json


class SyncPlanner:
    TRIGGERS: ClassVar[set[str]] = {
        "manual",
        "periodic",
        "release",
        "config_change",
        "on_demand",
    }

    def __init__(self, registry: CodeSourceRegistry) -> None:
        self.registry = registry

    def plan(
        self,
        *,
        trigger: str,
        libraries: Sequence[str] = (),
        version: str | None = None,
        interval_hours: int | None = None,
    ) -> dict[str, Any]:
        if trigger not in self.TRIGGERS:
            raise ValueError(f"unsupported sync trigger: {trigger}")
        if trigger == "periodic" and (interval_hours is None or interval_hours < 1):
            raise ValueError("periodic plans require interval_hours >= 1")
        selected = (
            [self.registry.get(name) for name in libraries]
            if libraries
            else self.registry.list(enabled_only=True)
        )
        if not selected:
            raise ValueError("sync plan selected no libraries")
        actions = []
        for library in selected:
            if trigger == "release":
                action = "check_release_and_notify"
            elif trigger == "config_change":
                action = "validate_then_build_candidate"
            elif trigger == "on_demand":
                action = "request_version_import_permission"
            else:
                action = "sync_configured_versions"
            actions.append(
                {
                    "library": library.name,
                    "repository": library.repository,
                    "action": action,
                    "version": version,
                    "allow_download": False,
                    "switch_environment": False,
                    "switch_index_alias": False,
                }
            )
        identity = {
            "trigger": trigger,
            "libraries": [item.name for item in selected],
            "version": version,
            "interval_hours": interval_hours,
            "registry": str(self.registry.path),
        }
        return {
            "schema_name": "sync_plan",
            "schema_version": "2.0",
            "plan_id": f"sync-plan:{sha256_json(identity)}",
            "trigger": trigger,
            "interval_hours": interval_hours,
            "actions": actions,
            "scheduler_started": False,
            "automatic_download": False,
            "automatic_environment_switch": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


class CleanupService:
    """Plan bounded cleanup without touching Literature or active source checkouts."""

    def __init__(
        self,
        *,
        code_root: Path,
        rag_dirs: Mapping[str, Path],
        index_root: Path,
    ) -> None:
        self.code_root = code_root.resolve(strict=False)
        self.rag_dirs = {name: path.resolve(strict=False) for name, path in rag_dirs.items()}
        self.index_root = index_root.resolve(strict=False)

    def plan_cache(self, *, min_age_hours: int = 24) -> dict[str, Any]:
        if min_age_hours < 1:
            raise ValueError("cache cleanup min_age_hours must be at least 1")
        staging = self.code_root / ".staging"
        cutoff = datetime.now(timezone.utc).timestamp() - min_age_hours * 3600
        candidates = [
            self._candidate(path, "directory", self.code_root)
            for path in sorted(staging.iterdir())
            if path.stat().st_mtime <= cutoff
        ] if staging.is_dir() else []
        plan = self._plan("clean_cache", candidates)
        plan["min_age_hours"] = min_age_hours
        return plan

    def plan_source(self, library: str, version: str) -> dict[str, Any]:
        root = ensure_path_within(
            self.code_root / "sources" / "repositories" / library / version,
            self.code_root,
        )
        marker_path = root / "current.json"
        if not marker_path.is_file():
            raise ValueError(f"source marker not found: {library} {version}")
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        current = Path(str(marker.get("source_path") or "")).resolve(strict=False)
        candidates = [
            self._candidate(path, "directory", self.code_root)
            for path in sorted(root.iterdir())
            if path.is_dir() and path.resolve(strict=False) != current
        ]
        plan = self._plan("clean_source", candidates)
        plan["protected_current_source"] = str(current)
        return plan

    def plan_snapshots(self, knowledge_base: str, *, keep: int) -> dict[str, Any]:
        if knowledge_base not in {"code", "writing"}:
            raise ValueError("snapshot cleanup supports only code or writing")
        if keep < 1:
            raise ValueError("snapshot cleanup keep must be at least 1")
        root = self.index_root / knowledge_base
        manifests = sorted(
            (root / "snapshots").glob("*.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        current_path = root / "current.json"
        current = (
            json.loads(current_path.read_text(encoding="utf-8"))
            if current_path.is_file()
            else {}
        )
        protected = str(current.get("snapshot_id") or "")
        removable = [
            path
            for path in manifests[keep:]
            if path.stem != protected
        ]
        candidates: list[dict[str, Any]] = []
        for path in removable:
            value = json.loads(path.read_text(encoding="utf-8"))
            candidates.append(
                self._candidate(path, "qdrant_snapshot", self.index_root)
                | {
                    "collection": value.get("collection"),
                    "qdrant_snapshot": value.get("qdrant_snapshot"),
                }
            )
        plan = self._plan("clean_snapshots", candidates)
        plan.update({"knowledge_base": knowledge_base, "keep": keep})
        return plan

    def plan_unreferenced(self, knowledge_bases: Sequence[str]) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        for knowledge_base in knowledge_bases:
            if knowledge_base not in self.rag_dirs:
                raise ValueError(f"unsupported prune knowledge base: {knowledge_base}")
            root = self.rag_dirs[knowledge_base]
            state_path = root / "state" / "index.sqlite3"
            document_ids: list[str] = []
            chunk_paths = sorted((root / "chunks").glob("*.jsonl"))
            if chunk_paths and not state_path.is_file():
                raise RuntimeError(
                    f"cannot prove artifacts are unreferenced without index state: {knowledge_base}"
                )
            if state_path.is_file():
                connection = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
                try:
                    document_ids = [
                        str(row[0])
                        for row in connection.execute("SELECT document_id FROM documents")
                    ]
                finally:
                    connection.close()
            expected = {f"{sha256_json(value)[:32]}.jsonl" for value in document_ids}
            for path in chunk_paths:
                if path.name not in expected:
                    candidates.append(self._candidate(path, "file", root))
        return self._plan("prune_unreferenced", candidates)

    def execute(
        self,
        plan: Mapping[str, Any],
        *,
        confirmed: bool = False,
        qdrant_client: Any | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("cleanup execution requires explicit confirmation")
        if plan.get("schema_name") != "cleanup_plan":
            raise ValueError("invalid cleanup plan")
        removed: list[str] = []
        allowed_roots = {
            self.code_root,
            self.index_root,
            *self.rag_dirs.values(),
        }
        for candidate in plan.get("candidates") or []:
            path = Path(str(candidate["path"]))
            root = Path(str(candidate["allowed_root"])).resolve(strict=False)
            if root not in allowed_roots:
                raise ValueError(f"cleanup candidate has an unapproved root: {root}")
            kind = str(candidate["kind"])
            if kind == "directory":
                safe_rmtree(path, root=root)
            elif kind == "file":
                safe_unlink(path, root=root)
            elif kind == "qdrant_snapshot":
                if qdrant_client is None:
                    raise RuntimeError("Qdrant client is required for snapshot cleanup")
                result = qdrant_client.delete_snapshot(
                    str(candidate["collection"]),
                    str(candidate["qdrant_snapshot"]),
                    wait=True,
                )
                if not result:
                    raise RuntimeError("Qdrant snapshot deletion failed")
                safe_unlink(path, root=root)
            else:
                raise ValueError(f"unsupported cleanup candidate kind: {kind}")
            removed.append(str(path))
        result = dict(plan) | {
            "status": "completed",
            "dry_run": False,
            "removed": removed,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
        audit = self.code_root / "manifests" / "maintenance" / f"{plan['plan_id']}.json"
        atomic_write_json(audit, result)
        result["audit_manifest"] = str(audit)
        return result

    def _plan(self, operation: str, candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        values = [dict(value) for value in candidates]
        identity = {"operation": operation, "candidates": values}
        return {
            "schema_name": "cleanup_plan",
            "schema_version": "2.0",
            "plan_id": sha256_json(identity)[:24],
            "operation": operation,
            "dry_run": True,
            "candidate_count": len(values),
            "bytes": sum(int(value["bytes"]) for value in values),
            "candidates": values,
            "literature_touched": False,
            "active_sources_touched": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _candidate(path: Path, kind: str, root: Path) -> dict[str, Any]:
        size = (
            sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
            if path.is_dir()
            else path.stat().st_size
        )
        return {
            "path": str(path.resolve(strict=False)),
            "kind": kind,
            "bytes": size,
            "allowed_root": str(root.resolve(strict=False)),
        }
