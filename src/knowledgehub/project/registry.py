"""Filesystem registry for isolated V3 workspace records."""

from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Mapping

from knowledgehub.core.atomic import atomic_write_json, ensure_path_within, safe_rmtree
from knowledgehub.core.hashing import sha256_json
from knowledgehub.core.locking import FileLock
from knowledgehub.project.models import (
    ClaimRecord,
    DecisionRecord,
    ExperimentRecord,
    FailureRecord,
    Workspace,
)

RECORD_DIRS = {
    "experiment": "experiments",
    "failure": "failures",
    "decision": "decisions",
    "claim": "claims",
}
EXPERIMENT_TRANSITIONS = {
    "planned": {"running", "cancelled", "invalid"},
    "running": {"completed", "failed", "cancelled", "invalid"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


class ProjectRegistry:
    """One-directory-per-workspace registry with immutable history records."""

    def __init__(
        self,
        root: Path | str = Path("state/fixtures"),
        *,
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.lock_timeout_seconds = lock_timeout_seconds

    def lock_path(self, workspace_id: str) -> Path:
        return self.root / ".locks" / f"{workspace_id}.lock"

    def _lock(self, workspace_id: str, operation: str) -> FileLock:
        return FileLock(
            self.lock_path(workspace_id),
            sync_id=f"project:{workspace_id}:{operation}",
            timeout_seconds=self.lock_timeout_seconds,
        )

    def workspace_dir(self, workspace_id: str) -> Path:
        # ID validation occurs through Workspace or get; containment remains a second guard.
        return ensure_path_within(self.root / workspace_id, self.root)

    def create(self, workspace: Workspace) -> dict[str, Any]:
        if workspace.workspace_type != "fixture":
            raise ValueError("fixture registry accepts only fixture workspaces")
        with self._lock(workspace.workspace_id, "create"):
            destination = self.workspace_dir(workspace.workspace_id) / "workspace.json"
            payload = workspace.to_dict()
            if destination.is_file():
                current = _read_json(destination)
                if sha256_json(current) == sha256_json(payload):
                    return {
                        "status": "unchanged",
                        "workspace": current,
                        "path": str(destination),
                    }
                raise FileExistsError(
                    f"workspace already exists with different content: {workspace.workspace_id}"
                )
            atomic_write_json(destination, payload, mode=0o600)
            return {"status": "created", "workspace": payload, "path": str(destination)}

    def get(self, workspace_id: str) -> dict[str, Any]:
        path = self.workspace_dir(workspace_id) / "workspace.json"
        if not path.is_file():
            raise KeyError(f"unknown workspace: {workspace_id}")
        value = _read_json(path)
        Workspace.from_dict(value)
        return value

    def list_workspaces(self, *, include_fixtures: bool = False) -> list[dict[str, Any]]:
        if not include_fixtures or not self.root.is_dir():
            return []
        workspaces: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*/workspace.json")):
            value = _read_json(path)
            Workspace.from_dict(value)
            workspaces.append(value)
        return workspaces

    def archive(self, workspace_id: str) -> dict[str, Any]:
        with self._lock(workspace_id, "archive"):
            current = self.get(workspace_id)
            if current["status"] == "archived":
                return {"status": "unchanged", "workspace": current}
            updated = current | {"status": "archived", "updated_at": utc_now()}
            Workspace.from_dict(updated)
            atomic_write_json(
                self.workspace_dir(workspace_id) / "workspace.json", updated, mode=0o600
            )
            return {"status": "archived", "workspace": updated}

    def validate(self, workspace_id: str, *, repository_root: Path | str = Path(".")) -> dict[str, Any]:
        workspace = self.get(workspace_id)
        root = Path(repository_root).resolve(strict=True)
        errors: list[str] = []
        warnings: list[str] = []
        for repository in workspace["repositories"]:
            path = (root / str(repository["path"])).resolve(strict=False)
            try:
                path.relative_to(root)
            except ValueError:
                errors.append(f"repository path escapes root: {repository['path']}")
                continue
            if not path.is_dir():
                errors.append(f"repository path is missing: {repository['path']}")
        environment_ids = set(workspace["environments"].values())
        for environment_id in sorted(environment_ids):
            profile = self.workspace_dir(workspace_id) / "environments" / f"{environment_id}.json"
            if not profile.is_file():
                errors.append(f"environment profile is missing: {environment_id}")
        for base, scope in workspace["knowledge"].items():
            namespace = str(scope.get("namespace") or "")
            if not namespace.startswith("fixture-"):
                errors.append(f"{base} references a non-fixture namespace")
            if scope.get("write_target"):
                errors.append(f"{base} must not define a write target")
        if not self.list_workspaces(include_fixtures=True):
            warnings.append("registry contains no fixture workspaces")
        return {
            "workspace_id": workspace_id,
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "checked_at": utc_now(),
        }

    def export(self, workspace_id: str) -> dict[str, Any]:
        return {
            "workspace": self.get(workspace_id),
            "environments": self.list_environments(workspace_id),
            "experiments": self.list_records(workspace_id, "experiment"),
            "failures": self.list_records(workspace_id, "failure"),
            "decisions": self.list_records(workspace_id, "decision"),
            "claims": self.list_records(workspace_id, "claim"),
            "exported_at": utc_now(),
        }

    def put_record(
        self,
        workspace_id: str,
        record_type: str,
        record_id: str,
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock(workspace_id, f"put-{record_type}"):
            self.get(workspace_id)
            if record_type not in RECORD_DIRS:
                raise ValueError(f"unsupported record type: {record_type}")
            data = dict(value)
            if data.get("workspace_id") != workspace_id:
                raise ValueError("record workspace_id does not match target")
            self._validate_record(record_type, data)
            path = (
                self.workspace_dir(workspace_id)
                / RECORD_DIRS[record_type]
                / f"{record_id}.json"
            )
            if path.is_file():
                current = _read_json(path)
                if sha256_json(current) == sha256_json(data):
                    return {"status": "unchanged", "record": current, "path": str(path)}
                raise FileExistsError(f"immutable {record_type} already exists: {record_id}")
            if record_type == "experiment":
                for existing in self.list_records(workspace_id, "experiment"):
                    if existing.get("run_id") == data.get("run_id"):
                        raise FileExistsError(
                            f"experiment run_id already exists: {data.get('run_id')}"
                        )
            atomic_write_json(path, data, mode=0o600)
            return {"status": "created", "record": data, "path": str(path)}

    def list_records(self, workspace_id: str, record_type: str) -> list[dict[str, Any]]:
        self.get(workspace_id)
        if record_type not in RECORD_DIRS:
            raise ValueError(f"unsupported record type: {record_type}")
        directory = self.workspace_dir(workspace_id) / RECORD_DIRS[record_type]
        return [_read_json(path) for path in sorted(directory.glob("*.json"))]

    def get_record(self, workspace_id: str, record_type: str, record_id: str) -> dict[str, Any]:
        if record_type not in RECORD_DIRS:
            raise ValueError(f"unsupported record type: {record_type}")
        path = self.workspace_dir(workspace_id) / RECORD_DIRS[record_type] / f"{record_id}.json"
        if not path.is_file():
            raise KeyError(f"unknown {record_type}: {record_id}")
        return _read_json(path)

    def transition_experiment(
        self,
        workspace_id: str,
        experiment_id: str,
        status: str,
        updates: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock(workspace_id, "transition-experiment"):
            current = self.get_record(workspace_id, "experiment", experiment_id)
            current_status = str(current["status"])
            if status not in EXPERIMENT_TRANSITIONS.get(current_status, set()):
                raise ValueError(f"invalid experiment transition: {current_status} -> {status}")
            updated = current | dict(updates) | {"status": status}
            ExperimentRecord.from_dict(updated)
            events = self.workspace_dir(workspace_id) / "experiment_events" / experiment_id
            sequence = len(list(events.glob("*.json"))) + 1
            event = {
                "experiment_id": experiment_id,
                "from_status": current_status,
                "to_status": status,
                "transitioned_at": utc_now(),
                "previous_hash": sha256_json(current),
                "current_hash": sha256_json(updated),
                "previous_record": current,
            }
            event_path = events / f"{sequence:03d}-{current_status}-to-{status}.json"
            atomic_write_json(event_path, event, mode=0o600)
            record_path = (
                self.workspace_dir(workspace_id) / "experiments" / f"{experiment_id}.json"
            )
            atomic_write_json(record_path, updated, mode=0o600)
            return {
                "status": "transitioned",
                "record": updated,
                "event": str(event_path),
                "path": str(record_path),
            }

    def capture_fixture_environment(self, workspace_id: str, environment_id: str) -> dict[str, Any]:
        with self._lock(workspace_id, "capture-environment"):
            self.get(workspace_id)
            package_names = ("knowledgehub", "numpy", "torch")
            packages: dict[str, str | None] = {}
            for name in package_names:
                try:
                    packages[name] = version(name)
                except PackageNotFoundError:
                    packages[name] = None
            stable = {
                "schema_version": "3.0",
                "environment_id": environment_id,
                "environment_type": "fixture",
                "data_scope": "test",
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "packages": packages,
                "device": "cpu",
                "project_root": "<fixture_repository>",
            }
            profile = stable | {"captured_at": utc_now(), "content_hash": sha256_json(stable)}
            path = (
                self.workspace_dir(workspace_id)
                / "environments"
                / f"{environment_id}.json"
            )
            if path.is_file():
                current = _read_json(path)
                if current.get("content_hash") == profile["content_hash"]:
                    return {"status": "unchanged", "profile": current, "path": str(path)}
            atomic_write_json(path, profile, mode=0o600)
            return {"status": "captured", "profile": profile, "path": str(path)}

    def list_environments(self, workspace_id: str) -> list[dict[str, Any]]:
        directory = self.workspace_dir(workspace_id) / "environments"
        return [_read_json(path) for path in sorted(directory.glob("*.json"))]

    def cleanup(self, workspace_id: str, *, execute: bool = False) -> dict[str, Any]:
        with self._lock(workspace_id, "cleanup"):
            workspace = self.get(workspace_id)
            if (
                workspace.get("workspace_type") != "fixture"
                or workspace.get("data_scope") != "test"
            ):
                raise PermissionError("cleanup is restricted to isolated fixture workspaces")
            target = self.workspace_dir(workspace_id)
            affected = sorted(
                str(path.relative_to(target)) for path in target.rglob("*") if path.is_file()
            )
            plan = {
                "workspace_id": workspace_id,
                "dry_run": not execute,
                "target": str(target),
                "affected_files": affected,
                "shared_knowledge_bases_deleted": False,
                "repositories_deleted": False,
                "executed_at": utc_now(),
            }
            manifest = self.root / "cleanup_manifests" / f"{workspace_id}.json"
            atomic_write_json(manifest, plan, mode=0o600)
            if execute:
                safe_rmtree(target, root=self.root)
            return plan | {"cleanup_manifest": str(manifest)}

    @staticmethod
    def _validate_record(record_type: str, value: Mapping[str, Any]) -> None:
        if record_type == "experiment":
            ExperimentRecord.from_dict(value)
        elif record_type == "failure":
            FailureRecord(**value)
        elif record_type == "decision":
            DecisionRecord(**value)
        else:
            ClaimRecord(**value)
