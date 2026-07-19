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
class WritingMaterialHubConfig:
    data_root: Path
    literature_data_dir: Path
    taxonomy_path: Path
    classify_prompt_path: Path
    abstract_prompt_path: Path
    provider: str = "openai_compatible"
    base_url_env: str = "KH_WRITING_MATERIAL_LLM_BASE_URL"
    api_key_env: str = "KH_WRITING_MATERIAL_LLM_API_KEY"
    model: str = ""
    timeout_seconds: float = 600.0
    max_retries: int = 2
    batch_size: int = 12
    classification_max_sentences_per_request: int = 8
    abstraction_batch_size: int = 8
    classification_max_tokens: int = 8192
    abstraction_max_tokens: int = 8192
    minimum_quality: float = 0.65
    minimum_provenance_coverage: float = 0.80
    enabled_categories: tuple[str, ...] = ()
    allowed_sections: tuple[str, ...] = ("introduction", "experiment", "conclusion")

    def runtime_config(self) -> Any:
        from knowledgehub.writing_rag.extract import WritingMaterialRuntimeConfig

        values: dict[str, Any] = {
            "data_root": self.data_root,
            "literature_data_dir": self.literature_data_dir,
            "taxonomy_path": self.taxonomy_path,
            "classify_prompt_path": self.classify_prompt_path,
            "abstract_prompt_path": self.abstract_prompt_path,
            "provider": self.provider,
            "base_url_env": self.base_url_env,
            "api_key_env": self.api_key_env,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "batch_size": self.batch_size,
            "classification_max_sentences_per_request": (
                self.classification_max_sentences_per_request
            ),
            "abstraction_batch_size": self.abstraction_batch_size,
            "classification_max_tokens": self.classification_max_tokens,
            "abstraction_max_tokens": self.abstraction_max_tokens,
            "minimum_quality": self.minimum_quality,
            "minimum_provenance_coverage": self.minimum_provenance_coverage,
            "allowed_sections": self.allowed_sections,
        }
        if self.enabled_categories:
            values["enabled_categories"] = self.enabled_categories
        return WritingMaterialRuntimeConfig(**values).validate()


@dataclass(frozen=True, slots=True)
class HubConfig:
    path: Path
    base_rag_config: Path
    knowledge_bases: Mapping[str, KnowledgeBaseConfig]
    code: CodeHubConfig
    writing: WritingHubConfig
    writing_materials: WritingMaterialHubConfig

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
        code = CodeHubConfig(
            data_root=resolve(code_raw.get("data_root")),
            registry=resolve(code_raw.get("registry")),
            github_token_env=str(code_raw.get("github_token_env") or "GITHUB_TOKEN"),
            timeout_seconds=float(code_raw.get("timeout_seconds", 60)),
            max_retries=int(code_raw.get("max_retries", 3)),
        )
        writing = WritingHubConfig(
            data_root=resolve(writing_raw.get("data_root")),
            literature_data_dir=resolve(writing_raw.get("literature_data_dir")),
            analyzer=str(writing_raw.get("analyzer") or "rules"),
            processor_version=str(writing_raw.get("processor_version") or "rules-v2"),
            default_limit=int(writing_raw.get("default_limit", 5)),
            minimum_quality=float(writing_raw.get("minimum_quality", 0.45)),
        )
        materials_raw = raw.get("writing_materials") or {}
        if not isinstance(materials_raw, Mapping):
            raise RagConfigError("writing_materials configuration must be a mapping")
        materials_root = root
        if materials_raw.get("config"):
            materials_path = resolve(materials_raw["config"])
            loaded = yaml.safe_load(materials_path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, Mapping) or int(loaded.get("schema_version", 0)) != 1:
                raise RagConfigError("unsupported writing-material configuration")
            materials_raw = loaded
            materials_root = materials_path.parent

        def resolve_material(value: Any, default: Path) -> Path:
            if value in (None, ""):
                return default
            candidate = Path(str(value)).expanduser()
            return candidate if candidate.is_absolute() else (materials_root / candidate).resolve()

        prompts = materials_raw.get("prompts") or {}
        if not isinstance(prompts, Mapping):
            raise RagConfigError("writing-material prompts must be a mapping")
        writing_materials = WritingMaterialHubConfig(
            data_root=resolve_material(
                materials_raw.get("data_root"), writing.data_root / "materials"
            ),
            literature_data_dir=resolve_material(
                materials_raw.get("literature_data_dir"), writing.literature_data_dir
            ),
            taxonomy_path=resolve_material(
                materials_raw.get("taxonomy_path"), root / "writing" / "taxonomy-v1.yaml"
            ),
            classify_prompt_path=resolve_material(
                prompts.get("classify"), root / "writing" / "prompts" / "classify-v9.md"
            ),
            abstract_prompt_path=resolve_material(
                prompts.get("abstract"), root / "writing" / "prompts" / "abstract-v7.md"
            ),
            provider=str(materials_raw.get("provider") or "openai_compatible"),
            base_url_env=str(
                materials_raw.get("base_url_env") or "KH_WRITING_MATERIAL_LLM_BASE_URL"
            ),
            api_key_env=str(materials_raw.get("api_key_env") or "KH_WRITING_MATERIAL_LLM_API_KEY"),
            model=str(materials_raw.get("model") or ""),
            timeout_seconds=float(materials_raw.get("timeout_seconds", 600)),
            max_retries=int(materials_raw.get("max_retries", 2)),
            batch_size=int(materials_raw.get("batch_size", 12)),
            classification_max_sentences_per_request=int(
                materials_raw.get("classification_max_sentences_per_request", 8)
            ),
            abstraction_batch_size=int(materials_raw.get("abstraction_batch_size", 8)),
            classification_max_tokens=int(materials_raw.get("classification_max_tokens", 8192)),
            abstraction_max_tokens=int(materials_raw.get("abstraction_max_tokens", 8192)),
            minimum_quality=float(materials_raw.get("minimum_quality", 0.65)),
            minimum_provenance_coverage=float(
                materials_raw.get("minimum_provenance_coverage", 0.80)
            ),
            enabled_categories=tuple(
                str(value) for value in materials_raw.get("enabled_categories") or ()
            ),
            allowed_sections=tuple(
                str(value)
                for value in materials_raw.get("allowed_sections")
                or ("introduction", "experiment", "conclusion")
            ),
        )
        return cls(
            path=selected,
            base_rag_config=resolve(raw.get("base_rag_config")),
            knowledge_bases=bases,
            code=code,
            writing=writing,
            writing_materials=writing_materials,
        )

    def rag_config(self, knowledge_base: str) -> RagConfig:
        try:
            selected = self.knowledge_bases[knowledge_base]
        except KeyError as exc:
            raise RagConfigError(f"unknown knowledge base: {knowledge_base}") from exc
        collection = selected.collection
        index_root = Path(os.environ.get("KH_INDEX_ROOT", "/data/KnowledgeHub/indexes"))
        from knowledgehub.governance.snapshots import (
            active_collection,
            active_release_data_dir,
        )

        collection = active_collection(index_root, knowledge_base, collection)
        data_dir = active_release_data_dir(index_root, knowledge_base, selected.data_dir)
        return RagConfig.load(self.base_rag_config).with_overrides(
            data_dir=data_dir,
            qdrant_collection=collection,
            embedding_query_instruction=selected.query_instruction,
        )

    def normalized_root(self, knowledge_base: str) -> Path:
        if knowledge_base != "code":
            raise RagConfigError("normalized release roots are implemented only for code")
        index_root = Path(os.environ.get("KH_INDEX_ROOT", "/data/KnowledgeHub/indexes"))
        from knowledgehub.governance.snapshots import active_release_normalized_root

        return active_release_normalized_root(
            index_root,
            knowledge_base,
            self.code.data_root / "normalized",
        )
