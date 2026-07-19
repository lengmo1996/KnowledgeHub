"""Scheduler-safe retention planning, quarantine, and verified purge."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from knowledgehub.core.atomic import atomic_write_json, fsync_directory, safe_rmtree
from knowledgehub.core.hashing import sha256_file, sha256_json
from knowledgehub.writing_rag.review import validate_run_governance

RETENTION_PLAN_SCHEMA_VERSION = "writing-material-retention-plan-v1"
RETENTION_INTENT_SCHEMA_VERSION = "writing-material-retention-intent-v1"
RETENTION_RECEIPT_SCHEMA_VERSION = "writing-material-retention-receipt-v1"
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class RetentionDispositionError(ValueError):
    """A retention plan or destructive transition is unsafe or stale."""


class WritingMaterialRetentionService:
    """Move unreferenced expired runs to quarantine, then purge after a grace period."""

    def __init__(self, data_root: Path, *, quarantine_days: int = 30) -> None:
        if quarantine_days < 1:
            raise ValueError("retention quarantine must be at least one day")
        self.data_root = data_root.expanduser().resolve(strict=False)
        self.runs_root = self.data_root / "runs"
        self.retention_root = self.data_root / "retention"
        self.quarantine_root = self.retention_root / "quarantine"
        self.intent_root = self.retention_root / "intents"
        self.receipt_root = self.retention_root / "receipts"
        self.quarantine_days = quarantine_days

    def plan(self, run_id: str | None = None, *, now: datetime | None = None) -> dict[str, Any]:
        current = self._utc_now(now)
        run_dirs = [self._run_dir(run_id)] if run_id is not None else self._run_dirs()
        entries = [self._plan_run(path, now=current) for path in run_dirs]
        expired = [entry for entry in entries if entry["retention_status"] == "expired"]
        ready = [entry for entry in expired if entry["status"] == "ready"]
        blocked = [entry for entry in expired if entry["status"] == "blocked"]
        unmanaged = [entry for entry in entries if entry["status"] == "unmanaged"]
        if blocked:
            status = "blocked"
        elif ready:
            status = "ready"
        elif entries and all(entry["status"] == "not_due" for entry in entries):
            status = "not_due"
        else:
            status = "no_action"
        payload: dict[str, Any] = {
            "schema_version": RETENTION_PLAN_SCHEMA_VERSION,
            "status": status,
            "created_at": current.isoformat(),
            "run_id": run_id,
            "quarantine_days": self.quarantine_days,
            "counts": {
                "scanned": len(entries),
                "expired": len(expired),
                "ready": len(ready),
                "blocked": len(blocked),
                "unmanaged": len(unmanaged),
            },
            "entries": entries,
            "dry_run": True,
            "writes_performed": False,
            "llm_called": False,
            "index_modified": False,
        }
        return {**payload, "artifact_fingerprint": sha256_json(payload)}

    def quarantine(
        self,
        run_id: str,
        *,
        confirmed: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise RetentionDispositionError("retention quarantine requires explicit confirmation")
        current = self._utc_now(now)
        source = self._run_path(run_id)
        destination = self._quarantine_path(run_id)
        intent_path = self.intent_root / f"{run_id}.json"
        receipt_path = self.receipt_root / f"{run_id}.json"
        if not source.exists() and destination.is_dir():
            intent = self._load_artifact(intent_path, RETENTION_INTENT_SCHEMA_VERSION)
            inventory = self._inventory(destination)
            if intent.get("inventory_fingerprint") != sha256_json(inventory):
                raise RetentionDispositionError("quarantined run differs from retention intent")
            return self._write_quarantine_receipt(
                intent,
                receipt_path=receipt_path,
                recovered=True,
            )
        plan = self.plan(run_id, now=current)
        if plan["status"] != "ready" or plan["counts"]["ready"] != 1:
            raise RetentionDispositionError("retention run is not ready for quarantine")
        entry = plan["entries"][0]
        if destination.exists() or destination.is_symlink():
            raise RetentionDispositionError("retention quarantine destination already exists")
        inventory = entry["inventory"]
        intent_payload: dict[str, Any] = {
            "schema_version": RETENTION_INTENT_SCHEMA_VERSION,
            "run_id": run_id,
            "source_path": str(source),
            "quarantine_path": str(destination),
            "expires_at": entry["expires_at"],
            "planned_at": plan["created_at"],
            "plan_fingerprint": plan["artifact_fingerprint"],
            "inventory": inventory,
            "inventory_fingerprint": sha256_json(inventory),
            "quarantine_days": self.quarantine_days,
        }
        intent = {
            **intent_payload,
            "artifact_fingerprint": sha256_json(intent_payload),
        }
        self._prepare_private_dirs()
        if intent_path.exists():
            existing = self._load_artifact(intent_path, RETENTION_INTENT_SCHEMA_VERSION)
            if existing != intent:
                raise RetentionDispositionError("retention intent already exists with other content")
        else:
            atomic_write_json(intent_path, intent, mode=0o600)
        os.replace(source, destination)
        fsync_directory(self.runs_root)
        fsync_directory(self.quarantine_root)
        return self._write_quarantine_receipt(
            intent,
            receipt_path=receipt_path,
            recovered=False,
        )

    def purge(
        self,
        run_id: str,
        *,
        confirmed: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise RetentionDispositionError("retention purge requires explicit confirmation")
        current = self._utc_now(now)
        receipt_path = self.receipt_root / f"{self._validated_run_id(run_id)}.json"
        receipt = self._load_artifact(receipt_path, RETENTION_RECEIPT_SCHEMA_VERSION)
        if receipt.get("status") == "purged":
            return receipt
        if receipt.get("status") != "quarantined":
            raise RetentionDispositionError("retention receipt is not purgeable")
        purge_after = self._parse_time(receipt.get("purge_after"), "purge_after")
        if current < purge_after:
            raise RetentionDispositionError("retention quarantine grace period is still active")
        quarantine = self._quarantine_path(run_id)
        if quarantine.is_dir():
            inventory = self._inventory(quarantine)
            if receipt.get("inventory_fingerprint") != sha256_json(inventory):
                raise RetentionDispositionError("quarantined run changed before purge")
            safe_rmtree(quarantine, root=self.quarantine_root, missing_ok=False)
            reconciled = False
        elif quarantine.exists() or quarantine.is_symlink():
            raise RetentionDispositionError("retention quarantine path is unsafe")
        else:
            reconciled = True
        payload = {
            key: value for key, value in receipt.items() if key != "artifact_fingerprint"
        }
        payload.update(
            {
                "status": "purged",
                "purged_at": current.isoformat(),
                "purge_reconciled": reconciled,
                "run_artifacts_present": False,
            }
        )
        result = {**payload, "artifact_fingerprint": sha256_json(payload)}
        atomic_write_json(receipt_path, result, mode=0o600)
        return result

    def _plan_run(self, run_dir: Path, *, now: datetime) -> dict[str, Any]:
        manifest_path = run_dir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "run_id": run_dir.name,
                "status": "unmanaged",
                "retention_status": "unknown",
                "expires_at": None,
                "blockers": [f"run manifest is unreadable: {type(exc).__name__}"],
                "references": [],
                "inventory": None,
            }
        if not isinstance(manifest, Mapping) or manifest.get("run_id") != run_dir.name:
            return {
                "run_id": run_dir.name,
                "status": "unmanaged",
                "retention_status": "unknown",
                "expires_at": None,
                "blockers": ["run manifest identity is invalid"],
                "references": [],
                "inventory": None,
            }
        governance = validate_run_governance(run_dir, manifest, now=now)
        retention = governance["retention"]
        retention_status = str(retention["status"])
        expires_at = retention.get("expires_at")
        if retention_status == "active":
            return {
                "run_id": run_dir.name,
                "status": "not_due",
                "retention_status": retention_status,
                "expires_at": expires_at,
                "blockers": [],
                "references": [],
                "inventory": None,
            }
        if retention_status != "expired":
            return {
                "run_id": run_dir.name,
                "status": "unmanaged",
                "retention_status": retention_status,
                "expires_at": expires_at,
                "blockers": ["retention policy cannot be enforced automatically"],
                "references": [],
                "inventory": None,
            }
        references, reference_errors = self._references(run_dir.name)
        blockers = list(reference_errors)
        blockers.extend(
            str(error)
            for error in governance["errors"]
            if error != "writing-material retention period has expired"
        )
        if references:
            blockers.append("run is referenced by candidate or release artifacts")
        versions = manifest.get("versions")
        provider = str(versions.get("provider") or "") if isinstance(versions, Mapping) else ""
        cache_files = list((self.data_root / "cache" / "llm").glob("*.json"))
        if provider not in {"", "deterministic_fixture"} and cache_files:
            blockers.append("provider cache lacks per-run retention scope")
        inventory = self._inventory(run_dir)
        return {
            "run_id": run_dir.name,
            "status": "blocked" if blockers else "ready",
            "retention_status": retention_status,
            "expires_at": expires_at,
            "blockers": blockers,
            "references": references,
            "inventory": inventory,
        }

    def _references(self, run_id: str) -> tuple[list[str], list[str]]:
        references: list[str] = []
        errors: list[str] = []
        roots = (
            self.data_root / "index-candidates",
            self.data_root / "release-candidates",
            self.data_root / "releases",
        )
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.json")):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    errors.append(f"reference artifact is unreadable: {path}")
                    continue
                if isinstance(value, Mapping) and value.get("run_id") == run_id:
                    references.append(str(path))
        return references, errors

    @staticmethod
    def _inventory(root: Path) -> list[dict[str, Any]]:
        inventory: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise RetentionDispositionError("retention target contains a symlink")
            if not path.is_file():
                continue
            inventory.append(
                {
                    "path": str(path.relative_to(root)),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
        return inventory

    def _write_quarantine_receipt(
        self,
        intent: Mapping[str, Any],
        *,
        receipt_path: Path,
        recovered: bool,
    ) -> dict[str, Any]:
        disposed_at = self._parse_time(intent.get("planned_at"), "planned_at")
        payload: dict[str, Any] = {
            "schema_version": RETENTION_RECEIPT_SCHEMA_VERSION,
            "status": "quarantined",
            "run_id": intent["run_id"],
            "expires_at": intent["expires_at"],
            "disposed_at": disposed_at.isoformat(),
            "purge_after": (disposed_at + timedelta(days=self.quarantine_days)).isoformat(),
            "quarantine_path": intent["quarantine_path"],
            "intent_fingerprint": intent["artifact_fingerprint"],
            "inventory_fingerprint": intent["inventory_fingerprint"],
            "file_count": len(intent["inventory"]),
            "bytes": sum(int(item["bytes"]) for item in intent["inventory"]),
            "recovered_after_interruption": recovered,
            "run_artifacts_present": True,
            "purged_at": None,
            "purge_reconciled": False,
        }
        result = {**payload, "artifact_fingerprint": sha256_json(payload)}
        if receipt_path.exists():
            existing = self._load_artifact(receipt_path, RETENTION_RECEIPT_SCHEMA_VERSION)
            if (
                existing.get("status") != "quarantined"
                or existing.get("intent_fingerprint") != intent["artifact_fingerprint"]
                or existing.get("inventory_fingerprint") != intent["inventory_fingerprint"]
            ):
                raise RetentionDispositionError("retention receipt already exists with other content")
            return existing
        atomic_write_json(receipt_path, result, mode=0o600)
        return result

    def _prepare_private_dirs(self) -> None:
        for path in (
            self.retention_root,
            self.quarantine_root,
            self.intent_root,
            self.receipt_root,
        ):
            if path.is_symlink():
                raise RetentionDispositionError("retention state directory must not be a symlink")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)

    def _run_dirs(self) -> list[Path]:
        if not self.runs_root.is_dir():
            return []
        return [path for path in sorted(self.runs_root.iterdir()) if path.is_dir()]

    def _run_dir(self, run_id: str) -> Path:
        path = self._run_path(run_id)
        if not path.is_dir() or path.is_symlink():
            raise RetentionDispositionError(f"writing-material run is missing or unsafe: {run_id}")
        return path

    def _run_path(self, run_id: str) -> Path:
        return self.runs_root / self._validated_run_id(run_id)

    def _quarantine_path(self, run_id: str) -> Path:
        return self.quarantine_root / self._validated_run_id(run_id)

    @staticmethod
    def _validated_run_id(run_id: str) -> str:
        if not _RUN_ID.fullmatch(run_id):
            raise RetentionDispositionError("invalid writing-material run ID")
        return run_id

    @staticmethod
    def _utc_now(value: datetime | None) -> datetime:
        current = value or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("retention time must include a timezone")
        return current.astimezone(timezone.utc)

    @staticmethod
    def _parse_time(value: Any, label: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise RetentionDispositionError(f"retention {label} is invalid") from exc
        if parsed.tzinfo is None:
            raise RetentionDispositionError(f"retention {label} must include a timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _load_artifact(path: Path, schema_version: str) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RetentionDispositionError(f"retention artifact is unreadable: {path}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != schema_version:
            raise RetentionDispositionError("retention artifact schema is invalid")
        fingerprint = value.get("artifact_fingerprint")
        payload = {key: item for key, item in value.items() if key != "artifact_fingerprint"}
        if fingerprint != sha256_json(payload):
            raise RetentionDispositionError("retention artifact fingerprint is invalid")
        return value
