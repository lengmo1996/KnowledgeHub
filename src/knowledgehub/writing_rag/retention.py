"""Scheduler-safe retention planning, quarantine, and verified purge."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from knowledgehub.core.atomic import (
    atomic_write_json,
    fsync_directory,
    safe_rmtree,
    safe_unlink,
)
from knowledgehub.core.hashing import sha256_file, sha256_json
from knowledgehub.writing_rag.extract import (
    CACHE_RETENTION_SCOPE_VERSION,
    LLMCache,
)
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
        self.cache_scope_intent_root = self.retention_root / "cache-scope-intents"
        self.cache_scope_receipt_root = self.retention_root / "cache-scope-receipts"
        self.cache_purge_intent_root = self.retention_root / "cache-purge-intents"
        self.cache_purge_receipt_root = self.retention_root / "cache-purge-receipts"
        self.cache_root = self.data_root / "cache" / "llm"
        self.quarantine_days = quarantine_days

    def cache_scope_plan(self, run_id: str) -> dict[str, Any]:
        self._run_dir(run_id)
        scan = self._scan_cache(run_id)
        payload: dict[str, Any] = {
            "schema_version": "writing-material-cache-scope-plan-v1",
            "status": "blocked" if scan["invalid"] else "ready",
            "run_id": run_id,
            "cache_root": str(self.cache_root),
            "counts": {
                key: len(scan[key])
                for key in ("all", "unscoped", "scoped_to_run", "scoped_other", "invalid")
            },
            "unscoped_keys_fingerprint": sha256_json(scan["unscoped"]),
            "invalid": scan["invalid"],
            "migration_policy": "bind_all_legacy_unscoped_cache_to_approved_run",
            "dry_run": True,
            "writes_performed": False,
            "responses_modified": False,
            "llm_called": False,
        }
        return {**payload, "artifact_fingerprint": sha256_json(payload)}

    def migrate_legacy_cache_scope(
        self,
        run_id: str,
        *,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise RetentionDispositionError("cache scope migration requires explicit confirmation")
        run_id = self._validated_run_id(run_id)
        plan = self.cache_scope_plan(run_id)
        if plan["status"] != "ready":
            raise RetentionDispositionError("LLM cache contains invalid retention metadata")
        self._prepare_private_dirs()
        pending_intents = [
            path
            for path in sorted(self.cache_scope_intent_root.glob(f"{run_id}*.json"))
            if not (self.cache_scope_receipt_root / path.name).exists()
        ]
        if pending_intents:
            intent_path = pending_intents[0]
            receipt_path = self.cache_scope_receipt_root / intent_path.name
        else:
            revision = str(plan["unscoped_keys_fingerprint"])[:16]
            intent_path = self.cache_scope_intent_root / f"{run_id}-{revision}.json"
            receipt_path = self.cache_scope_receipt_root / f"{run_id}-{revision}.json"
        if plan["counts"]["unscoped"] == 0 and not pending_intents:
            receipts = sorted(self.cache_scope_receipt_root.glob(f"{run_id}*.json"))
            if receipts:
                return self._load_artifact(
                    receipts[-1],
                    "writing-material-cache-scope-receipt-v1",
                )
        if receipt_path.exists():
            receipt = self._load_artifact(
                receipt_path,
                "writing-material-cache-scope-receipt-v1",
            )
            current = self._scan_cache(run_id)
            if current["invalid"] or current["unscoped"]:
                raise RetentionDispositionError(
                    "new unscoped cache appeared after cache scope migration"
                )
            return receipt
        scan = self._scan_cache(run_id)
        target_keys = scan["unscoped"]
        intent_payload = {
            "schema_version": "writing-material-cache-scope-intent-v1",
            "run_id": run_id,
            "cache_root": str(self.cache_root),
            "target_keys": target_keys,
            "target_keys_fingerprint": sha256_json(target_keys),
            "plan_fingerprint": plan["artifact_fingerprint"],
            "migration_policy": plan["migration_policy"],
        }
        intent = {**intent_payload, "artifact_fingerprint": sha256_json(intent_payload)}
        if intent_path.exists():
            intent = self._load_artifact(
                intent_path,
                "writing-material-cache-scope-intent-v1",
            )
            target_keys = list(intent["target_keys"])
        else:
            atomic_write_json(intent_path, intent, mode=0o600)
        cache = LLMCache(self.cache_root)
        for key in target_keys:
            cache.bind_retention_scope(str(key), run_id)
        after = self._scan_cache(run_id)
        if after["invalid"] or after["unscoped"]:
            raise RetentionDispositionError("cache scope migration did not cover all cache entries")
        payload: dict[str, Any] = {
            "schema_version": "writing-material-cache-scope-receipt-v1",
            "status": "completed",
            "run_id": run_id,
            "intent_fingerprint": intent["artifact_fingerprint"],
            "migrated": len(target_keys),
            "cache_entries": len(after["all"]),
            "scoped_to_run": len(after["scoped_to_run"]),
            "unscoped": 0,
            "responses_modified": False,
            "llm_called": False,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        receipt = {**payload, "artifact_fingerprint": sha256_json(payload)}
        atomic_write_json(receipt_path, receipt, mode=0o600)
        return receipt

    def purge_cache_scope(
        self,
        run_id: str,
        *,
        confirmed: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise RetentionDispositionError("cache scope purge requires explicit confirmation")
        current = self._utc_now(now)
        run_dir = self._run_dir(run_id)
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        governance = validate_run_governance(run_dir, manifest, now=current)
        if governance["retention"]["status"] != "expired":
            raise RetentionDispositionError("cache scope purge requires an expired run")
        scan = self._scan_cache(run_id)
        if scan["invalid"] or scan["unscoped"]:
            raise RetentionDispositionError("cache scope purge requires fully scoped cache entries")
        self._prepare_private_dirs()
        intent_path = self.cache_purge_intent_root / f"{run_id}.json"
        receipt_path = self.cache_purge_receipt_root / f"{run_id}.json"
        if receipt_path.exists():
            receipt = self._load_artifact(
                receipt_path,
                "writing-material-cache-purge-receipt-v1",
            )
            current_scan = self._scan_cache(run_id)
            if (
                current_scan["invalid"]
                or current_scan["unscoped"]
                or current_scan["scoped_to_run"]
            ):
                raise RetentionDispositionError(
                    "cache entries appeared after the expired scope was purged"
                )
            return receipt
        target_keys = scan["scoped_to_run"]
        intent_payload = {
            "schema_version": "writing-material-cache-purge-intent-v1",
            "run_id": run_id,
            "target_keys": target_keys,
            "target_keys_fingerprint": sha256_json(target_keys),
            "expires_at": governance["retention"]["expires_at"],
        }
        intent = {**intent_payload, "artifact_fingerprint": sha256_json(intent_payload)}
        if intent_path.exists():
            intent = self._load_artifact(
                intent_path,
                "writing-material-cache-purge-intent-v1",
            )
            target_keys = list(intent["target_keys"])
        else:
            atomic_write_json(intent_path, intent, mode=0o600)
        removed = 0
        retained_shared = 0
        for key in target_keys:
            path = self.cache_root / f"{key}.json"
            if not path.exists():
                continue
            value = self._cache_value(path)
            scopes = list(value["retention_scope_run_ids"])
            if run_id not in scopes:
                continue
            remaining = sorted(scope for scope in scopes if scope != run_id)
            if remaining:
                updated = dict(value) | {
                    "retention_scope_run_ids": remaining,
                    "retention_scope_fingerprint": sha256_json(
                        {
                            "cache_key": str(key),
                            "version": CACHE_RETENTION_SCOPE_VERSION,
                            "run_ids": remaining,
                        }
                    ),
                }
                atomic_write_json(path, updated, mode=0o600)
                retained_shared += 1
            else:
                safe_unlink(path, root=self.cache_root, missing_ok=False)
                removed += 1
        after = self._scan_cache(run_id)
        if after["invalid"] or after["unscoped"] or after["scoped_to_run"]:
            raise RetentionDispositionError("cache scope purge did not remove the expired scope")
        payload = {
            "schema_version": "writing-material-cache-purge-receipt-v1",
            "status": "completed",
            "run_id": run_id,
            "intent_fingerprint": intent["artifact_fingerprint"],
            "removed": removed,
            "retained_shared": retained_shared,
            "responses_modified": False,
            "llm_called": False,
            "completed_at": current.isoformat(),
        }
        receipt = {**payload, "artifact_fingerprint": sha256_json(payload)}
        atomic_write_json(receipt_path, receipt, mode=0o600)
        return receipt

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
        if provider not in {"", "deterministic_fixture"}:
            cache_scan = self._scan_cache(run_dir.name)
            if cache_scan["invalid"] or cache_scan["unscoped"]:
                blockers.append("provider cache lacks complete per-run retention scope")
            if cache_scan["scoped_to_run"]:
                blockers.append("provider cache scope must be purged before run quarantine")
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

    def _scan_cache(self, run_id: str) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {
            "all": [],
            "unscoped": [],
            "scoped_to_run": [],
            "scoped_other": [],
            "invalid": [],
        }
        if not self.cache_root.is_dir():
            return result
        for path in sorted(self.cache_root.glob("*.json")):
            key = path.stem
            result["all"].append(key)
            try:
                value = self._cache_value(path, allow_unscoped=True)
            except RetentionDispositionError:
                result["invalid"].append(key)
                continue
            scopes = value.get("retention_scope_run_ids")
            if scopes is None or value.get("retention_scope_fingerprint") is None:
                result["unscoped"].append(key)
            elif run_id in scopes:
                result["scoped_to_run"].append(key)
            else:
                result["scoped_other"].append(key)
        return result

    @staticmethod
    def _cache_value(path: Path, *, allow_unscoped: bool = False) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RetentionDispositionError(f"LLM cache entry is unreadable: {path}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("response"), Mapping):
            raise RetentionDispositionError("LLM cache entry structure is invalid")
        scopes = value.get("retention_scope_run_ids")
        version = value.get("retention_scope_version")
        fingerprint = value.get("retention_scope_fingerprint")
        if scopes is None and version is None and fingerprint is None and allow_unscoped:
            return value
        if (
            version != CACHE_RETENTION_SCOPE_VERSION
            or not isinstance(scopes, list)
            or not scopes
            or scopes != sorted(set(scopes))
            or any(
                not isinstance(scope, str) or not _RUN_ID.fullmatch(scope)
                for scope in scopes
            )
        ):
            raise RetentionDispositionError("LLM cache retention scope is invalid")
        if fingerprint is None and allow_unscoped:
            return value
        if fingerprint != sha256_json(
            {
                "cache_key": path.stem,
                "version": CACHE_RETENTION_SCOPE_VERSION,
                "run_ids": scopes,
            }
        ):
            raise RetentionDispositionError("LLM cache retention scope fingerprint is invalid")
        return value

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
            self.cache_scope_intent_root,
            self.cache_scope_receipt_root,
            self.cache_purge_intent_root,
            self.cache_purge_receipt_root,
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
