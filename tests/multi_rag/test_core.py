from __future__ import annotations

from pathlib import Path

import pytest

from knowledgehub.code_rag.registry import (
    CodeSourceRegistry,
    resolve_tag,
    select_versions,
)
from knowledgehub.core.hashing import sha256_text
from knowledgehub.core.models import KnowledgeDocument
from knowledgehub.hub.config import HubConfig


def test_hub_config_preserves_isolated_collections() -> None:
    config = HubConfig.load(Path("configs/knowledgehub.yaml"))
    assert config.knowledge_bases["literature"].collection == "zotero_papers_qwen3_4b_1024_v2"
    assert config.knowledge_bases["code"].collection == "knowledgehub_code_qwen3_4b_1024_v1"
    assert config.knowledge_bases["writing"].collection == "knowledgehub_writing_qwen3_4b_1024_v1"
    assert config.writing_materials.classification_max_tokens == 8192
    assert config.writing_materials.abstraction_max_tokens == 8192
    assert config.writing_materials.classification_max_sentences_per_request == 8
    assert config.writing_materials.abstraction_batch_size == 8


def test_knowledge_document_requires_one_content_source(tmp_path: Path) -> None:
    digest = sha256_text("body")
    value = KnowledgeDocument(
        document_id="code:x@1:a.py",
        knowledge_base="code",
        source_type="source_code",
        title="a.py",
        content_hash=digest,
        source_url="https://example.test/a.py",
        retrieved_at="2026-01-01T00:00:00Z",
        content="body",
    )
    assert value.validate().read_content() == "body"
    with pytest.raises(ValueError, match="exactly one"):
        KnowledgeDocument(
            document_id="x",
            knowledge_base="code",
            source_type="source_code",
            title="x",
            content_hash=digest,
            source_url="",
            retrieved_at="now",
        ).validate()


def test_registry_and_adjacent_version_selection() -> None:
    registry = CodeSourceRegistry.load("configs/sources/code.yaml")
    library = registry.get("transformers")
    assert library.enabled is True
    assert library.repository == "huggingface/transformers"
    selected = select_versions(
        installed="5.13.1",
        available_tags=("v5.12.0", "v5.13.1", "v5.14.0", "v6.0.0rc1"),
        strategies=("installed", "adjacent"),
    )
    assert selected == ("5.12.0", "5.13.1", "5.14.0")
    assert resolve_tag(library, "5.13.1", ("v5.13.1",)) == "v5.13.1"
