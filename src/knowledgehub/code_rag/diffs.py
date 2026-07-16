"""Bounded source-diff documents derived from pinned symbol catalogs."""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from knowledgehub.code_rag.chunking import CodeChunker
from knowledgehub.code_rag.registry import CodeSourceRegistry
from knowledgehub.code_rag.symbols import SymbolIndex
from knowledgehub.code_rag.version_diff import compare_symbols
from knowledgehub.core.atomic import atomic_write_jsonl
from knowledgehub.core.hashing import sha256_text
from knowledgehub.core.models import KnowledgeDocument
from knowledgehub.indexing.incremental import IncrementalChunkIndexer, IndexInput
from knowledgehub.pipeline.config import RagConfig


class VersionDiffBuildService:
    processor_version = "code-version-diff-v1"

    def __init__(
        self,
        registry: CodeSourceRegistry,
        data_root: Path,
        rag_config: RagConfig,
        *,
        catalog: SymbolIndex | None = None,
        indexer: IncrementalChunkIndexer | None = None,
    ) -> None:
        self.registry = registry
        self.data_root = data_root
        self.rag_config = rag_config
        self.catalog = catalog or SymbolIndex(
            data_root / "state" / "symbols.sqlite3", read_only=True
        )
        self.indexer = indexer
        self.chunker = CodeChunker()

    def close(self) -> None:
        if self.indexer is not None:
            self.indexer.close()

    def build(
        self,
        library_name: str,
        from_version: str,
        to_version: str,
        *,
        symbols: Sequence[str] = (),
        limit: int = 20,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if from_version == to_version:
            raise ValueError("from_version and to_version must differ")
        if limit <= 0:
            raise ValueError("limit must be positive")
        library = self.registry.get(library_name)
        old_marker = self._marker(library_name, from_version)
        new_marker = self._marker(library_name, to_version)
        old_root = Path(str(old_marker["source_path"]))
        new_root = Path(str(new_marker["source_path"]))
        pairs = self._pairs(
            library_name,
            from_version,
            to_version,
            symbols=symbols,
            limit=limit,
        )
        inputs: list[IndexInput] = []
        normalized: list[dict[str, Any]] = []
        statuses: dict[str, int] = {}
        for old, new in pairs:
            comparison = compare_symbols(old, new)
            if comparison["status"] == "unchanged":
                continue
            document = self._document(
                library.name,
                library.package_name,
                library.repository,
                from_version,
                to_version,
                old_marker,
                new_marker,
                old_root,
                new_root,
                old,
                new,
                comparison,
            )
            chunks = self.chunker.chunk(document)
            if not chunks:
                continue
            inputs.append(IndexInput(document, chunks, self.processor_version))
            normalized.append(document.to_dict(include_content=False))
            status = str(comparison["status"])
            statuses[status] = statuses.get(status, 0) + 1
        normalized_path = (
            self.data_root
            / "normalized"
            / "version_diffs"
            / library.name
            / f"{from_version}--{to_version}.jsonl"
        )
        if not dry_run:
            atomic_write_jsonl(
                normalized_path,
                normalized,
                sort_key=lambda item: str(item["document_id"]),
            )
        indexer = self.indexer or IncrementalChunkIndexer(
            self.rag_config, initialize=not dry_run
        )
        summary = indexer.build(
            inputs,
            knowledge_base="code",
            dry_run=dry_run,
            prune=False,
        )
        result = summary.to_dict()
        result.update(
            {
                "library": library.name,
                "from_version": from_version,
                "to_version": to_version,
                "diff_documents": len(inputs),
                "change_statuses": statuses,
                "normalized_manifest": str(normalized_path),
                "processor_version": self.processor_version,
            }
        )
        return result

    def _pairs(
        self,
        library: str,
        from_version: str,
        to_version: str,
        *,
        symbols: Sequence[str],
        limit: int,
    ) -> list[tuple[dict[str, Any] | None, dict[str, Any] | None]]:
        if symbols:
            result = []
            for symbol in dict.fromkeys(symbols):
                old = self.catalog.inspect(library, from_version, symbol)
                new = self.catalog.inspect(library, to_version, symbol)
                if old is None and new is None:
                    raise ValueError(f"symbol not found in either version: {symbol}")
                result.append((old, new))
                if len(result) >= limit:
                    break
            return result
        return list(
            self.catalog.changed_pairs(
                library,
                from_version,
                to_version,
                limit=limit,
            )
        )

    def _marker(self, library: str, version: str) -> dict[str, Any]:
        path = (
            self.data_root
            / "sources"
            / "repositories"
            / library
            / version
            / "current.json"
        )
        if not path.is_file():
            raise RuntimeError(f"synchronized source is missing: {library} {version}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise RuntimeError(f"invalid synchronized source marker: {path}")
        source = Path(str(value.get("source_path") or ""))
        if not source.is_dir() or len(str(value.get("commit") or "")) != 40:
            raise RuntimeError(f"invalid synchronized source marker: {path}")
        return dict(value)

    def _document(
        self,
        library: str,
        package: str,
        repository: str,
        from_version: str,
        to_version: str,
        old_marker: Mapping[str, Any],
        new_marker: Mapping[str, Any],
        old_root: Path,
        new_root: Path,
        old: Mapping[str, Any] | None,
        new: Mapping[str, Any] | None,
        comparison: Mapping[str, Any],
    ) -> KnowledgeDocument:
        selected = new or old
        assert selected is not None
        symbol = str(selected["qualified_name"])
        old_text = self._symbol_text(old_root, old)
        new_text = self._symbol_text(new_root, new)
        old_path = str(old.get("path") or "") if old else ""
        new_path = str(new.get("path") or "") if new else ""
        patch = "\n".join(
            difflib.unified_diff(
                old_text.splitlines(),
                new_text.splitlines(),
                fromfile=f"{from_version}/{old_path or symbol}",
                tofile=f"{to_version}/{new_path or symbol}",
                lineterm="",
                n=3,
            )
        )[:30_000]
        status = str(comparison["status"])
        changes = comparison.get("changes") or {}
        summary = self._summary(symbol, from_version, to_version, status, changes)
        content = (
            f"# {symbol}\n\n"
            f"{summary}\n\n"
            f"## Structured changes\n\n```json\n"
            f"{json.dumps(changes, ensure_ascii=False, sort_keys=True)}\n```\n\n"
            f"## Source patch\n\n```diff\n{patch or '(no textual patch available)'}\n```"
        )
        old_commit = str(old_marker["commit"])
        new_commit = str(new_marker["commit"])
        compare_url = (
            f"https://github.com/{repository}/compare/{old_commit}...{new_commit}"
        )
        retrieved_at = str(new_marker.get("retrieved_at") or old_marker["retrieved_at"])
        metadata = {
            "knowledge_base": "code",
            "library": library,
            "package": package,
            "version": to_version,
            "from_version": from_version,
            "to_version": to_version,
            "source_type": "version_diff",
            "repository": repository,
            "tag": new_marker.get("tag"),
            "from_commit": old_commit,
            "to_commit": new_commit,
            "commit": new_commit,
            "path": new_path or old_path,
            "old_path": old_path,
            "new_path": new_path,
            "symbol": symbol,
            "symbol_type": selected.get("symbol_type"),
            "old_start_line": old.get("start_line") if old else None,
            "old_end_line": old.get("end_line") if old else None,
            "new_start_line": new.get("start_line") if new else None,
            "new_end_line": new.get("end_line") if new else None,
            "change_type": status,
            "changes": changes,
            "confidence": comparison.get("confidence"),
            "evidence_role": "system_derived_source_diff",
            "inference": False,
            "related_release_notes": self._release_urls(library, from_version, to_version),
            "retrieved_at": retrieved_at,
            "source": "pinned_source_diff",
        }
        identity = sha256_text(
            f"{repository}\0{from_version}\0{to_version}\0{symbol}\0{old_commit}\0{new_commit}"
        )[:24]
        return KnowledgeDocument(
            document_id=f"code-diff:{repository}@{from_version}..{to_version}:{identity}",
            knowledge_base="code",
            source_type="version_diff",
            title=f"{library} {from_version} -> {to_version}: {symbol}",
            content_hash=sha256_text(content),
            source_url=compare_url,
            retrieved_at=retrieved_at,
            content=content,
            metadata=metadata,
        ).validate()

    @staticmethod
    def _symbol_text(root: Path, value: Mapping[str, Any] | None) -> str:
        if value is None:
            return ""
        path = (root / str(value["path"])).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("symbol path escapes synchronized source") from exc
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(0, int(value["start_line"]) - 1)
        end = min(len(lines), int(value["end_line"]), start + 500)
        return "\n".join(lines[start:end])[:30_000]

    def _release_urls(self, library: str, *versions: str) -> list[str]:
        path = self.data_root / "sources" / "releases" / f"{library}.json"
        if not path.is_file():
            return []
        try:
            releases = json.loads(path.read_text(encoding="utf-8")).get("releases") or []
        except (OSError, json.JSONDecodeError):
            return []
        selected = {value.removeprefix("v") for value in versions}
        return sorted(
            {
                str(item["url"])
                for item in releases
                if str(item.get("tag") or "").removeprefix("v") in selected
                and item.get("url")
            }
        )

    @staticmethod
    def _summary(
        symbol: str,
        from_version: str,
        to_version: str,
        status: str,
        changes: Mapping[str, Any],
    ) -> str:
        details: list[str] = []
        for key in ("added_parameters", "removed_parameters", "default_changes"):
            values = changes.get(key)
            if values:
                details.append(f"{key}={json.dumps(values, ensure_ascii=False)}")
        suffix = f" Confirmed details: {'; '.join(details)}." if details else ""
        return (
            f"Pinned source comparison classifies `{symbol}` as `{status}` between "
            f"{from_version} and {to_version}.{suffix}"
        )


__all__ = ["VersionDiffBuildService"]
