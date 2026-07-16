"""Qdrant server snapshot manifests with explicit, confirmation-gated recovery."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from knowledgehub.core.atomic import atomic_write_json


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
