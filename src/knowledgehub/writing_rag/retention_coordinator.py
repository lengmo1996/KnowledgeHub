"""Recoverable orchestration for complete writing-material retention disposal."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from knowledgehub.core.atomic import atomic_write_json
from knowledgehub.core.hashing import sha256_json
from knowledgehub.writing_rag.release_retention import (
    WritingMaterialReleaseRetirementService,
)
from knowledgehub.writing_rag.retention import (
    RetentionDispositionError,
    WritingMaterialRetentionService,
)

DISPOSITION_PLAN_VERSION = "writing-material-coordinated-disposition-plan-v1"
DISPOSITION_INTENT_VERSION = "writing-material-coordinated-disposition-intent-v1"
DISPOSITION_RECEIPT_VERSION = "writing-material-coordinated-disposition-receipt-v1"
PURGE_PLAN_VERSION = "writing-material-coordinated-purge-plan-v1"
PURGE_RECEIPT_VERSION = "writing-material-coordinated-purge-receipt-v1"

_EXPECTED_PREREQUISITE_BLOCKERS = {
    "run is referenced by candidate or release artifacts",
    "provider cache scope must be purged before run quarantine",
}
_UNSCOPED_CACHE_BLOCKER = "provider cache lacks complete per-run retention scope"


class WritingMaterialRetentionCoordinator:
    """Order and resume cache, release, and run retention transitions."""

    def __init__(
        self,
        retention: WritingMaterialRetentionService,
        release: WritingMaterialReleaseRetirementService,
    ) -> None:
        if retention.data_root != release.data_root:
            raise ValueError("coordinated retention services must share one data root")
        self.retention = retention
        self.release = release
        self.data_root = retention.data_root
        self.root = self.data_root / "retention"
        self.intent_root = self.root / "coordinated-intents"
        self.receipt_root = self.root / "coordinated-receipts"
        self.purge_receipt_root = self.root / "coordinated-purge-receipts"

    def plan(self, run_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        current = self._utc_now(now)
        run_receipt = self.retention.run_disposition_receipt(run_id)
        if run_receipt is not None:
            status = (
                "completed" if run_receipt.get("status") in {"quarantined", "purged"} else "blocked"
            )
            return self._plan_result(
                run_id,
                current,
                status=status,
                expires_at=run_receipt.get("expires_at"),
                steps=self._completed_steps(run_id, run_receipt),
                blockers=[] if status == "completed" else ["run disposition receipt is invalid"],
            )
        base = self.retention.plan(run_id, now=current)
        entry = dict(base["entries"][0])
        if base["status"] == "not_due":
            return self._plan_result(
                run_id,
                current,
                status="not_due",
                expires_at=entry.get("expires_at"),
                steps={
                    "cache_scope_purge": {"status": "not_due", "required": False},
                    "release_retirement": {"status": "not_due", "required": False},
                    "run_quarantine": {"status": "not_due", "required": False},
                },
                blockers=[],
            )
        blockers = [
            str(value)
            for value in entry.get("blockers") or ()
            if value not in _EXPECTED_PREREQUISITE_BLOCKERS and value != _UNSCOPED_CACHE_BLOCKER
        ]
        base_blockers = set(str(value) for value in entry.get("blockers") or ())

        cache_receipt = self.retention.cache_purge_receipt(run_id)
        if cache_receipt is not None:
            cache_plan = self.retention.cache_scope_plan(run_id)
            cache_drift = (
                cache_plan["status"] != "ready"
                or cache_plan["counts"]["unscoped"]
                or cache_plan["counts"]["scoped_to_run"]
            )
            cache_step = self._receipt_step(cache_receipt)
            if cache_drift:
                cache_step["status"] = "blocked"
                blockers.append("provider cache scope reappeared after purge")
        elif _UNSCOPED_CACHE_BLOCKER in base_blockers:
            cache_plan = self.retention.cache_scope_plan(run_id)
            cache_step = {
                "status": "blocked",
                "required": True,
                "plan_fingerprint": cache_plan["artifact_fingerprint"],
            }
            blockers.append(_UNSCOPED_CACHE_BLOCKER)
        elif "provider cache scope must be purged before run quarantine" in base_blockers:
            cache_plan = self.retention.cache_scope_plan(run_id)
            if cache_plan["status"] != "ready" or cache_plan["counts"]["unscoped"]:
                blockers.append("provider cache scope is not safely purgeable")
                cache_status = "blocked"
            else:
                cache_status = "pending"
            cache_step = {
                "status": cache_status,
                "required": True,
                "plan_fingerprint": cache_plan["artifact_fingerprint"],
                "scoped_entries": cache_plan["counts"]["scoped_to_run"],
            }
        else:
            cache_step = {"status": "not_required", "required": False}

        release_receipt = self.release.retirement_receipt(run_id)
        references = list(entry.get("references") or ())
        if release_receipt is not None:
            validation = self.release.validate_retirement(run_id)
            release_step = self._receipt_step(release_receipt)
            if not validation["valid"]:
                release_step["status"] = "blocked"
                blockers.extend(str(value) for value in validation["errors"])
        elif references:
            release_plan = self.release.plan(run_id, now=current)
            release_step = {
                "status": "pending" if release_plan["status"] == "ready" else "blocked",
                "required": True,
                "plan_fingerprint": release_plan["artifact_fingerprint"],
                "reference_count": len(references),
                "collection_count": len(release_plan["collections"]),
            }
            if release_plan["status"] != "ready":
                blockers.extend(str(value) for value in release_plan["blockers"])
        else:
            release_step = {"status": "not_required", "required": False}

        steps = {
            "cache_scope_purge": cache_step,
            "release_retirement": release_step,
            "run_quarantine": {"status": "pending", "required": True},
        }
        return self._plan_result(
            run_id,
            current,
            status="blocked" if blockers else "ready",
            expires_at=entry.get("expires_at"),
            steps=steps,
            blockers=sorted(set(blockers)),
        )

    def dispose(
        self,
        run_id: str,
        *,
        confirmed: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise RetentionDispositionError(
                "coordinated disposition requires explicit confirmation"
            )
        current = self._utc_now(now)
        self._prepare_dirs()
        intent_path = self.intent_root / f"{run_id}.json"
        receipt_path = self.receipt_root / f"{run_id}.json"
        if receipt_path.is_file():
            receipt = self._load(receipt_path, DISPOSITION_RECEIPT_VERSION)
            run_receipt = self.retention.run_disposition_receipt(run_id)
            if run_receipt is None or (
                run_receipt.get("status") != "purged"
                and run_receipt.get("artifact_fingerprint")
                != receipt.get("run_disposition_receipt_fingerprint")
            ):
                raise RetentionDispositionError("coordinated run disposition receipt drifted")
            return receipt
        if intent_path.is_file():
            intent = self._load(intent_path, DISPOSITION_INTENT_VERSION)
        else:
            existing_run = self.retention.run_disposition_receipt(run_id)
            if existing_run is not None:
                payload = {
                    "schema_version": DISPOSITION_INTENT_VERSION,
                    "run_id": run_id,
                    "planned_at": current.isoformat(),
                    "expires_at": existing_run.get("expires_at"),
                    "plan_fingerprint": None,
                    "steps": self._completed_steps(run_id, existing_run),
                    "recovered_existing_disposition": True,
                }
            else:
                plan = self.plan(run_id, now=current)
                if plan["status"] != "ready":
                    raise RetentionDispositionError("coordinated disposition plan is not ready")
                payload = {
                    "schema_version": DISPOSITION_INTENT_VERSION,
                    "run_id": run_id,
                    "planned_at": current.isoformat(),
                    "expires_at": plan["expires_at"],
                    "plan_fingerprint": plan["artifact_fingerprint"],
                    "steps": plan["steps"],
                    "recovered_existing_disposition": False,
                }
            intent = {**payload, "artifact_fingerprint": sha256_json(payload)}
            atomic_write_json(intent_path, intent, mode=0o600)

        run_receipt = self.retention.run_disposition_receipt(run_id)
        steps = dict(intent["steps"])
        if run_receipt is None:
            if steps["cache_scope_purge"]["required"]:
                cache = self.retention.purge_cache_scope(run_id, confirmed=True, now=current)
            else:
                cache = None
            if steps["release_retirement"]["required"]:
                release = self.release.decommission(run_id, confirmed=True, now=current)
            else:
                release = None
            run_receipt = self.retention.quarantine(run_id, confirmed=True, now=current)
        else:
            cache = self.retention.cache_purge_receipt(run_id)
            release = self.release.retirement_receipt(run_id)
        if run_receipt.get("status") not in {"quarantined", "purged"}:
            raise RetentionDispositionError("run quarantine did not complete")
        if steps["cache_scope_purge"]["required"] and cache is None:
            raise RetentionDispositionError("required cache purge receipt is missing")
        if steps["release_retirement"]["required"] and release is None:
            raise RetentionDispositionError("required release retirement receipt is missing")
        payload = {
            "schema_version": DISPOSITION_RECEIPT_VERSION,
            "status": "completed",
            "run_id": run_id,
            "intent_fingerprint": intent["artifact_fingerprint"],
            "cache_purge_receipt_fingerprint": (
                cache.get("artifact_fingerprint") if cache is not None else None
            ),
            "release_retirement_receipt_fingerprint": (
                release.get("artifact_fingerprint") if release is not None else None
            ),
            "run_disposition_receipt_fingerprint": run_receipt["artifact_fingerprint"],
            "completed_at": current.isoformat(),
            "llm_called": False,
        }
        receipt = {**payload, "artifact_fingerprint": sha256_json(payload)}
        atomic_write_json(receipt_path, receipt, mode=0o600)
        return receipt

    def purge_plan(self, run_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        current = self._utc_now(now)
        run_receipt = self.retention.run_disposition_receipt(run_id)
        disposition_path = self.receipt_root / f"{run_id}.json"
        disposition = (
            self._load(disposition_path, DISPOSITION_RECEIPT_VERSION)
            if disposition_path.is_file()
            else None
        )
        references_required = bool(
            (disposition and disposition.get("release_retirement_receipt_fingerprint") is not None)
            or self.release.retirement_receipt(run_id) is not None
        )
        reference_plan = (
            self.release.reference_purge_plan(run_id, now=current)
            if references_required
            else {
                "status": "not_required",
                "run_id": run_id,
                "purge_after": None,
                "blockers": [],
            }
        )
        blockers: list[str] = []
        if run_receipt is None:
            blockers.append("run has not entered retention quarantine")
            run_status = "not_available"
            run_purge_after = None
        elif run_receipt.get("status") == "purged":
            run_status = "purged"
            run_purge_after = run_receipt.get("purged_at")
        else:
            run_purge_after = run_receipt.get("purge_after")
            parsed = self._parse_time(run_purge_after, "run purge_after")
            run_status = "ready" if current >= parsed else "grace_period"
        if reference_plan["status"] == "blocked":
            blockers.extend(reference_plan["blockers"])
        statuses = {run_status, str(reference_plan["status"])}
        if blockers or "not_available" in statuses:
            status = "blocked"
        elif statuses <= {"purged", "not_required"}:
            status = "purged"
        elif statuses <= {"ready", "purged", "not_required"}:
            status = "ready"
        else:
            status = "grace_period"
        payload = {
            "schema_version": PURGE_PLAN_VERSION,
            "status": status,
            "run_id": run_id,
            "created_at": current.isoformat(),
            "run": {"status": run_status, "purge_after": run_purge_after},
            "release_references": reference_plan,
            "blockers": blockers,
            "dry_run": True,
            "writes_performed": False,
            "index_modified": False,
            "llm_called": False,
        }
        return {**payload, "artifact_fingerprint": sha256_json(payload)}

    def purge(
        self,
        run_id: str,
        *,
        confirmed: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise RetentionDispositionError("coordinated purge requires explicit confirmation")
        current = self._utc_now(now)
        self._prepare_dirs()
        receipt_path = self.purge_receipt_root / f"{run_id}.json"
        if receipt_path.is_file():
            receipt = self._load(receipt_path, PURGE_RECEIPT_VERSION)
            if self.purge_plan(run_id, now=current)["status"] != "purged":
                raise RetentionDispositionError("data reappeared after coordinated purge")
            return receipt
        plan = self.purge_plan(run_id, now=current)
        if plan["status"] not in {"ready", "purged"}:
            raise RetentionDispositionError("coordinated purge plan is not ready")
        references_required = plan["release_references"]["status"] != "not_required"
        reference = (
            self.release.purge_references(run_id, confirmed=True, now=current)
            if references_required
            else None
        )
        run = self.retention.purge(run_id, confirmed=True, now=current)
        payload = {
            "schema_version": PURGE_RECEIPT_VERSION,
            "status": "purged",
            "run_id": run_id,
            "plan_fingerprint": plan["artifact_fingerprint"],
            "reference_purge_receipt_fingerprint": (
                reference["artifact_fingerprint"] if reference is not None else None
            ),
            "run_purge_receipt_fingerprint": run["artifact_fingerprint"],
            "purged_at": current.isoformat(),
            "llm_called": False,
        }
        receipt = {**payload, "artifact_fingerprint": sha256_json(payload)}
        atomic_write_json(receipt_path, receipt, mode=0o600)
        return receipt

    def _completed_steps(self, run_id: str, run_receipt: Mapping[str, Any]) -> dict[str, Any]:
        cache = self.retention.cache_purge_receipt(run_id)
        release = self.release.retirement_receipt(run_id)
        return {
            "cache_scope_purge": self._receipt_step(cache),
            "release_retirement": self._receipt_step(release),
            "run_quarantine": self._receipt_step(run_receipt),
        }

    @staticmethod
    def _receipt_step(receipt: Mapping[str, Any] | None) -> dict[str, Any]:
        return {
            "status": "completed" if receipt is not None else "not_required",
            "required": receipt is not None,
            "receipt_fingerprint": (
                receipt.get("artifact_fingerprint") if receipt is not None else None
            ),
        }

    def _plan_result(
        self,
        run_id: str,
        now: datetime,
        *,
        status: str,
        expires_at: Any,
        steps: dict[str, Any],
        blockers: list[str],
    ) -> dict[str, Any]:
        payload = {
            "schema_version": DISPOSITION_PLAN_VERSION,
            "status": status,
            "run_id": run_id,
            "created_at": now.isoformat(),
            "expires_at": expires_at,
            "steps": steps,
            "blockers": blockers,
            "dry_run": True,
            "writes_performed": False,
            "index_modified": False,
            "llm_called": False,
        }
        return {**payload, "artifact_fingerprint": sha256_json(payload)}

    def _prepare_dirs(self) -> None:
        for path in (self.root, self.intent_root, self.receipt_root, self.purge_receipt_root):
            if path.is_symlink():
                raise RetentionDispositionError("coordinated retention state path is a symlink")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)

    @staticmethod
    def _load(path: Path, schema_version: str) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != schema_version:
            raise RetentionDispositionError("coordinated retention artifact schema is invalid")
        fingerprint = value.get("artifact_fingerprint")
        payload = {key: item for key, item in value.items() if key != "artifact_fingerprint"}
        if fingerprint != sha256_json(payload):
            raise RetentionDispositionError("coordinated retention artifact fingerprint is invalid")
        return value

    @staticmethod
    def _utc_now(value: datetime | None) -> datetime:
        current = value or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("coordinated retention time must include a timezone")
        return current.astimezone(timezone.utc)

    @staticmethod
    def _parse_time(value: Any, label: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise RetentionDispositionError(f"coordinated {label} is invalid") from exc
        if parsed.tzinfo is None:
            raise RetentionDispositionError(f"coordinated {label} must include a timezone")
        return parsed.astimezone(timezone.utc)
