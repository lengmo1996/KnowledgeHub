"""Alias-safe retirement of expired writing-material release artifacts."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from knowledgehub.core.atomic import (
    atomic_replace,
    atomic_write_json,
    ensure_path_within,
    safe_rmtree,
)
from knowledgehub.core.hashing import sha256_file, sha256_json
from knowledgehub.writing_rag.retention import RetentionDispositionError
from knowledgehub.writing_rag.review import validate_run_governance

RELEASE_RETIREMENT_PLAN_VERSION = "writing-material-release-retirement-plan-v1"
RELEASE_RETIREMENT_INTENT_VERSION = "writing-material-release-retirement-intent-v1"
RELEASE_RETIREMENT_RECEIPT_VERSION = "writing-material-release-retirement-receipt-v1"
REFERENCE_PURGE_PLAN_VERSION = "writing-material-release-reference-purge-plan-v1"
REFERENCE_PURGE_INTENT_VERSION = "writing-material-release-reference-purge-intent-v1"
REFERENCE_PURGE_RECEIPT_VERSION = "writing-material-release-reference-purge-receipt-v1"
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_COLLECTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class RetirementBackend(Protocol):
    def inspect(self, collection: str) -> Mapping[str, Any]: ...

    def alias_target(self, alias: str) -> str | None: ...

    def delete_collection(self, collection: str) -> None: ...


class RetirementPromotion(Protocol):
    def status(self, knowledge_base: str, fallback: str) -> Mapping[str, Any]: ...

    def rollback(self, knowledge_base: str, *, confirmed: bool = False) -> Mapping[str, Any]: ...

    def finalize_retired_previous(
        self,
        knowledge_base: str,
        retired_collection: str,
        *,
        confirmed: bool = False,
    ) -> Mapping[str, Any]: ...


class WritingMaterialReleaseRetirementService:
    """Retire run-owned releases only after a verified alias fallback."""

    def __init__(
        self,
        data_root: Path,
        backend: RetirementBackend,
        promotion: RetirementPromotion,
        *,
        fallback_collection: str,
        quarantine_days: int = 30,
    ) -> None:
        if quarantine_days < 1:
            raise ValueError("release reference quarantine must be at least one day")
        self.data_root = data_root.expanduser().resolve(strict=False)
        self.runs_root = self.data_root / "runs"
        self.backend = backend
        self.promotion = promotion
        self.fallback_collection = self._collection(fallback_collection)
        self.retention_root = self.data_root / "retention"
        self.intent_root = self.retention_root / "release-retirement-intents"
        self.receipt_root = self.retention_root / "release-retirement-receipts"
        self.quarantine_root = self.retention_root / "release-reference-quarantine"
        self.purge_intent_root = self.retention_root / "release-reference-purge-intents"
        self.purge_receipt_root = self.retention_root / "release-reference-purge-receipts"
        self.quarantine_days = quarantine_days
        self.reference_roots = {
            "index-candidates": self.data_root / "index-candidates",
            "release-candidates": self.data_root / "release-candidates",
            "releases": self.data_root / "releases",
        }

    def close(self) -> None:
        client = getattr(self.backend, "client", None)
        if client is not None and hasattr(client, "close"):
            client.close()

    def plan(self, run_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        run_id = self._run_id(run_id)
        current = self._utc_now(now)
        run_dir = self.runs_root / run_id
        manifest = self._run_manifest(run_dir, run_id)
        governance = validate_run_governance(run_dir, manifest, now=current)
        retention = governance["retention"]
        if retention["status"] == "active":
            return self._plan_result(
                run_id,
                current,
                status="not_due",
                expires_at=retention.get("expires_at"),
                references=[],
                directories=[],
                collections=[],
                alias_action=None,
                blockers=[],
            )
        blockers = [
            str(error)
            for error in governance["errors"]
            if error != "writing-material retention period has expired"
        ]
        if retention["status"] != "expired":
            blockers.append("retention policy cannot be enforced automatically")
        references, scan_errors = self._references(run_id)
        blockers.extend(scan_errors)
        directories: list[dict[str, Any]] = []
        collections: list[dict[str, Any]] = []
        alias_action: dict[str, Any] | None = None
        if retention["status"] == "expired" and not scan_errors:
            directories, collections, ownership_errors = self._owned_targets(run_id, references)
            blockers.extend(ownership_errors)
            if not ownership_errors:
                alias_action, alias_errors = self._alias_action(collections)
                blockers.extend(alias_errors)
        if not references:
            blockers.append("run has no release references to retire")
        return self._plan_result(
            run_id,
            current,
            status="blocked" if blockers else "ready",
            expires_at=retention.get("expires_at"),
            references=references,
            directories=directories,
            collections=collections,
            alias_action=alias_action,
            blockers=blockers,
        )

    def decommission(
        self,
        run_id: str,
        *,
        confirmed: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise RetentionDispositionError("release retirement requires explicit confirmation")
        run_id = self._run_id(run_id)
        current = self._utc_now(now)
        self._prepare_dirs()
        intent_path = self.intent_root / f"{run_id}.json"
        receipt_path = self.receipt_root / f"{run_id}.json"
        if receipt_path.exists():
            receipt = self._load(receipt_path, RELEASE_RETIREMENT_RECEIPT_VERSION)
            intent = self._load(intent_path, RELEASE_RETIREMENT_INTENT_VERSION)
            self._verify_retirement_completion(run_id, intent)
            return receipt
        if intent_path.exists():
            intent = self._load(intent_path, RELEASE_RETIREMENT_INTENT_VERSION)
        else:
            plan = self.plan(run_id, now=current)
            if plan["status"] != "ready":
                raise RetentionDispositionError("release retirement plan is not ready")
            payload = {
                "schema_version": RELEASE_RETIREMENT_INTENT_VERSION,
                "run_id": run_id,
                "planned_at": current.isoformat(),
                "plan_fingerprint": plan["artifact_fingerprint"],
                "references": plan["references"],
                "directories": plan["directories"],
                "collections": plan["collections"],
                "alias_action": plan["alias_action"],
            }
            intent = {**payload, "artifact_fingerprint": sha256_json(payload)}
            atomic_write_json(intent_path, intent, mode=0o600)

        self._verify_resume_safety(intent, now=current)

        alias_action = intent.get("alias_action")
        rollback_performed_this_attempt = False
        if isinstance(alias_action, Mapping):
            rollback_performed_this_attempt = self._ensure_fallback(dict(alias_action))

        deleted: list[str] = []
        for item in intent["collections"]:
            collection = self._collection(str(item["collection"]))
            if self.backend.alias_target("knowledgehub_writing_current") == collection:
                raise RetentionDispositionError("refusing to delete the live alias target")
            if self.backend.inspect(collection).get("exists"):
                self.backend.delete_collection(collection)
            if self.backend.inspect(collection).get("exists"):
                raise RetentionDispositionError("retired collection still exists")
            deleted.append(collection)

        quarantined: list[str] = []
        for item in intent["directories"]:
            source = Path(str(item["source"]))
            destination = Path(str(item["quarantine_path"]))
            root = self.reference_roots[str(item["root"])]
            ensure_path_within(source, root)
            ensure_path_within(destination, self.quarantine_root)
            if source.exists() and destination.exists():
                raise RetentionDispositionError("release reference exists in two locations")
            if source.exists():
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_replace(source, destination)
            if not destination.is_dir() or self._inventory(destination) != item["inventory"]:
                raise RetentionDispositionError("quarantined release reference inventory differs")
            quarantined.append(str(destination))

        if isinstance(alias_action, Mapping):
            self.promotion.finalize_retired_previous(
                "writing",
                str(alias_action["retired_collection"]),
                confirmed=True,
            )
        payload = {
            "schema_version": RELEASE_RETIREMENT_RECEIPT_VERSION,
            "status": "completed",
            "run_id": run_id,
            "intent_fingerprint": intent["artifact_fingerprint"],
            "rollback_performed": bool(
                isinstance(alias_action, Mapping) and alias_action.get("operation") == "rollback"
            ),
            "rollback_performed_this_attempt": rollback_performed_this_attempt,
            "alias_target": self.backend.alias_target("knowledgehub_writing_current"),
            "deleted_collections": sorted(deleted),
            "quarantined_directories": sorted(quarantined),
            "completed_at": current.isoformat(),
            "llm_called": False,
        }
        receipt = {**payload, "artifact_fingerprint": sha256_json(payload)}
        atomic_write_json(receipt_path, receipt, mode=0o600)
        return receipt

    def retirement_receipt(self, run_id: str) -> dict[str, Any] | None:
        path = self.receipt_root / f"{self._run_id(run_id)}.json"
        return self._load(path, RELEASE_RETIREMENT_RECEIPT_VERSION) if path.is_file() else None

    def validate_retirement(self, run_id: str) -> dict[str, Any]:
        run_id = self._run_id(run_id)
        receipt = self.retirement_receipt(run_id)
        if receipt is None:
            return {"status": "not_available", "valid": False, "errors": []}
        try:
            intent = self._load(
                self.intent_root / f"{run_id}.json",
                RELEASE_RETIREMENT_INTENT_VERSION,
            )
            self._verify_retirement_completion(run_id, intent)
        except Exception as exc:
            return {
                "status": "blocked",
                "valid": False,
                "errors": [f"{type(exc).__name__}: {exc}"],
                "receipt_fingerprint": receipt["artifact_fingerprint"],
            }
        return {
            "status": "completed",
            "valid": True,
            "errors": [],
            "receipt_fingerprint": receipt["artifact_fingerprint"],
        }

    def reference_purge_plan(
        self,
        run_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        run_id = self._run_id(run_id)
        current = self._utc_now(now)
        retirement = self.retirement_receipt(run_id)
        purge_receipt_path = self.purge_receipt_root / f"{run_id}.json"
        if purge_receipt_path.is_file():
            receipt = self._load(purge_receipt_path, REFERENCE_PURGE_RECEIPT_VERSION)
            if (self.quarantine_root / run_id).exists():
                return self._reference_purge_plan_result(
                    run_id,
                    current,
                    status="blocked",
                    purge_after=receipt["purged_at"],
                    directories=[],
                    blockers=["release reference data reappeared after purge"],
                )
            return self._reference_purge_plan_result(
                run_id,
                current,
                status="purged",
                purge_after=receipt["purged_at"],
                directories=[],
                blockers=[],
            )
        purge_intent_path = self.purge_intent_root / f"{run_id}.json"
        if purge_intent_path.is_file():
            intent = self._load(purge_intent_path, REFERENCE_PURGE_INTENT_VERSION)
            purge_after = self._parse_time(intent.get("purge_after"), "purge_after")
            directories = [dict(item) for item in intent["directories"]]
            resume_blockers: list[str] = []
            for item in directories:
                path = Path(str(item["quarantine_path"]))
                ensure_path_within(path, self.quarantine_root)
                if path.is_symlink():
                    resume_blockers.append(f"release reference quarantine path is unsafe: {path}")
                elif path.is_dir() and self._inventory(path) != item["inventory"]:
                    resume_blockers.append(
                        f"quarantined release reference changed during purge: {path}"
                    )
                elif path.exists() and not path.is_dir():
                    resume_blockers.append(f"release reference quarantine path is unsafe: {path}")
            status = (
                "blocked"
                if resume_blockers
                else "grace_period"
                if current < purge_after
                else "ready"
            )
            return self._reference_purge_plan_result(
                run_id,
                current,
                status=status,
                purge_after=purge_after.isoformat(),
                directories=directories,
                blockers=resume_blockers,
            )
        if retirement is None:
            return self._reference_purge_plan_result(
                run_id,
                current,
                status="not_available",
                purge_after=None,
                directories=[],
                blockers=["release retirement has not completed"],
            )
        completed_at = self._parse_time(retirement.get("completed_at"), "completed_at")
        purge_after = completed_at + timedelta(days=self.quarantine_days)
        intent = self._load(
            self.intent_root / f"{run_id}.json",
            RELEASE_RETIREMENT_INTENT_VERSION,
        )
        directories = [dict(item) for item in intent["directories"]]
        blockers: list[str] = []
        for item in intent["collections"]:
            collection = self._collection(str(item["collection"]))
            if self.backend.inspect(collection).get("exists"):
                blockers.append(f"retired collection reappeared: {collection}")
            if self.backend.alias_target("knowledgehub_writing_current") == collection:
                blockers.append(f"live alias targets retired collection: {collection}")
        for item in directories:
            path = Path(str(item["quarantine_path"]))
            ensure_path_within(path, self.quarantine_root)
            if not path.is_dir() or self._inventory(path) != item["inventory"]:
                blockers.append(f"quarantined release reference changed or is missing: {path}")
        if blockers:
            status = "blocked"
        elif current < purge_after:
            status = "grace_period"
        else:
            status = "ready"
        return self._reference_purge_plan_result(
            run_id,
            current,
            status=status,
            purge_after=purge_after.isoformat(),
            directories=directories,
            blockers=blockers,
        )

    def purge_references(
        self,
        run_id: str,
        *,
        confirmed: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise RetentionDispositionError(
                "release reference purge requires explicit confirmation"
            )
        run_id = self._run_id(run_id)
        current = self._utc_now(now)
        self._prepare_dirs()
        intent_path = self.purge_intent_root / f"{run_id}.json"
        receipt_path = self.purge_receipt_root / f"{run_id}.json"
        if receipt_path.is_file():
            receipt = self._load(receipt_path, REFERENCE_PURGE_RECEIPT_VERSION)
            if (self.quarantine_root / run_id).exists():
                raise RetentionDispositionError("release reference data reappeared after purge")
            return receipt
        if intent_path.is_file():
            intent = self._load(intent_path, REFERENCE_PURGE_INTENT_VERSION)
        else:
            plan = self.reference_purge_plan(run_id, now=current)
            if plan["status"] != "ready":
                raise RetentionDispositionError("release reference purge plan is not ready")
            payload = {
                "schema_version": REFERENCE_PURGE_INTENT_VERSION,
                "run_id": run_id,
                "planned_at": current.isoformat(),
                "purge_after": plan["purge_after"],
                "plan_fingerprint": plan["artifact_fingerprint"],
                "directories": plan["directories"],
            }
            intent = {**payload, "artifact_fingerprint": sha256_json(payload)}
            atomic_write_json(intent_path, intent, mode=0o600)
        if current < self._parse_time(intent.get("purge_after"), "purge_after"):
            raise RetentionDispositionError("release reference quarantine grace period is active")
        reconciled = False
        for item in intent["directories"]:
            path = Path(str(item["quarantine_path"]))
            ensure_path_within(path, self.quarantine_root)
            if path.is_dir():
                if self._inventory(path) != item["inventory"]:
                    raise RetentionDispositionError(
                        "quarantined release reference changed before purge"
                    )
                safe_rmtree(path, root=self.quarantine_root, missing_ok=False)
            elif path.exists() or path.is_symlink():
                raise RetentionDispositionError("release reference quarantine path is unsafe")
            else:
                reconciled = True
        run_root = self.quarantine_root / run_id
        if run_root.is_dir():
            if self._inventory(run_root):
                raise RetentionDispositionError(
                    "unexpected release reference remains in quarantine"
                )
            safe_rmtree(run_root, root=self.quarantine_root, missing_ok=False)
        retirement = self.retirement_receipt(run_id)
        if retirement is None:
            raise RetentionDispositionError("release retirement receipt disappeared before purge")
        payload = {
            "schema_version": REFERENCE_PURGE_RECEIPT_VERSION,
            "status": "purged",
            "run_id": run_id,
            "retirement_receipt_fingerprint": retirement["artifact_fingerprint"],
            "intent_fingerprint": intent["artifact_fingerprint"],
            "purged_directories": len(intent["directories"]),
            "purged_at": current.isoformat(),
            "purge_reconciled": reconciled,
            "llm_called": False,
        }
        receipt = {**payload, "artifact_fingerprint": sha256_json(payload)}
        atomic_write_json(receipt_path, receipt, mode=0o600)
        return receipt

    def _verify_retirement_completion(
        self,
        run_id: str,
        intent: Mapping[str, Any],
    ) -> None:
        references, reference_errors = self._references(run_id)
        if references or reference_errors:
            raise RetentionDispositionError(
                "release references reappeared after retirement completion"
            )
        for item in intent["collections"]:
            collection = self._collection(str(item["collection"]))
            if self.backend.inspect(collection).get("exists"):
                raise RetentionDispositionError("retired collection reappeared after completion")
            if self.backend.alias_target("knowledgehub_writing_current") == collection:
                raise RetentionDispositionError("live alias returned to a retired collection")
        references_purged = (self.purge_receipt_root / f"{run_id}.json").is_file()
        for item in intent["directories"]:
            destination = Path(str(item["quarantine_path"]))
            ensure_path_within(destination, self.quarantine_root)
            if references_purged and not destination.exists():
                continue
            if not destination.is_dir() or self._inventory(destination) != item["inventory"]:
                raise RetentionDispositionError(
                    "quarantined release reference changed after retirement"
                )

    def _reference_purge_plan_result(
        self,
        run_id: str,
        now: datetime,
        *,
        status: str,
        purge_after: str | None,
        directories: list[dict[str, Any]],
        blockers: list[str],
    ) -> dict[str, Any]:
        payload = {
            "schema_version": REFERENCE_PURGE_PLAN_VERSION,
            "status": status,
            "run_id": run_id,
            "created_at": now.isoformat(),
            "quarantine_days": self.quarantine_days,
            "purge_after": purge_after,
            "directories": directories,
            "blockers": blockers,
            "dry_run": True,
            "writes_performed": False,
            "index_modified": False,
            "llm_called": False,
        }
        return {**payload, "artifact_fingerprint": sha256_json(payload)}

    def _verify_resume_safety(self, intent: Mapping[str, Any], *, now: datetime) -> None:
        run_id = self._run_id(str(intent.get("run_id") or ""))
        run_dir = self.runs_root / run_id
        governance = validate_run_governance(
            run_dir,
            self._run_manifest(run_dir, run_id),
            now=now,
        )
        errors = [
            str(error)
            for error in governance["errors"]
            if error != "writing-material retention period has expired"
        ]
        if governance["retention"]["status"] != "expired" or errors:
            raise RetentionDispositionError("run is no longer eligible for release retirement")
        _, scan_errors = self._references(run_id)
        if scan_errors:
            raise RetentionDispositionError("release reference scan is not trustworthy")
        owners = self._collection_owners()
        for item in intent["collections"]:
            collection = self._collection(str(item["collection"]))
            if owners.get(collection, set()) - {run_id}:
                raise RetentionDispositionError("collection acquired another run owner")
            observed = dict(self.backend.inspect(collection))
            expected = dict(item["inspection"])
            if observed.get("exists"):
                if not item.get("exists") or observed != expected:
                    raise RetentionDispositionError("run-owned collection changed after planning")
        for item in intent["directories"]:
            source = Path(str(item["source"]))
            destination = Path(str(item["quarantine_path"]))
            root = self.reference_roots[str(item["root"])]
            ensure_path_within(source, root)
            ensure_path_within(destination, self.quarantine_root)
            if source.exists() == destination.exists():
                raise RetentionDispositionError(
                    "release reference must exist in exactly one managed location"
                )
            present = source if source.exists() else destination
            if not present.is_dir() or self._inventory(present) != item["inventory"]:
                raise RetentionDispositionError("release reference changed after planning")

    def _ensure_fallback(self, action: dict[str, Any]) -> bool:
        retired = self._collection(str(action["retired_collection"]))
        fallback = self._collection(str(action["fallback_collection"]))
        status = self.promotion.status("writing", self.fallback_collection)
        current = dict(status.get("current") or {})
        live = self.backend.alias_target("knowledgehub_writing_current")
        operation = action.get("operation")
        if (
            operation == "rollback"
            and live == retired
            and current.get("active_collection") == retired
        ):
            result = self.promotion.rollback("writing", confirmed=True)
            if result.get("active_collection") != fallback:
                raise RetentionDispositionError("alias rollback selected an unexpected fallback")
            live = self.backend.alias_target("knowledgehub_writing_current")
            if live != fallback:
                raise RetentionDispositionError("live alias did not switch to the fallback")
            return True
        if (
            live == fallback
            and current.get("active_collection") == fallback
            and current.get("previous_collection") in {retired, None}
        ):
            return False
        raise RetentionDispositionError("alias state drift prevents release retirement")

    def _alias_action(
        self, collections: list[dict[str, Any]]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        errors: list[str] = []
        owned = {str(item["collection"]) for item in collections}
        status = self.promotion.status("writing", self.fallback_collection)
        current = dict(status.get("current") or {})
        staged = dict(status.get("staged") or {})
        alias = str(status.get("alias") or "")
        live = self.backend.alias_target("knowledgehub_writing_current")
        active = str(current.get("active_collection") or "")
        if alias != "knowledgehub_writing_current" or live != active:
            errors.append("live alias target differs from promotion state")
            return None, errors
        previous = str(current.get("previous_collection") or "")
        staged_collection = str(staged.get("candidate_collection") or "")
        if staged_collection in owned and staged_collection not in {active, previous}:
            errors.append("staged promotion state references a run-owned collection")
        if active in owned:
            retired = active
            fallback = previous
            operation = "rollback"
            if previous in owned or previous == active or not _COLLECTION.fullmatch(previous):
                errors.append("active run has no independent safe fallback collection")
                return None, errors
        elif previous in owned:
            retired = previous
            fallback = active
            operation = "retire_previous"
        else:
            return None, errors
        try:
            info = dict(self.backend.inspect(fallback))
        except Exception as exc:
            errors.append(f"fallback collection inspection failed: {exc}")
            return None, errors
        if not self._healthy(info):
            errors.append("fallback collection is missing or unhealthy")
            return None, errors
        return {
            "operation": operation,
            "alias": alias,
            "retired_collection": retired,
            "fallback_collection": fallback,
        }, errors

    def _owned_targets(
        self, run_id: str, references: list[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        errors: list[str] = []
        directories: dict[str, dict[str, Any]] = {}
        collection_names: set[str] = set()
        for raw_path in references:
            path = Path(raw_path)
            root_name, root = self._reference_root(path)
            try:
                value = self._verified_reference(path, run_id)
                collection_names.add(self._collection(str(value.get("candidate_collection") or "")))
                source = path.parent.resolve(strict=False)
                relative = source.relative_to(root.resolve(strict=False))
                quarantine = self.quarantine_root / run_id / root_name / relative
                directories[str(source)] = {
                    "root": root_name,
                    "source": str(source),
                    "quarantine_path": str(quarantine),
                    "inventory": self._inventory(source),
                }
            except (OSError, ValueError, RetentionDispositionError) as exc:
                errors.append(f"invalid release reference {path}: {exc}")
        other_owners = self._collection_owners()
        collections: list[dict[str, Any]] = []
        for name in sorted(collection_names):
            owners = other_owners.get(name, set())
            if owners != {run_id}:
                errors.append(f"collection ownership is ambiguous: {name}")
                continue
            try:
                info = dict(self.backend.inspect(name))
            except Exception as exc:
                errors.append(f"run-owned collection inspection failed: {name}: {exc}")
                continue
            if info.get("exists") and not self._healthy(info):
                errors.append(f"run-owned collection is unhealthy: {name}")
            collections.append(
                {"collection": name, "exists": bool(info.get("exists")), "inspection": info}
            )
        return (
            list(sorted(directories.values(), key=lambda item: item["source"])),
            collections,
            errors,
        )

    def _references(self, run_id: str) -> tuple[list[str], list[str]]:
        references: list[str] = []
        errors: list[str] = []
        for root in self.reference_roots.values():
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.json")):
                if path.is_symlink():
                    errors.append(f"release reference is a symlink: {path}")
                    continue
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    errors.append(f"release reference is unreadable: {path}")
                    continue
                if isinstance(value, Mapping) and value.get("run_id") == run_id:
                    references.append(str(path.resolve(strict=False)))
        return references, errors

    def _collection_owners(self) -> dict[str, set[str]]:
        owners: dict[str, set[str]] = {}
        for root in self.reference_roots.values():
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.json")):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(value, Mapping):
                    continue
                name = value.get("candidate_collection")
                owner = value.get("run_id")
                if isinstance(name, str) and isinstance(owner, str):
                    owners.setdefault(name, set()).add(owner)
        return owners

    def _verified_reference(self, path: Path, run_id: str) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("run_id") != run_id:
            raise RetentionDispositionError("run identity differs")
        fingerprint = value.pop("artifact_fingerprint", None)
        if fingerprint != sha256_json(value):
            raise RetentionDispositionError("artifact fingerprint is invalid")
        value["artifact_fingerprint"] = fingerprint
        return value

    def _reference_root(self, path: Path) -> tuple[str, Path]:
        for name, root in self.reference_roots.items():
            try:
                path.resolve(strict=False).relative_to(root.resolve(strict=False))
                return name, root
            except ValueError:
                continue
        raise RetentionDispositionError("release reference is outside managed roots")

    @staticmethod
    def _inventory(root: Path) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise RetentionDispositionError("release artifact directory contains a symlink")
            if path.is_file():
                result.append(
                    {
                        "path": str(path.relative_to(root)),
                        "bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                )
        return result

    @staticmethod
    def _healthy(info: Mapping[str, Any]) -> bool:
        return (
            info.get("exists") is True
            and info.get("status") == "green"
            and isinstance(info.get("points"), int)
            and int(info["points"]) > 0
            and isinstance(info.get("schema"), Mapping)
        )

    def _plan_result(
        self,
        run_id: str,
        now: datetime,
        *,
        status: str,
        expires_at: Any,
        references: list[str],
        directories: list[dict[str, Any]],
        collections: list[dict[str, Any]],
        alias_action: dict[str, Any] | None,
        blockers: list[str],
    ) -> dict[str, Any]:
        payload = {
            "schema_version": RELEASE_RETIREMENT_PLAN_VERSION,
            "status": status,
            "run_id": run_id,
            "created_at": now.isoformat(),
            "expires_at": expires_at,
            "references": references,
            "directories": directories,
            "collections": collections,
            "alias_action": alias_action,
            "blockers": blockers,
            "dry_run": True,
            "writes_performed": False,
            "index_modified": False,
            "llm_called": False,
        }
        return {**payload, "artifact_fingerprint": sha256_json(payload)}

    @staticmethod
    def _run_manifest(run_dir: Path, run_id: str) -> dict[str, Any]:
        if not run_dir.is_dir() or run_dir.is_symlink():
            raise RetentionDispositionError("writing-material run is missing or unsafe")
        value = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("run_id") != run_id:
            raise RetentionDispositionError("writing-material run identity is invalid")
        return value

    def _prepare_dirs(self) -> None:
        for path in (
            self.retention_root,
            self.intent_root,
            self.receipt_root,
            self.quarantine_root,
            self.purge_intent_root,
            self.purge_receipt_root,
        ):
            if path.is_symlink():
                raise RetentionDispositionError("release retirement state path is a symlink")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)

    @staticmethod
    def _load(path: Path, schema_version: str) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != schema_version:
            raise RetentionDispositionError("release retirement artifact schema is invalid")
        fingerprint = value.get("artifact_fingerprint")
        payload = {key: item for key, item in value.items() if key != "artifact_fingerprint"}
        if fingerprint != sha256_json(payload):
            raise RetentionDispositionError("release retirement artifact fingerprint is invalid")
        return value

    @staticmethod
    def _utc_now(value: datetime | None) -> datetime:
        current = value or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("release retirement time must include a timezone")
        return current.astimezone(timezone.utc)

    @staticmethod
    def _parse_time(value: Any, label: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise RetentionDispositionError(f"release retirement {label} is invalid") from exc
        if parsed.tzinfo is None:
            raise RetentionDispositionError(f"release retirement {label} must include a timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _run_id(value: str) -> str:
        if not _RUN_ID.fullmatch(value):
            raise RetentionDispositionError("invalid writing-material run ID")
        return value

    @staticmethod
    def _collection(value: str) -> str:
        if not _COLLECTION.fullmatch(value) or value == "knowledgehub_writing_current":
            raise RetentionDispositionError("invalid physical collection name")
        return value
