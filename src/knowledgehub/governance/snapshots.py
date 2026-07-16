"""Qdrant server snapshot manifests with explicit, confirmation-gated recovery."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from knowledgehub.core.atomic import atomic_write_json


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


class IndexSnapshotManager:
    def __init__(self, root: Path, client: Any) -> None:
        self.root = root
        self.client = client

    def create(self, knowledge_base: str, collection: str) -> dict[str, Any]:
        info = self.client.get_collection(collection)
        snapshot = self.client.create_snapshot(collection, wait=True)
        if snapshot is None:
            raise RuntimeError("Qdrant did not return a snapshot description")
        created = datetime.now(timezone.utc).isoformat()
        snapshot_id = f"{created[:19].replace(':', '').replace('-', '')}-{snapshot.name}"
        manifest = {
            "schema_name": "index_snapshot_manifest",
            "schema_version": "2.0",
            "snapshot_id": snapshot_id,
            "knowledge_base": knowledge_base,
            "collection": collection,
            "qdrant_snapshot": snapshot.name,
            "checksum": getattr(snapshot, "checksum", None),
            "points": int(info.points_count or 0),
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

    def rollback(self, knowledge_base: str, snapshot_id: str, *, confirmed: bool = False) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("rollback requires explicit confirmation")
        path = self.root / knowledge_base / "snapshots" / f"{snapshot_id}.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        collection = str(manifest["collection"])
        location = f"file:///qdrant/snapshots/{collection}/{manifest['qdrant_snapshot']}"
        result = self.client.recover_snapshot(
            collection, location=location, checksum=manifest.get("checksum"), wait=True
        )
        if not result:
            raise RuntimeError("Qdrant snapshot recovery failed")
        restored = {**manifest, "restored_at": datetime.now(timezone.utc).isoformat()}
        atomic_write_json(self.root / knowledge_base / "current.json", restored)
        return restored


class CollectionPromotionManager:
    """Stage physical collections and atomically move a stable Qdrant alias."""

    def __init__(self, root: Path, client: Any) -> None:
        self.root = root
        self.client = client

    @staticmethod
    def alias_name(knowledge_base: str) -> str:
        return f"knowledgehub_{knowledge_base}_current"

    def stage(self, knowledge_base: str, candidate: str) -> dict[str, Any]:
        alias = self.alias_name(knowledge_base)
        if candidate == alias:
            raise ValueError("candidate must be a physical collection, not the stable alias")
        info = self.client.get_collection(candidate)
        points = int(getattr(info, "points_count", 0) or 0)
        if points <= 0:
            raise ValueError("candidate collection must contain at least one point")
        value = {
            "schema_name": "collection_promotion",
            "schema_version": "2.0",
            "knowledge_base": knowledge_base,
            "alias": alias,
            "candidate_collection": candidate,
            "candidate_points": points,
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
        staged = self._read(knowledge_base, "staged.json")
        if not staged:
            raise ValueError("no staged candidate collection")
        candidate = str(staged["candidate_collection"])
        info = self.client.get_collection(candidate)
        points = int(getattr(info, "points_count", 0) or 0)
        if points <= 0:
            raise ValueError("candidate collection is empty")
        current = self._read(knowledge_base, "current.json") or {}
        previous = str(current.get("active_collection") or fallback)
        alias = self.alias_name(knowledge_base)
        self._switch(alias, candidate)
        value = {
            **staged,
            "candidate_points": points,
            "status": "active",
            "active_collection": candidate,
            "previous_collection": previous,
            "promoted_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(self._path(knowledge_base, "current.json"), value)
        return value

    def rollback(
        self, knowledge_base: str, *, confirmed: bool = False
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("alias rollback requires explicit confirmation")
        current = self._read(knowledge_base, "current.json")
        if not current or not current.get("previous_collection"):
            raise ValueError("no previous collection is available")
        previous = str(current["previous_collection"])
        active = str(current["active_collection"])
        alias = self.alias_name(knowledge_base)
        self.client.get_collection(previous)
        self._switch(alias, previous)
        value = {
            **current,
            "status": "active",
            "active_collection": previous,
            "previous_collection": active,
            "rolled_back_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(self._path(knowledge_base, "current.json"), value)
        return value

    def _switch(self, alias: str, collection: str) -> None:
        from qdrant_client import models

        aliases = getattr(self.client.get_aliases(), "aliases", ())
        exists = any(getattr(item, "alias_name", None) == alias for item in aliases)
        operations: list[Any] = []
        if exists:
            operations.append(
                models.DeleteAliasOperation(
                    delete_alias=models.DeleteAlias(alias_name=alias)
                )
            )
        operations.append(
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(
                    collection_name=collection, alias_name=alias
                )
            )
        )
        if not self.client.update_collection_aliases(operations):
            raise RuntimeError("Qdrant alias update failed")

    def _path(self, knowledge_base: str, name: str) -> Path:
        return self.root / knowledge_base / "aliases" / name

    def _read(self, knowledge_base: str, name: str) -> dict[str, Any] | None:
        path = self._path(knowledge_base, name)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
