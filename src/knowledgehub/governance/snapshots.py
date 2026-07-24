"""Qdrant server snapshot manifests with explicit, confirmation-gated recovery."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from knowledgehub.core.atomic import atomic_write_json, safe_unlink
from knowledgehub.governance.releases import CandidateReleaseManager


def active_collection(root: Path, knowledge_base: str, fallback: str) -> str:
    """Return the promoted alias only after a successful, persisted promotion."""
    path = root / knowledge_base / "aliases" / "current.json"
    if not path.is_file():
        return fallback
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback
    if value.get("status") != "active" or not value.get("alias"):
        return fallback
    return str(value["alias"])


def active_release_data_dir(root: Path, knowledge_base: str, fallback: Path) -> Path:
    """Resolve the immutable local artifact root paired with the active alias."""
    path = root / knowledge_base / "aliases" / "current.json"
    if not path.is_file():
        return fallback
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback
    selected = value.get("rag_data_dir") if value.get("status") == "active" else None
    return Path(str(selected)) if selected and Path(str(selected)).is_dir() else fallback


def active_release_metadata(root: Path, knowledge_base: str) -> dict[str, Any] | None:
    """Return a copy of the active promotion record without trusting its paths."""
    path = root / knowledge_base / "aliases" / "current.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("status") != "active":
        return None
    return dict(value)


def active_release_normalized_root(
    root: Path,
    knowledge_base: str,
    fallback: Path,
) -> Path:
    """Resolve normalized manifests paired with the active immutable release."""
    path = root / knowledge_base / "aliases" / "current.json"
    if not path.is_file():
        return fallback
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        release_path = current.get("release_manifest")
        release = (
            json.loads(Path(str(release_path)).read_text(encoding="utf-8"))
            if current.get("status") == "active" and release_path
            else {}
        )
    except (OSError, ValueError):
        return fallback
    selected = release.get("normalized_root")
    return Path(str(selected)) if selected and Path(str(selected)).is_dir() else fallback


class IndexSnapshotManager:
    def __init__(self, root: Path, client: Any) -> None:
        self.root = root
        self.client = client

    def create(
        self,
        knowledge_base: str,
        collection: str,
        *,
        release_manifest: Path | None = None,
    ) -> dict[str, Any]:
        release: dict[str, Any] | None = None
        if release_manifest is not None:
            release = CandidateReleaseManager(
                release_manifest.resolve().parents[2]
            ).verify_validated(release_manifest)
            if release.get("collection") != collection:
                raise ValueError("snapshot collection differs from the active release")
        info = self.client.get_collection(collection)
        snapshot = self.client.create_snapshot(collection, wait=True)
        if snapshot is None:
            raise RuntimeError("Qdrant did not return a snapshot description")
        created = datetime.now(timezone.utc).isoformat()
        snapshot_id = f"{created[:19].replace(':', '').replace('-', '')}-{snapshot.name}"
        manifest = {
            "schema_name": "index_snapshot_manifest",
            "schema_version": "2.1",
            "snapshot_id": snapshot_id,
            "knowledge_base": knowledge_base,
            "collection": collection,
            "qdrant_snapshot": snapshot.name,
            "checksum": getattr(snapshot, "checksum", None),
            "points": int(info.points_count or 0),
            "cross_store_complete": release is not None,
            "release_manifest": str(release_manifest) if release_manifest else None,
            "artifact_fingerprint": (
                release.get("artifact_fingerprint") if release is not None else None
            ),
            "created_at": created,
        }
        path = self.root / knowledge_base / "snapshots" / f"{snapshot_id}.json"
        atomic_write_json(path, manifest)
        atomic_write_json(self.root / knowledge_base / "current.json", manifest)
        return manifest

    def list(self, knowledge_base: str) -> list[dict[str, Any]]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((self.root / knowledge_base / "snapshots").glob("*.json"))
        ]

    def rollback(
        self,
        knowledge_base: str,
        snapshot_id: str,
        *,
        target_collection: str | None = None,
        allow_qdrant_only: bool = False,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("rollback requires explicit confirmation")
        path = self.root / knowledge_base / "snapshots" / f"{snapshot_id}.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        source_collection = str(manifest["collection"])
        if not target_collection:
            raise ValueError("snapshot recovery requires a new target collection")
        cross_store_complete = bool(manifest.get("cross_store_complete"))
        if not cross_store_complete and not allow_qdrant_only:
            raise ValueError(
                "legacy snapshot covers Qdrant only; explicit qdrant-only recovery is required"
            )
        if target_collection in {
            source_collection,
            CollectionPromotionManager.alias_name(knowledge_base),
        }:
            raise ValueError("snapshot recovery must not overwrite the source or stable alias")
        if hasattr(self.client, "collection_exists") and self.client.collection_exists(
            target_collection
        ):
            raise ValueError("snapshot recovery target collection already exists")
        release_manager: CandidateReleaseManager | None = None
        source_release: dict[str, Any] | None = None
        if cross_store_complete:
            source_manifest = Path(str(manifest["release_manifest"]))
            release_manager = CandidateReleaseManager(source_manifest.resolve().parents[2])
            source_release = release_manager.verify_validated(source_manifest)
            if source_release.get("artifact_fingerprint") != manifest.get("artifact_fingerprint"):
                raise ValueError("snapshot release fingerprint no longer matches")
            if release_manager.layout(knowledge_base, target_collection).root.exists():
                raise ValueError("snapshot recovery candidate release already exists")
        location = f"file:///qdrant/snapshots/{source_collection}/{manifest['qdrant_snapshot']}"
        result = self.client.recover_snapshot(
            target_collection,
            location=location,
            checksum=manifest.get("checksum"),
            wait=True,
        )
        if not result:
            raise RuntimeError("Qdrant snapshot recovery failed")
        restored: dict[str, Any] = {
            **manifest,
            "source_collection": source_collection,
            "candidate_collection": target_collection,
            "recovery_status": "candidate",
            "restored_at": datetime.now(timezone.utc).isoformat(),
        }
        if cross_store_complete:
            if release_manager is None or source_release is None:
                raise RuntimeError("cross-store snapshot has no verified release")
            layout = release_manager.prepare(
                knowledge_base,
                target_collection,
                build_scope={
                    "recovered_from_snapshot": snapshot_id,
                    "source_release": source_release.get("release_id"),
                    "expected_documents": (source_release.get("build_scope") or {}).get(
                        "expected_documents"
                    ),
                    "source_document_fingerprint": (source_release.get("build_scope") or {}).get(
                        "source_document_fingerprint"
                    ),
                },
                embedding=dict(source_release.get("embedding") or {}),
                promotion_eligible=True,
            )
            try:
                shutil.copytree(
                    Path(str(source_release["normalized_root"])),
                    layout.normalized_root,
                    dirs_exist_ok=True,
                )
                shutil.copytree(
                    Path(str(source_release["rag_data_dir"])),
                    layout.rag_data_dir,
                    dirs_exist_ok=True,
                )
                release_manager.record_build(
                    layout,
                    [dict(item) for item in source_release.get("build_results") or ()],
                )
                recovered_release = release_manager.validate(
                    layout,
                    qdrant_client=self.client,
                )
            except BaseException as error:
                release_manager.record_failure(layout, error)
                raise
            if recovered_release.get("status") != "validated":
                raise RuntimeError("recovered candidate failed cross-store validation")
            restored.update(
                {
                    "release_manifest": str(layout.manifest_path),
                    "artifact_fingerprint": recovered_release["artifact_fingerprint"],
                    "validation": recovered_release["validation"],
                }
            )
        return restored


class CollectionPromotionManager:
    """Stage physical collections and atomically move a stable Qdrant alias."""

    def __init__(self, root: Path, client: Any) -> None:
        self.root = root
        self.client = client

    @staticmethod
    def alias_name(knowledge_base: str) -> str:
        return f"knowledgehub_{knowledge_base}_current"

    def stage(
        self,
        knowledge_base: str,
        candidate: str,
        *,
        verified_release: dict[str, Any],
    ) -> dict[str, Any]:
        self._ensure_no_pending(knowledge_base)
        alias = self.alias_name(knowledge_base)
        if candidate == alias:
            raise ValueError("candidate must be a physical collection, not the stable alias")
        info = self.client.get_collection(candidate)
        points = int(getattr(info, "points_count", 0) or 0)
        if points <= 0:
            raise ValueError("candidate collection must contain at least one point")
        if verified_release.get("status") != "validated":
            raise ValueError("candidate release must be validated before staging")
        if not verified_release.get("promotion_eligible"):
            raise ValueError("candidate release is not promotion eligible")
        if verified_release.get("collection") != candidate:
            raise ValueError("candidate collection differs from its release manifest")
        validation = verified_release.get("validation") or {}
        if not validation.get("valid"):
            raise ValueError("candidate release validation did not pass")
        validated_qdrant = (validation.get("index") or {}).get("qdrant") or {}
        if validated_qdrant.get("status") != "green":
            raise ValueError("candidate release Qdrant collection is not green")
        if int(validated_qdrant.get("points") or 0) != points:
            raise ValueError("candidate point count changed after validation")
        value = {
            "schema_name": "collection_promotion",
            "schema_version": "2.0",
            "knowledge_base": knowledge_base,
            "alias": alias,
            "candidate_collection": candidate,
            "candidate_points": points,
            "release_manifest": str(verified_release.get("manifest_path") or ""),
            "artifact_fingerprint": verified_release.get("artifact_fingerprint"),
            "rag_data_dir": verified_release.get("rag_data_dir"),
            "status": "staged",
            "staged_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(self._path(knowledge_base, "staged.json"), value)
        return value

    def status(self, knowledge_base: str, fallback: str) -> dict[str, Any]:
        current = self._read(knowledge_base, "current.json")
        staged = self._read(knowledge_base, "staged.json")
        return {
            "knowledge_base": knowledge_base,
            "alias": self.alias_name(knowledge_base),
            "query_collection": active_collection(self.root, knowledge_base, fallback),
            "current": current,
            "staged": staged,
        }

    def promote(
        self, knowledge_base: str, fallback: str, *, confirmed: bool = False
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("promotion requires explicit confirmation")
        self._ensure_no_pending(knowledge_base)
        staged = self._read(knowledge_base, "staged.json")
        if not staged:
            raise ValueError("no staged candidate collection")
        candidate = str(staged["candidate_collection"])
        info = self.client.get_collection(candidate)
        points = int(getattr(info, "points_count", 0) or 0)
        if points <= 0:
            raise ValueError("candidate collection is empty")
        current = self._read(knowledge_base, "current.json") or {}
        alias = self.alias_name(knowledge_base)
        actual = self._actual_alias(alias)
        previous = str(actual or current.get("active_collection") or fallback)
        transaction = {
            "schema_name": "collection_promotion_transaction",
            "schema_version": "2.1",
            "knowledge_base": knowledge_base,
            "operation": "promote",
            "alias": alias,
            "previous_collection": previous,
            "candidate_collection": candidate,
            "candidate_points": points,
            "previous_rag_data_dir": current.get("rag_data_dir"),
            "candidate_rag_data_dir": staged.get("rag_data_dir"),
            "candidate_release_manifest": staged.get("release_manifest"),
            "candidate_artifact_fingerprint": staged.get("artifact_fingerprint"),
            "status": "prepared",
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(knowledge_base, "transaction.json", transaction)
        self._switch(alias, candidate)
        transaction = {
            **transaction,
            "status": "alias_switched",
            "alias_switched_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(knowledge_base, "transaction.json", transaction)
        self._after_alias_switch()
        value = {
            **staged,
            "candidate_points": points,
            "status": "active",
            "active_collection": candidate,
            "previous_collection": previous,
            "previous_rag_data_dir": current.get("rag_data_dir"),
            "previous_release_manifest": current.get("release_manifest"),
            "previous_artifact_fingerprint": current.get("artifact_fingerprint"),
            "promoted_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(knowledge_base, "current.json", value)
        self._write(
            knowledge_base,
            "transaction.json",
            {
                **transaction,
                "status": "committed",
                "committed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return value

    def rollback(self, knowledge_base: str, *, confirmed: bool = False) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("alias rollback requires explicit confirmation")
        self._ensure_no_pending(knowledge_base)
        current = self._read(knowledge_base, "current.json")
        if not current or not current.get("previous_collection"):
            raise ValueError("no previous collection is available")
        previous = str(current["previous_collection"])
        active = str(current["active_collection"])
        alias = self.alias_name(knowledge_base)
        self.client.get_collection(previous)
        transaction = {
            "schema_name": "collection_promotion_transaction",
            "schema_version": "2.1",
            "knowledge_base": knowledge_base,
            "operation": "rollback",
            "alias": alias,
            "previous_collection": active,
            "candidate_collection": previous,
            "candidate_points": int(
                getattr(self.client.get_collection(previous), "points_count", 0) or 0
            ),
            "previous_rag_data_dir": current.get("rag_data_dir"),
            "candidate_rag_data_dir": current.get("previous_rag_data_dir"),
            "candidate_release_manifest": current.get("previous_release_manifest"),
            "candidate_artifact_fingerprint": current.get("previous_artifact_fingerprint"),
            "status": "prepared",
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(knowledge_base, "transaction.json", transaction)
        self._switch(alias, previous)
        transaction = {
            **transaction,
            "status": "alias_switched",
            "alias_switched_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(knowledge_base, "transaction.json", transaction)
        self._after_alias_switch()
        value = {
            **current,
            "status": "active",
            "active_collection": previous,
            "previous_collection": active,
            "rag_data_dir": current.get("previous_rag_data_dir"),
            "previous_rag_data_dir": current.get("rag_data_dir"),
            "release_manifest": current.get("previous_release_manifest"),
            "artifact_fingerprint": current.get("previous_artifact_fingerprint"),
            "previous_release_manifest": current.get("release_manifest"),
            "previous_artifact_fingerprint": current.get("artifact_fingerprint"),
            "rolled_back_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(knowledge_base, "current.json", value)
        self._write(
            knowledge_base,
            "transaction.json",
            {
                **transaction,
                "status": "committed",
                "committed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return value

    def finalize_retired_previous(
        self,
        knowledge_base: str,
        retired_collection: str,
        *,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Forget a decommissioned rollback target after its alias-safe retirement."""
        if not confirmed:
            raise ValueError("previous collection retirement requires explicit confirmation")
        self._ensure_no_pending(knowledge_base)
        current = self._read(knowledge_base, "current.json")
        if not current:
            raise ValueError("no active promotion state is available")
        alias = self.alias_name(knowledge_base)
        active = str(current.get("active_collection") or "")
        previous = current.get("previous_collection")
        if self._actual_alias(alias) != active:
            raise RuntimeError("live alias target differs from promotion state")
        staged = self._read(knowledge_base, "staged.json")
        if previous is None:
            if staged and staged.get("candidate_collection") == retired_collection:
                safe_unlink(
                    self._path(knowledge_base, "staged.json"),
                    root=self.root,
                )
            return current
        if previous != retired_collection or active == retired_collection:
            raise ValueError("retired collection is not the inactive rollback target")
        if staged and staged.get("candidate_collection") not in {
            retired_collection,
            active,
        }:
            raise ValueError("an unrelated staged collection prevents retirement")
        value = {
            **current,
            "previous_collection": None,
            "previous_rag_data_dir": None,
            "previous_release_manifest": None,
            "previous_artifact_fingerprint": None,
            "retired_collection": retired_collection,
            "retired_at": datetime.now(timezone.utc).isoformat(),
        }
        if staged and staged.get("candidate_collection") == retired_collection:
            safe_unlink(
                self._path(knowledge_base, "staged.json"),
                root=self.root,
            )
        self._write(knowledge_base, "current.json", value)
        return value

    def recover_pending(self, knowledge_base: str, fallback: str) -> dict[str, Any]:
        transaction = self._read(knowledge_base, "transaction.json")
        if not transaction or transaction.get("status") in {"committed", "aborted"}:
            return {"status": "none", "knowledge_base": knowledge_base}
        alias = self.alias_name(knowledge_base)
        actual = self._actual_alias(alias)
        candidate = str(transaction["candidate_collection"])
        previous = str(transaction["previous_collection"])
        if actual == candidate:
            current = self._read(knowledge_base, "staged.json") or {}
            value = {
                **current,
                "knowledge_base": knowledge_base,
                "alias": alias,
                "status": "active",
                "active_collection": candidate,
                "previous_collection": previous,
                "candidate_points": transaction.get("candidate_points"),
                "rag_data_dir": transaction.get("candidate_rag_data_dir"),
                "previous_rag_data_dir": transaction.get("previous_rag_data_dir"),
                "release_manifest": transaction.get("candidate_release_manifest"),
                "artifact_fingerprint": transaction.get("candidate_artifact_fingerprint"),
                "recovered_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write(knowledge_base, "current.json", value)
            outcome = "committed"
        elif actual in {previous, None}:
            outcome = "aborted"
        else:
            raise RuntimeError(
                f"alias {alias} points to unexpected collection {actual}; manual review required"
            )
        result = {
            **transaction,
            "status": outcome,
            "recovered_at": datetime.now(timezone.utc).isoformat(),
            "observed_collection": actual or fallback,
        }
        self._write(knowledge_base, "transaction.json", result)
        return result

    def _switch(self, alias: str, collection: str) -> None:
        from qdrant_client import models

        aliases = getattr(self.client.get_aliases(), "aliases", ())
        exists = any(getattr(item, "alias_name", None) == alias for item in aliases)
        operations: list[Any] = []
        if exists:
            operations.append(
                models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias))
            )
        operations.append(
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(collection_name=collection, alias_name=alias)
            )
        )
        if not self.client.update_collection_aliases(operations):
            raise RuntimeError("Qdrant alias update failed")

    def _actual_alias(self, alias: str) -> str | None:
        aliases = getattr(self.client.get_aliases(), "aliases", ())
        return next(
            (
                str(item.collection_name)
                for item in aliases
                if getattr(item, "alias_name", None) == alias
            ),
            None,
        )

    def _ensure_no_pending(self, knowledge_base: str) -> None:
        transaction = self._read(knowledge_base, "transaction.json")
        if transaction and transaction.get("status") not in {"committed", "aborted"}:
            raise RuntimeError("pending promotion transaction must be recovered first")

    def _after_alias_switch(self) -> None:
        """Fault-injection seam; production behavior is intentionally empty."""

    def _write(self, knowledge_base: str, name: str, value: dict[str, Any]) -> None:
        atomic_write_json(self._path(knowledge_base, name), value)

    def _path(self, knowledge_base: str, name: str) -> Path:
        return self.root / knowledge_base / "aliases" / name

    def _read(self, knowledge_base: str, name: str) -> dict[str, Any] | None:
        path = self._path(knowledge_base, name)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
