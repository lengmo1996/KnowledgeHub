"""Configuration catalog for logically isolated knowledge bases."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from knowledgehub.pipeline.config import RagConfig, RagConfigError


@dataclass(frozen=True, slots=True)
class KnowledgeBaseConfig:
    name: str
    data_dir: Path
    collection: str
    query_instruction: str


@dataclass(frozen=True, slots=True)
class CodeHubConfig:
    data_root: Path
    registry: Path
    github_token_env: str = "GITHUB_TOKEN"
    timeout_seconds: float = 60.0
    max_retries: int = 3


@dataclass(frozen=True, slots=True)
class WritingHubConfig:
    data_root: Path
    literature_data_dir: Path
    analyzer: str = "rules"
    processor_version: str = "rules-v2"
    default_limit: int = 5
    minimum_quality: float = 0.45


@dataclass(frozen=True, slots=True)
class HubConfig:
    path: Path
    base_rag_config: Path
    knowledge_bases: Mapping[str, KnowledgeBaseConfig]
    code: CodeHubConfig
    writing: WritingHubConfig

    @classmethod
    def load(cls, path: Path | str = Path("configs/knowledgehub.yaml")) -> "HubConfig":
        selected = Path(path).expanduser().resolve(strict=True)
        raw = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, Mapping) or int(raw.get("schema_version", 0)) != 1:
            raise RagConfigError("unsupported KnowledgeHub configuration")
        root = selected.parent

        def resolve(value: Any) -> Path:
            candidate = Path(str(value)).expanduser()
            return candidate if candidate.is_absolute() else (root / candidate).resolve()

        kb_raw = raw.get("knowledge_bases")
        if not isinstance(kb_raw, Mapping):
            raise RagConfigError("knowledge_bases must be a mapping")
        bases: dict[str, KnowledgeBaseConfig] = {}
        for name in ("literature", "code", "writing"):
            item = kb_raw.get(name)
            if not isinstance(item, Mapping):
                raise RagConfigError(f"missing knowledge base: {name}")
            collection = str(item.get("collection") or "").strip()
            if not collection:
                raise RagConfigError(f"empty collection for knowledge base: {name}")
            bases[name] = KnowledgeBaseConfig(
                name=name,
                data_dir=resolve(item.get("data_dir")),
                collection=collection,
                query_instruction=str(item.get("query_instruction") or "").strip(),
            )
        code_raw = raw.get("code") or {}
        writing_raw = raw.get("writing") or {}
        if not isinstance(code_raw, Mapping) or not isinstance(writing_raw, Mapping):
            raise RagConfigError("code and writing configuration must be mappings")
        return cls(
            path=selected,
            base_rag_config=resolve(raw.get("base_rag_config")),
            knowledge_bases=bases,
            code=CodeHubConfig(
                data_root=resolve(code_raw.get("data_root")),
                registry=resolve(code_raw.get("registry")),
                github_token_env=str(code_raw.get("github_token_env") or "GITHUB_TOKEN"),
                timeout_seconds=float(code_raw.get("timeout_seconds", 60)),
                max_retries=int(code_raw.get("max_retries", 3)),
            ),
            writing=WritingHubConfig(
                data_root=resolve(writing_raw.get("data_root")),
                literature_data_dir=resolve(writing_raw.get("literature_data_dir")),
                analyzer=str(writing_raw.get("analyzer") or "rules"),
                processor_version=str(writing_raw.get("processor_version") or "rules-v2"),
                default_limit=int(writing_raw.get("default_limit", 5)),
                minimum_quality=float(writing_raw.get("minimum_quality", 0.45)),
            ),
        )

    def rag_config(self, knowledge_base: str) -> RagConfig:
        try:
            selected = self.knowledge_bases[knowledge_base]
        except KeyError as exc:
            raise RagConfigError(f"unknown knowledge base: {knowledge_base}") from exc
        collection = selected.collection
        index_root = Path(os.environ.get("KH_INDEX_ROOT", "/data/KnowledgeHub/indexes"))
        from knowledgehub.governance.snapshots import active_collection

        collection = active_collection(index_root, knowledge_base, collection)
        return RagConfig.load(self.base_rag_config).with_overrides(
            data_dir=selected.data_dir,
            qdrant_collection=collection,
            embedding_query_instruction=selected.query_instruction,
        )
