"""Explicit on-demand imports and release watching, separate from scheduling."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from knowledgehub.code_rag.build import CodeBuildService
from knowledgehub.code_rag.registry import CodeSourceRegistry, version_from_tag
from knowledgehub.code_rag.sync import CodeSyncService
from knowledgehub.core.atomic import atomic_write_json
from knowledgehub.hub.config import HubConfig


class OnDemandVersionImporter:
    def __init__(self, config: HubConfig, registry: CodeSourceRegistry) -> None:
        self.config = config
        self.registry = registry

    def import_version(
        self,
        library: str,
        version: str,
        *,
        allowed: bool,
        build_limit: int = 20,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        marker = (
            self.config.code.data_root
            / "sources"
            / "repositories"
            / library
            / version.removeprefix("v")
            / "current.json"
        )
        if marker.is_file():
            return {"status": "available", "library": library, "version": version, "marker": str(marker)}
        if not allowed:
            return {
                "status": "permission_required",
                "library": library,
                "version": version,
                "would_sync": True,
                "would_build_limit": build_limit,
            }
        sync = CodeSyncService(
            self.registry,
            self.config.code.data_root,
            token_env=self.config.code.github_token_env,
            timeout_seconds=self.config.code.timeout_seconds,
            max_retries=self.config.code.max_retries,
        ).sync(library, version=version, dry_run=dry_run)
        if dry_run:
            return {"status": "planned", "sync": sync, "build_limit": build_limit}
        build_service = CodeBuildService(
            self.registry, self.config.code.data_root, self.config.rag_config("code")
        )
        try:
            build = build_service.build(
                library, version=version.removeprefix("v"), limit=build_limit
            )
        finally:
            build_service.close()
        return {"status": "completed" if build["status"] == "success" else "partial", "sync": sync, "build": build}


class ReleaseWatchService:
    def __init__(self, config: HubConfig, registry: CodeSourceRegistry) -> None:
        self.config = config
        self.registry = registry

    def check(self, library_name: str, *, dry_run: bool = False) -> dict[str, Any]:
        library = self.registry.get(library_name)
        sync = CodeSyncService(
            self.registry,
            self.config.code.data_root,
            token_env=self.config.code.github_token_env,
            timeout_seconds=self.config.code.timeout_seconds,
            max_retries=self.config.code.max_retries,
        )
        tags = sync._remote_tags(library)
        stable = sorted(
            (value, tag)
            for tag in tags
            if (value := version_from_tag(tag)) is not None
        )
        latest_version, latest_tag = stable[-1] if stable else (None, None)
        state_path = self.config.code.data_root / "state" / "release-watch" / f"{library_name}.json"
        previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        changed = bool(latest_tag and latest_tag != previous.get("latest_tag"))
        relevant = library.installed_version() is not None
        result = {
            "schema_name": "release_watch",
            "schema_version": "2.0",
            "library": library_name,
            "latest_version": str(latest_version) if latest_version else None,
            "latest_tag": latest_tag,
            "previous_tag": previous.get("latest_tag"),
            "new_release": changed,
            "environment_relevant": relevant,
            "breaking_change": "unknown",
            "action": "notify" if changed else "none",
            "auto_downloaded": False,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        if not dry_run:
            atomic_write_json(state_path, result)
        return result
