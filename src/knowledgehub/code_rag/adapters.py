"""Lightweight library layout adapters; transport and indexing stay shared."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from knowledgehub.code_rag.versioning import NormalizedVersion


class LibraryAdapter(Protocol):
    name: str

    def normalize_version(self, value: str) -> NormalizedVersion: ...
    def discover_docs(self, root: Path) -> tuple[Path, ...]: ...
    def discover_source(self, root: Path) -> tuple[Path, ...]: ...
    def discover_releases(self, root: Path) -> tuple[Path, ...]: ...


@dataclass(frozen=True, slots=True)
class GenericPythonAdapter:
    name: str
    source_roots: tuple[str, ...] = ("src",)
    doc_roots: tuple[str, ...] = ("docs", "examples")

    def normalize_version(self, value: str) -> NormalizedVersion:
        return NormalizedVersion.parse(value)

    def discover_docs(self, root: Path) -> tuple[Path, ...]:
        return self._files(root, self.doc_roots, {".md", ".mdx", ".rst"})

    def discover_source(self, root: Path) -> tuple[Path, ...]:
        return self._files(root, self.source_roots, {".py"})

    def discover_releases(self, root: Path) -> tuple[Path, ...]:
        return tuple(sorted(path for pattern in ("CHANGELOG*", "RELEASE*", "MIGRATION*") for path in root.glob(pattern) if path.is_file()))

    @staticmethod
    def _files(root: Path, roots: tuple[str, ...], suffixes: set[str]) -> tuple[Path, ...]:
        result: list[Path] = []
        for relative in roots:
            candidate = root / relative
            if candidate.is_dir():
                result.extend(path for path in candidate.rglob("*") if path.is_file() and path.suffix.lower() in suffixes)
        return tuple(sorted(result))


def adapter_for(name: str) -> GenericPythonAdapter:
    layouts = {
        "pytorch": GenericPythonAdapter("pytorch", ("torch",), ("docs", "examples")),
        "transformers": GenericPythonAdapter("transformers", ("src/transformers",), ("docs", "examples")),
        "diffusers": GenericPythonAdapter("diffusers", ("src/diffusers",), ("docs", "examples")),
        "accelerate": GenericPythonAdapter("accelerate", ("src/accelerate",), ("docs", "examples")),
        "lightning": GenericPythonAdapter("lightning", ("src/lightning", "src/pytorch_lightning"), ("docs", "examples")),
    }
    try:
        return layouts[name]
    except KeyError as exc:
        raise ValueError(f"no V2 adapter for library: {name}") from exc
