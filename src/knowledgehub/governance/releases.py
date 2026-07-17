"""Immutable candidate release layouts and cross-store integrity manifests."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from knowledgehub.core.atomic import atomic_write_json, ensure_path_within
from knowledgehub.core.hashing import sha256_file, sha256_json
from knowledgehub.governance.validation import HubValidator

_COLLECTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class CandidateReleaseLayout:
    """All local artifacts associated with one physical candidate collection."""

    knowledge_base: str
    collection: str
    root: Path

    @property
    def normalized_root(self) -> Path:
        return self.root / "normalized"

    @property
    def rag_data_dir(self) -> Path:
        return self.root / "rag"

    @property
    def manifest_path(self) -> Path:
        return self.root / "release.json"


class CandidateReleaseManager:
    """Prepare, record and validate immutable pre-promotion releases."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=False)

    def layout(self, knowledge_base: str, collection: str) -> CandidateReleaseLayout:
        if knowledge_base not in {"code", "writing"}:
            raise ValueError("candidate releases support only code or writing")
        if not _COLLECTION.fullmatch(collection):
            raise ValueError("candidate collection has an unsafe name")
        alias = f"knowledgehub_{knowledge_base}_current"
        if collection == alias:
            raise ValueError("candidate collection must be physical, not the stable alias")
        release_root = ensure_path_within(
            self.root / knowledge_base / collection,
            self.root,
        )
        return CandidateReleaseLayout(knowledge_base, collection, release_root)

    def prepare(
        self,
        knowledge_base: str,
        collection: str,
        *,
        build_scope: Mapping[str, Any],
        embedding: Mapping[str, Any],
        promotion_eligible: bool,
    ) -> CandidateReleaseLayout:
        layout = self.layout(knowledge_base, collection)
        if layout.root.exists():
            raise FileExistsError(
                f"candidate release already exists; use a new collection name: {layout.root}"
            )
        layout.root.mkdir(parents=True, mode=0o700)
        manifest = {
            "schema_name": "candidate_index_release",
            "schema_version": "2.1",
            "knowledge_base": knowledge_base,
            "release_id": collection,
            "collection": collection,
            "status": "building",
            "promotion_eligible": promotion_eligible,
            "build_scope": dict(build_scope),
            "embedding": dict(embedding),
            "normalized_root": str(layout.normalized_root),
            "rag_data_dir": str(layout.rag_data_dir),
            "created_at": _now(),
        }
        atomic_write_json(layout.manifest_path, manifest, mode=0o600)
        return layout

    def record_build(
        self,
        layout: CandidateReleaseLayout,
        results: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        manifest = self.load(layout)
        self._require_status(manifest, "building")
        failures = [
            dict(failure)
            for result in results
            for failure in result.get("failures", ())
            if isinstance(failure, Mapping)
        ]
        status = (
            "failed"
            if failures or any(result.get("status") != "success" for result in results)
            else "built"
        )
        updated = {
            **manifest,
            "status": status,
            "build_results": [dict(result) for result in results],
            "build_finished_at": _now(),
            "failures": failures,
        }
        atomic_write_json(layout.manifest_path, updated, mode=0o600)
        return updated

    def record_failure(
        self,
        layout: CandidateReleaseLayout,
        error: BaseException,
    ) -> dict[str, Any]:
        manifest = self.load(layout)
        updated = {
            **manifest,
            "status": "failed",
            "build_finished_at": _now(),
            "failures": [
                {
                    "error_type": type(error).__name__,
                    "error": str(error)[:2000],
                }
            ],
        }
        atomic_write_json(layout.manifest_path, updated, mode=0o600)
        return updated

    def validate(
        self,
        layout: CandidateReleaseLayout,
        *,
        qdrant_client: Any,
    ) -> dict[str, Any]:
        manifest = self.load(layout)
        if manifest.get("status") not in {"built", "validated"}:
            raise ValueError("candidate release must be built or validated before validation")
        validator = HubValidator(
            layout.root,
            layout.root,
            rag_dirs={layout.knowledge_base: layout.rag_data_dir},
        )
        normalized = validator.normalized()
        index = validator.index(
            layout.knowledge_base,
            qdrant_client=qdrant_client,
            collection=layout.collection,
        )
        checksums = self._checksums(layout)
        identity = self.normalized_identity(layout.normalized_root)
        expected_count = manifest.get("build_scope", {}).get("expected_documents")
        expected_fingerprint = manifest.get("build_scope", {}).get(
            "source_document_fingerprint"
        )
        scope_valid = bool(
            (expected_count is None or identity["count"] == expected_count)
            and (
                expected_fingerprint is None
                or identity["fingerprint"] == expected_fingerprint
            )
        )
        valid = bool(
            normalized["valid"] and index["valid"] and checksums and scope_valid
        )
        updated = {
            **manifest,
            "status": "validated" if valid else "invalid",
            "validated_at": _now(),
            "validation": {
                "valid": valid,
                "normalized": normalized,
                "index": index,
                "scope": {
                    **identity,
                    "expected_count": expected_count,
                    "expected_fingerprint": expected_fingerprint,
                    "valid": scope_valid,
                },
            },
            "artifacts": checksums,
            "artifact_fingerprint": sha256_json(checksums),
        }
        atomic_write_json(layout.manifest_path, updated, mode=0o600)
        return updated

    def copy_active_artifacts(
        self,
        layout: CandidateReleaseLayout,
        *,
        normalized_root: Path,
        rag_data_dir: Path,
    ) -> None:
        manifest = self.load(layout)
        self._require_status(manifest, "building")
        if not normalized_root.is_dir() or not rag_data_dir.is_dir():
            raise ValueError("active normalized or RAG artifact root is missing")
        shutil.copytree(normalized_root, layout.normalized_root, dirs_exist_ok=True)
        shutil.copytree(rag_data_dir, layout.rag_data_dir, dirs_exist_ok=True)

    @staticmethod
    def normalized_identity(root: Path) -> dict[str, Any]:
        document_ids: set[str] = set()
        duplicates: set[str] = set()
        for path in sorted(root.glob("**/*.jsonl")):
            if path.parent == root and (path.parent / path.stem).is_dir():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                document_id = str(value["document_id"])
                if document_id in document_ids:
                    duplicates.add(document_id)
                document_ids.add(document_id)
        if duplicates:
            raise ValueError("active normalized manifests contain duplicate document IDs")
        ordered = sorted(document_ids)
        return {
            "count": len(ordered),
            "fingerprint": sha256_json(ordered),
        }

    def verify_validated(self, manifest_path: Path) -> dict[str, Any]:
        selected = ensure_path_within(manifest_path, self.root)
        manifest = json.loads(selected.read_text(encoding="utf-8"))
        layout = self.layout(
            str(manifest.get("knowledge_base") or ""),
            str(manifest.get("collection") or ""),
        )
        if selected.resolve() != layout.manifest_path.resolve():
            raise ValueError("release manifest path does not match its collection")
        self._require_status(manifest, "validated")
        if not manifest.get("promotion_eligible"):
            raise ValueError("candidate release is scoped and not promotion eligible")
        checksums = self._checksums(layout)
        if manifest.get("artifact_fingerprint") != sha256_json(checksums):
            raise ValueError("candidate release artifacts changed after validation")
        return {**manifest, "manifest_path": str(selected)}

    @staticmethod
    def load(layout: CandidateReleaseLayout) -> dict[str, Any]:
        if not layout.manifest_path.is_file():
            raise FileNotFoundError(
                f"candidate release manifest is missing: {layout.manifest_path}"
            )
        raw = json.loads(layout.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("candidate release manifest must be an object")
        value: dict[str, Any] = raw
        if value.get("schema_name") != "candidate_index_release":
            raise ValueError("invalid candidate release manifest")
        if value.get("collection") != layout.collection:
            raise ValueError("candidate release collection mismatch")
        if value.get("knowledge_base") != layout.knowledge_base:
            raise ValueError("candidate release knowledge base mismatch")
        if value.get("normalized_root") != str(layout.normalized_root):
            raise ValueError("candidate release normalized root mismatch")
        if value.get("rag_data_dir") != str(layout.rag_data_dir):
            raise ValueError("candidate release RAG data directory mismatch")
        return value

    @staticmethod
    def _require_status(manifest: Mapping[str, Any], expected: str) -> None:
        if manifest.get("status") != expected:
            raise ValueError(
                f"candidate release must be {expected}, got {manifest.get('status') or 'unknown'}"
            )

    @staticmethod
    def _checksums(layout: CandidateReleaseLayout) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for root in (layout.normalized_root, layout.rag_data_dir):
            if not root.is_dir():
                continue
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                if path.is_symlink():
                    raise ValueError(f"candidate release artifact must not be a symlink: {path}")
                values.append(
                    {
                        "path": path.relative_to(layout.root).as_posix(),
                        "sha256": sha256_file(path),
                        "size": path.stat().st_size,
                    }
                )
        return values
