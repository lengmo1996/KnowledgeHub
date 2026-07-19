"""Clone-and-merge release orchestration for reviewed writing materials."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from knowledgehub.core.atomic import atomic_write_json, ensure_path_within
from knowledgehub.core.hashing import sha256_json, sha256_text
from knowledgehub.writing_rag.review import WritingMaterialReviewService

RELEASE_SCHEMA_VERSION = "writing-material-release-v1"
_COLLECTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SNAPSHOT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\Z")


class ReleaseBackend(Protocol):
    def inspect(self, collection: str) -> Mapping[str, Any]: ...

    def snapshot(self, collection: str) -> Mapping[str, Any]: ...

    def restore(self, snapshot: Mapping[str, Any], target_collection: str) -> None: ...


class PromotionBackend(Protocol):
    def stage(
        self, knowledge_base: str, candidate: str, *, verified_release: dict[str, Any]
    ) -> Mapping[str, Any]: ...

    def promote(
        self, knowledge_base: str, fallback: str, *, confirmed: bool = False
    ) -> Mapping[str, Any]: ...

    def rollback(self, knowledge_base: str, *, confirmed: bool = False) -> Mapping[str, Any]: ...


class QdrantReleaseBackend:
    """Concrete snapshot/restore adapter; construction itself performs no I/O."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def inspect(self, collection: str) -> Mapping[str, Any]:
        if hasattr(self.client, "collection_exists") and not self.client.collection_exists(collection):
            return {"exists": False}
        try:
            info = self.client.get_collection(collection)
        except Exception as exc:
            if type(exc).__name__ in {"UnexpectedResponse", "NotFoundError"}:
                return {"exists": False}
            raise
        raw_status = getattr(info, "status", "")
        status = str(getattr(raw_status, "value", raw_status)).lower()
        config = getattr(info, "config", None)
        params = getattr(config, "params", None)
        schema = {
            "vectors": _model_value(getattr(params, "vectors", None)),
            "sparse_vectors": _model_value(getattr(params, "sparse_vectors", None)),
        }
        return {
            "exists": True,
            "status": status,
            "points": int(getattr(info, "points_count", 0) or 0),
            "schema": schema,
        }

    def snapshot(self, collection: str) -> Mapping[str, Any]:
        before = self.inspect(collection)
        if not before.get("exists"):
            raise ValueError("snapshot source collection is missing")
        snapshot = self.client.create_snapshot(collection, wait=True)
        if snapshot is None or not getattr(snapshot, "name", None):
            raise RuntimeError("Qdrant did not return a snapshot description")
        return {
            "collection": collection,
            "name": str(snapshot.name),
            "checksum": getattr(snapshot, "checksum", None),
            "points": before["points"],
            "schema": before["schema"],
        }

    def restore(self, snapshot: Mapping[str, Any], target_collection: str) -> None:
        if self.inspect(target_collection).get("exists"):
            raise ValueError("snapshot target collection already exists")
        source = str(snapshot.get("collection") or "")
        name = str(snapshot.get("name") or "")
        if not _COLLECTION.fullmatch(source) or not _SNAPSHOT.fullmatch(name):
            raise ValueError("snapshot identity is invalid")
        result = self.client.recover_snapshot(
            target_collection,
            location=f"file:///qdrant/snapshots/{source}/{name}",
            checksum=snapshot.get("checksum"),
            wait=True,
        )
        if not result:
            raise RuntimeError("Qdrant snapshot recovery failed")


class WritingMaterialReleaseService:
    """Clone active Writing, merge accepted assets, and validate without alias mutation."""

    def __init__(
        self,
        review: WritingMaterialReviewService,
        backend: ReleaseBackend,
        release_root: Path,
        *,
        promotion: PromotionBackend | None = None,
    ) -> None:
        self.review = review
        self.backend = backend
        self.release_root = release_root
        self.promotion = promotion

    def build(
        self,
        run_id: str,
        *,
        active_collection: str,
        candidate_collection: str,
        merge: Callable[[str], Mapping[str, Any]],
        candidate_data_dir: Path | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if (
            not active_collection
            or not candidate_collection
            or active_collection == candidate_collection
        ):
            raise ValueError("release candidate must be a distinct physical collection")
        if not _COLLECTION.fullmatch(active_collection) or not _COLLECTION.fullmatch(
            candidate_collection
        ):
            raise ValueError("release collection has an unsafe name")
        if "knowledgehub_writing_current" in {active_collection, candidate_collection}:
            raise ValueError("release collections must be physical names, not the stable alias")
        validation = self.review.validate(run_id, verify_source=True)
        if validation.get("status") != "success" or not validation.get("index_eligible"):
            raise ValueError("release requires a complete, source-verified accepted snapshot")
        accepted_manifest = self.review.run_dir(run_id) / "accepted" / "manifest.json"
        accepted_hash = sha256_text(accepted_manifest.read_text(encoding="utf-8"))
        accepted = json.loads(accepted_manifest.read_text(encoding="utf-8"))
        active = dict(self.backend.inspect(active_collection))
        candidate_before = dict(self.backend.inspect(candidate_collection))
        self._validate_collection(active, expected_exists=True, label="active")
        if candidate_before.get("exists"):
            raise ValueError("release candidate collection already exists")
        expected_additions = sum(
            int(accepted["counts"].get(asset_type, 0))
            for asset_type in ("strategy", "template", "phrase")
        )
        plan = {
            "schema_name": "writing_material_release",
            "schema_version": RELEASE_SCHEMA_VERSION,
            "run_id": run_id,
            "active_collection": active_collection,
            "candidate_collection": candidate_collection,
            "candidate_data_dir": (
                str(candidate_data_dir.resolve()) if candidate_data_dir is not None else None
            ),
            "active_points": int(active["points"]),
            "expected_additions": expected_additions,
            "expected_candidate_points": int(active["points"]) + expected_additions,
            "accepted_manifest": str(accepted_manifest),
            "accepted_manifest_sha256": accepted_hash,
            "dry_run": dry_run,
            "promotion_performed": False,
        }
        if dry_run:
            return {**plan, "status": "planned"}

        snapshot = dict(self.backend.snapshot(active_collection))
        self.backend.restore(snapshot, candidate_collection)
        cloned = dict(self.backend.inspect(candidate_collection))
        self._validate_collection(cloned, expected_exists=True, label="cloned candidate")
        if int(cloned["points"]) != int(active["points"]) or cloned.get("schema") != active.get(
            "schema"
        ):
            raise RuntimeError("candidate clone differs from the active collection")
        merge_result = dict(merge(candidate_collection))
        if merge_result.get("status") != "success" or merge_result.get("failures"):
            raise RuntimeError("accepted writing-material merge failed")
        if int(merge_result.get("indexed", -1)) != expected_additions:
            raise RuntimeError("merged asset count differs from the accepted snapshot")
        final = dict(self.backend.inspect(candidate_collection))
        self._validate_collection(final, expected_exists=True, label="merged candidate")
        if int(final["points"]) != plan["expected_candidate_points"]:
            raise RuntimeError("merged candidate point count is invalid")
        if final.get("schema") != active.get("schema"):
            raise RuntimeError("merged candidate vector schema differs from active")
        manifest = {
            **plan,
            "status": "validated",
            "dry_run": False,
            "snapshot": snapshot,
            "merge_result": merge_result,
            "candidate_validation": final,
            "promotion_eligible": True,
            "validated_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest["artifact_fingerprint"] = sha256_json(manifest)
        output = ensure_path_within(
            self.release_root / "writing" / candidate_collection / "manifest.json",
            self.release_root,
        )
        atomic_write_json(output, manifest, mode=0o600)
        return {**manifest, "manifest_path": str(output)}

    def stage(self, manifest_path: Path, *, confirmed: bool = False) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("release staging requires explicit confirmation")
        if self.promotion is None:
            raise RuntimeError("promotion backend is unavailable")
        manifest = self._verified_manifest(manifest_path)
        release = {
            **manifest,
            "collection": manifest["candidate_collection"],
            "knowledge_base": "writing",
            "validation": {
                "valid": True,
                "index": {
                    "qdrant": {
                        "status": manifest["candidate_validation"]["status"],
                        "points": manifest["candidate_validation"]["points"],
                    }
                },
            },
            "rag_data_dir": manifest.get("candidate_data_dir"),
            "manifest_path": str(manifest_path),
        }
        return dict(
            self.promotion.stage(
                "writing", str(manifest["candidate_collection"]), verified_release=release
            )
        )

    def promote(self, fallback: str, *, confirmed: bool = False) -> dict[str, Any]:
        if self.promotion is None:
            raise RuntimeError("promotion backend is unavailable")
        return dict(self.promotion.promote("writing", fallback, confirmed=confirmed))

    def rollback(self, *, confirmed: bool = False) -> dict[str, Any]:
        if self.promotion is None:
            raise RuntimeError("promotion backend is unavailable")
        return dict(self.promotion.rollback("writing", confirmed=confirmed))

    @staticmethod
    def _validate_collection(
        value: Mapping[str, Any], *, expected_exists: bool, label: str
    ) -> None:
        if bool(value.get("exists")) != expected_exists:
            raise ValueError(f"{label} collection existence is invalid")
        if expected_exists and (
            value.get("status") != "green"
            or not isinstance(value.get("points"), int)
            or int(value["points"]) < 0
            or not isinstance(value.get("schema"), Mapping)
        ):
            raise ValueError(f"{label} collection is not green or has an invalid schema")

    def _verified_manifest(self, path: Path) -> dict[str, Any]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("release manifest must be an object")
        manifest: dict[str, Any] = raw
        if (
            manifest.get("schema_version") != RELEASE_SCHEMA_VERSION
            or manifest.get("status") != "validated"
            or not manifest.get("promotion_eligible")
        ):
            raise ValueError("release manifest is not validated and promotion eligible")
        fingerprint = manifest.pop("artifact_fingerprint", None)
        if fingerprint != sha256_json(manifest):
            raise ValueError("release manifest fingerprint is invalid")
        manifest["artifact_fingerprint"] = fingerprint
        return manifest


def _model_value(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _model_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_model_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
