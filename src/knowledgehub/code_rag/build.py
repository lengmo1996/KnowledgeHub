"""Normalize synchronized official sources and build the Code collection."""

from __future__ import annotations

import fnmatch
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from knowledgehub.code_rag.chunking import CodeChunker, source_type_for_path
from knowledgehub.code_rag.registry import CodeLibrary, CodeSourceRegistry
from knowledgehub.core.atomic import atomic_write_jsonl
from knowledgehub.core.hashing import sha256_file, sha256_text
from knowledgehub.core.models import KnowledgeDocument
from knowledgehub.governance.releases import (
    CandidateReleaseLayout,
    CandidateReleaseManager,
)
from knowledgehub.indexing.incremental import IncrementalChunkIndexer, IndexInput
from knowledgehub.pipeline.config import RagConfig


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(Path(path).name, pattern) for pattern in patterns)


class CodeBuildService:
    def __init__(
        self,
        registry: CodeSourceRegistry,
        data_root: Path,
        rag_config: RagConfig,
        *,
        indexer: IncrementalChunkIndexer | None = None,
        candidate_release: CandidateReleaseLayout | None = None,
    ) -> None:
        self.registry = registry
        self.data_root = data_root
        self.rag_config = rag_config
        self.chunker = CodeChunker()
        self.indexer = indexer
        self.candidate_release = candidate_release

    def close(self) -> None:
        if self.indexer is not None:
            self.indexer.close()

    def build(
        self,
        library_name: str,
        *,
        version: str | None = None,
        limit: int | None = None,
        dry_run: bool = False,
        prune: bool = False,
    ) -> dict[str, Any]:
        if prune and (version is not None or limit is not None):
            raise ValueError("prune requires a complete all-version Code build")
        if not dry_run:
            self._validate_candidate_release()
        library = self.registry.get(library_name)
        markers = self._markers(library, version)
        inputs: list[IndexInput] = []
        normalized: list[dict[str, Any]] = []
        truncated_files = 0
        for marker in markers:
            value = json.loads(marker.read_text(encoding="utf-8"))
            source_root = Path(value["source_path"])
            license_value = self._license(source_root)
            count = 0
            for path in sorted(item for item in source_root.rglob("*") if item.is_file()):
                if limit is not None and len(inputs) >= limit:
                    break
                relative = path.relative_to(source_root).as_posix()
                if not _matches(relative, library.include) or _matches(relative, library.exclude):
                    continue
                if path.stat().st_size > library.max_file_bytes:
                    continue
                if path.suffix.lower() not in {".py", ".md", ".mdx", ".rst", ".txt"} and not path.name.lower().startswith(("readme", "changelog", "release", "migration")):
                    continue
                count += 1
                if count > library.max_files:
                    break
                document = self._file_document(library, value, path, relative, license_value)
                chunks = self.chunker.chunk(document)
                if len(chunks) > library.max_chunks_per_file:
                    chunks = chunks[: library.max_chunks_per_file]
                    truncated_files += 1
                if chunks:
                    inputs.append(IndexInput(document, chunks, self.chunker.version))
                    normalized.append(document.to_dict(include_content=False))
            if limit is not None and len(inputs) >= limit:
                break
        if limit is None or len(inputs) < limit:
            releases = self._release_inputs(library, version)
            selected_releases = releases[
                : None if limit is None else max(0, limit - len(inputs))
            ]
            inputs.extend(selected_releases)
            normalized.extend(
                item.document.to_dict(include_content=False) for item in selected_releases
            )
        normalized_root = (
            self.candidate_release.normalized_root
            if self.candidate_release is not None
            else self.data_root / "normalized"
        )
        normalized_path = normalized_root / library.name / f"{version or 'all-versions'}.jsonl"
        if not dry_run:
            atomic_write_jsonl(normalized_path, normalized, sort_key=lambda item: item["document_id"])
        if self.indexer is None:
            self.indexer = IncrementalChunkIndexer(
                self.rag_config,
                initialize=not dry_run,
                require_new_collection=not dry_run,
            )
        summary = self.indexer.build(
            inputs,
            knowledge_base="code",
            dry_run=dry_run,
            prune=prune,
        )
        result = summary.to_dict()
        result.update(
            {
                "library": library.name,
                "versions": sorted({str(item.document.metadata.get("version")) for item in inputs}),
                "normalized_manifest": str(normalized_path),
                "candidate_release": (
                    str(self.candidate_release.manifest_path)
                    if self.candidate_release is not None
                    else None
                ),
                "truncated_files": truncated_files,
            }
        )
        return result

    def _validate_candidate_release(self) -> None:
        release = self.candidate_release
        if release is None:
            raise RuntimeError(
                "direct Code index writes are disabled; build into a prepared candidate release"
            )
        if self.rag_config.qdrant_collection != release.collection:
            raise ValueError("candidate release collection differs from the RAG configuration")
        if self.rag_config.data_dir.resolve(strict=False) != release.rag_data_dir.resolve(
            strict=False
        ):
            raise ValueError("candidate release must use its isolated RAG data directory")
        if not release.manifest_path.is_file():
            raise ValueError("candidate release must be prepared before building")
        manifest = CandidateReleaseManager.load(release)
        if manifest.get("status") != "building":
            raise ValueError("candidate release is immutable after its build is finalized")

    def _markers(self, library: CodeLibrary, version: str | None) -> list[Path]:
        root = self.data_root / "sources" / "repositories" / library.name
        if version:
            marker = root / version / "current.json"
            if not marker.is_file():
                raise RuntimeError(f"synchronized source is missing: {library.name} {version}")
            return [marker]
        markers = sorted(root.glob("*/current.json"))
        if not markers:
            raise RuntimeError(f"no synchronized source for {library.name}")
        return markers

    def _file_document(
        self,
        library: CodeLibrary,
        marker: dict[str, Any],
        path: Path,
        relative: str,
        license_value: str,
    ) -> KnowledgeDocument:
        source_type = source_type_for_path(relative)
        version = str(marker["version"])
        repository = library.repository
        metadata = {
            "knowledge_base": "code",
            "library": library.name,
            "package": library.package_name,
            "version": version,
            "source_type": source_type,
            "repository": repository,
            "tag": marker["tag"],
            "commit": marker["commit"],
            "path": relative,
            "license": license_value,
            "retrieved_at": marker["retrieved_at"],
            "source": "official_repository",
        }
        url = f"https://github.com/{repository}/blob/{marker['commit']}/{quote(relative)}"
        return KnowledgeDocument(
            document_id=f"code:{repository}@{version}:{relative}",
            knowledge_base="code",
            source_type=source_type,
            title=f"{library.name} {version}: {relative}",
            content_hash=sha256_file(path),
            source_url=url,
            retrieved_at=str(marker["retrieved_at"]),
            content_path=path,
            metadata=metadata,
        ).validate()

    def _release_inputs(self, library: CodeLibrary, version: str | None) -> list[IndexInput]:
        path = self.data_root / "sources" / "releases" / f"{library.name}.json"
        if not path.is_file():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        values: list[IndexInput] = []
        for release in payload.get("releases") or []:
            tag = str(release.get("tag") or "")
            release_version = tag.removeprefix("v")
            if version and release_version != version:
                continue
            text = f"# {release.get('title') or tag}\n\n{release.get('body') or ''}".strip()
            if not text:
                continue
            metadata = {
                "knowledge_base": "code",
                "library": library.name,
                "package": library.package_name,
                "version": release_version,
                "source_type": "release_note",
                "repository": library.repository,
                "tag": tag,
                "commit": "",
                "path": "",
                "release_date": release.get("published_at"),
                "retrieved_at": payload.get("retrieved_at"),
                "source": "github_release_api",
            }
            document = KnowledgeDocument(
                document_id=f"code:{library.repository}@{release_version}:release:{tag}",
                knowledge_base="code",
                source_type="release_note",
                title=str(release.get("title") or tag),
                content_hash=sha256_text(text),
                source_url=str(release.get("url") or ""),
                retrieved_at=str(payload.get("retrieved_at") or datetime.now(timezone.utc).isoformat()),
                content=text,
                metadata=metadata,
            ).validate()
            values.append(IndexInput(document, self.chunker.chunk(document), self.chunker.version))
        return values

    @staticmethod
    def _license(root: Path) -> str:
        for pattern in ("LICENSE*", "COPYING*"):
            matches = sorted(root.glob(pattern))
            if matches:
                return matches[0].name
        return "unknown"
