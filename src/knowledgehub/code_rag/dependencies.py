"""Version-pinned dependency manifests for synchronized official libraries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name

from knowledgehub.code_rag.registry import CodeSourceRegistry
from knowledgehub.core.atomic import atomic_write_json
from knowledgehub.core.hashing import sha256_json
from knowledgehub.workflows.repository import RepositoryIntake


class DependencyManifestService:
    def __init__(self, registry: CodeSourceRegistry, data_root: Path) -> None:
        self.registry = registry
        self.data_root = data_root

    def capture(
        self, library_name: str, version: str, *, dry_run: bool = False
    ) -> dict[str, Any]:
        library = self.registry.get(library_name)
        marker_path = (
            self.data_root
            / "sources"
            / "repositories"
            / library.name
            / version
            / "current.json"
        )
        if not marker_path.is_file():
            raise RuntimeError(f"synchronized source is missing: {library.name} {version}")
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(marker, dict):
            raise RuntimeError(f"invalid synchronized source marker: {marker_path}")
        root = Path(str(marker.get("source_path") or ""))
        commit = str(marker.get("commit") or "")
        if not root.is_dir() or len(commit) != 40:
            raise RuntimeError(f"invalid synchronized source marker: {marker_path}")
        package_to_library = {
            canonicalize_name(item.package_name): item.name for item in self.registry.list()
        }
        dependencies = []
        for item in RepositoryIntake(root).dependencies():
            package = str(item["package"])
            source = str(item["source"])
            scope = self._scope(source)
            dependencies.append(
                {
                    **item,
                    "evidence_kind": (
                        "dependency_catalog"
                        if scope == "catalog"
                        else item["evidence_kind"]
                    ),
                    "normalized_package": canonicalize_name(package),
                    "target_library": package_to_library.get(canonicalize_name(package)),
                    "relation": (
                        "lists_dependency"
                        if scope == "catalog"
                        else "declares_dependency"
                    ),
                    "scope": scope,
                    "confidence": 1.0,
                    "inference": False,
                }
            )
        value = {
            "schema_name": "dependency_manifest",
            "schema_version": "2.0",
            "library": library.name,
            "package": library.package_name,
            "version": version,
            "repository": library.repository,
            "tag": marker.get("tag"),
            "commit": commit,
            "retrieved_at": marker.get("retrieved_at"),
            "source_path": str(root),
            "dependency_count": len(dependencies),
            "dependencies": dependencies,
            "content_hash": sha256_json(dependencies),
            "dry_run": dry_run,
        }
        path = (
            self.data_root
            / "manifests"
            / "dependencies"
            / library.name
            / f"{version}.json"
        )
        if not dry_run:
            atomic_write_json(path, value)
        return {
            **value,
            "status": "planned" if dry_run else "success",
            "manifest": str(path),
        }

    @staticmethod
    def _scope(source: str) -> str:
        lowered = source.lower()
        if "setup.py:_deps" in lowered:
            return "catalog"
        if "build-system" in lowered or "build" in lowered:
            return "build"
        if "optional-dependencies" in lowered:
            return "optional"
        if "dependency-groups" in lowered or any(
            value in lowered for value in ("dev", "test", "lint", "docs")
        ):
            return "development"
        return "declared"


__all__ = ["DependencyManifestService"]
