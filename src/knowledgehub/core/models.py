"""Domain-neutral documents used by non-Zotero KnowledgeHub pipelines."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, slots=True, kw_only=True)
class KnowledgeDocument:
    document_id: str
    knowledge_base: str
    source_type: str
    title: str
    content_hash: str
    source_url: str
    retrieved_at: str
    content: str | None = None
    content_path: Path | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> "KnowledgeDocument":
        if self.knowledge_base not in {"literature", "code", "writing"}:
            raise ValueError("invalid knowledge_base")
        if not all(
            (self.document_id, self.source_type, self.content_hash, self.retrieved_at)
        ):
            raise ValueError("knowledge document has empty required fields")
        if len(self.content_hash) != 64:
            raise ValueError("content_hash must be a SHA-256 digest")
        if (self.content is None) == (self.content_path is None):
            raise ValueError("exactly one of content or content_path is required")
        return self

    def read_content(self) -> str:
        if self.content is not None:
            return self.content
        assert self.content_path is not None
        return self.content_path.read_text(encoding="utf-8", errors="replace")

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        result = {
            "content_hash": self.content_hash,
            "content_path": str(self.content_path) if self.content_path else None,
            "document_id": self.document_id,
            "knowledge_base": self.knowledge_base,
            "metadata": dict(self.metadata),
            "retrieved_at": self.retrieved_at,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "title": self.title,
        }
        if include_content:
            result["content"] = self.content
        return result
