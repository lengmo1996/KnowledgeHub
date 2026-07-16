"""Bounded official Git and GitHub Release synchronization."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import httpx

from knowledgehub.code_rag.registry import (
    CodeLibrary,
    CodeSourceRegistry,
    resolve_tag,
    select_versions,
)
from knowledgehub.core.atomic import atomic_replace, atomic_write_json
from knowledgehub.core.hashing import sha256_json


@dataclass(frozen=True, slots=True)
class SyncResult:
    library: str
    version: str
    tag: str
    commit: str
    source_path: str
    status: str

    def to_dict(self) -> dict[str, str]:
        return {
            "commit": self.commit,
            "library": self.library,
            "source_path": self.source_path,
            "status": self.status,
            "tag": self.tag,
            "version": self.version,
        }


class CodeSyncService:
    def __init__(
        self,
        registry: CodeSourceRegistry,
        data_root: Path,
        *,
        token_env: str = "GITHUB_TOKEN",
        timeout_seconds: float = 60,
        max_retries: int = 3,
    ) -> None:
        self.registry = registry
        self.data_root = data_root
        self.token_env = token_env
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    def sync(
        self,
        library_name: str,
        *,
        version: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        library = self.registry.get(library_name)
        if dry_run:
            return {
                "status": "planned",
                "dry_run": True,
                "library": library.name,
                "installed_version": library.installed_version(),
                "version": version or "configured strategies",
                "repository": library.repository,
                "releases": library.releases_enabled,
            }
        tags = self._remote_tags(library)
        strategies = ("explicit",) if version and version not in {"installed", "latest", "adjacent"} else (
            (version,) if version else library.version_strategy
        )
        explicit = version if version and version not in {"installed", "latest", "adjacent"} else None
        versions = select_versions(
            installed=library.installed_version(),
            available_tags=tags,
            strategies=strategies,
            explicit=explicit,
        )
        if not versions:
            raise RuntimeError(f"no versions resolved for {library.name}")
        results = [self._sync_version(library, item, resolve_tag(library, item, tags)) for item in versions]
        release_count = 0
        release_error: str | None = None
        if library.releases_enabled:
            try:
                release_count = self._sync_releases(library)
            except httpx.HTTPStatusError as exc:
                release_error = f"github_releases_http_{exc.response.status_code}"
            except httpx.HTTPError as exc:
                release_error = f"github_releases_unavailable:{type(exc).__name__}"
        summary = {
            "schema_version": 1,
            "status": "partial" if release_error else "success",
            "library": library.name,
            "repository": library.repository,
            "results": [item.to_dict() for item in results],
            "release_count": release_count,
            "release_error": release_error,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(self.data_root / "manifests" / f"sync-{library.name}.json", summary)
        return summary

    def _remote_tags(self, library: CodeLibrary) -> list[str]:
        completed = self._run(
            ["git", "ls-remote", "--tags", "--refs", self._repository_url(library)]
        )
        return [line.split()[1].removeprefix("refs/tags/") for line in completed.stdout.splitlines() if len(line.split()) == 2]

    def _sync_version(self, library: CodeLibrary, version: str, tag: str) -> SyncResult:
        source_base = self.data_root / "sources" / "repositories" / library.name / version
        marker = source_base / "current.json"
        source_config_hash = sha256_json(
            {
                "include": library.include,
                "exclude": library.exclude,
                "max_file_bytes": library.max_file_bytes,
                "max_files": library.max_files,
            }
        )
        if marker.is_file():
            current = json.loads(marker.read_text(encoding="utf-8"))
            path = Path(str(current.get("source_path") or ""))
            if (
                current.get("tag") == tag
                and current.get("source_config_hash") == source_config_hash
                and path.is_dir()
            ):
                return SyncResult(library.name, version, tag, str(current["commit"]), str(path), "skipped")
        staging_parent = self.data_root / ".staging"
        staging_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = Path(tempfile.mkdtemp(prefix=f"{library.name}-{version}-", dir=staging_parent))
        checkout = temporary / "checkout"
        try:
            self._run(["git", "init", str(checkout)])
            self._run(["git", "-C", str(checkout), "remote", "add", "origin", self._repository_url(library)])
            self._run(["git", "-C", str(checkout), "config", "core.sparseCheckout", "true"])
            sparse = checkout / ".git" / "info" / "sparse-checkout"
            sparse.parent.mkdir(parents=True, exist_ok=True)
            sparse.write_text("\n".join(library.include) + "\n", encoding="utf-8")
            self._run(["git", "-C", str(checkout), "fetch", "--depth", "1", "origin", f"refs/tags/{tag}"])
            self._run(["git", "-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"])
            commit = self._run(["git", "-C", str(checkout), "rev-parse", "HEAD"]).stdout.strip()
            shutil.rmtree(checkout / ".git")
            destination = source_base / f"{commit}-{source_config_hash[:8]}"
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not destination.exists():
                atomic_replace(checkout, destination)
            marker_value = {
                "library": library.name,
                "repository": library.repository,
                "version": version,
                "tag": tag,
                "commit": commit,
                "source_path": str(destination),
                "source_config_hash": source_config_hash,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            }
            atomic_write_json(marker, marker_value)
            return SyncResult(library.name, version, tag, commit, str(destination), "synced")
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def _sync_releases(self, library: CodeLibrary) -> int:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "KnowledgeHub/0.1"}
        token = os.environ.get(self.token_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"https://api.github.com/repos/{library.repository}/releases"
        response = httpx.get(
            url,
            params={"per_page": min(100, library.release_limit)},
            headers=headers,
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )
        response.raise_for_status()
        values = response.json()
        if not isinstance(values, list):
            raise RuntimeError("GitHub Releases response was not a list")
        records = []
        for value in values[: library.release_limit]:
            records.append(
                {
                    "tag": value.get("tag_name"),
                    "title": value.get("name") or value.get("tag_name"),
                    "published_at": value.get("published_at"),
                    "body": value.get("body") or "",
                    "url": value.get("html_url"),
                    "source": "github_release_api",
                    "repository": library.repository,
                }
            )
        atomic_write_json(
            self.data_root / "sources" / "releases" / f"{library.name}.json",
            {"retrieved_at": datetime.now(timezone.utc).isoformat(), "releases": records},
        )
        return len(records)

    def _run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        last: subprocess.CompletedProcess[str] | None = None
        for attempt in range(self.max_retries):
            last = subprocess.run(
                list(command), check=False, capture_output=True, text=True, timeout=self.timeout_seconds
            )
            if last.returncode == 0:
                return last
            if attempt + 1 < self.max_retries:
                time.sleep(min(2**attempt, 5))
        assert last is not None
        raise RuntimeError(f"command failed: {command[0]}: {last.stderr.strip()[:500]}")

    @staticmethod
    def _repository_url(library: CodeLibrary) -> str:
        value = library.repository
        if "://" in value or Path(value).expanduser().exists():
            return str(Path(value).expanduser().resolve()) if "://" not in value else value
        return f"https://github.com/{value}.git"
