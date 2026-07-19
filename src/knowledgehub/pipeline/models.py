"""Stable in-process models for source documents and derived chunks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceDocument:
    source: str
    document_id: str
    attachment_key: str
    source_document_fingerprint: str
    source_metadata_fingerprint: str
    source_content_fingerprint: str
    source_status: str
    pdf_path: Path
    pdf_sha256: str
    title: str = ""
    doi: str = ""
    year: int | None = None
    tags: tuple[str, ...] = ()
    collection_keys: tuple[str, ...] = ()
    collection_paths: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_snapshot(cls, value: Mapping[str, Any]) -> "SourceDocument":
        attachment = value.get("attachment")
        if not isinstance(attachment, Mapping):
            raise ValueError("snapshot attachment must be an object")
        status = str(value.get("status") or "")
        pdf_path = attachment.get("pdf_path")
        pdf_sha256 = attachment.get("pdf_sha256")
        content = value.get("content_fingerprint")
        if status != "ready" or not pdf_path or not pdf_sha256 or not content:
            raise ValueError("source document is not ready")
        collections = value.get("collections") or []
        if not isinstance(collections, list):
            raise ValueError("snapshot collections must be a list")
        return cls(
            source=str(value.get("source") or ""),
            document_id=str(value.get("document_id") or ""),
            attachment_key=str(value.get("attachment_key") or ""),
            source_document_fingerprint=str(value.get("document_fingerprint") or ""),
            source_metadata_fingerprint=str(value.get("metadata_fingerprint") or ""),
            source_content_fingerprint=str(content),
            source_status=status,
            pdf_path=Path(str(pdf_path)),
            pdf_sha256=str(pdf_sha256),
            title=str(value.get("title") or ""),
            doi=str(value.get("doi") or ""),
            year=int(value["year"]) if isinstance(value.get("year"), int) else None,
            tags=tuple(sorted({str(tag) for tag in value.get("tags") or []})),
            collection_keys=tuple(str(item.get("key") or "") for item in collections),
            collection_paths=tuple(str(item.get("path") or "") for item in collections),
            metadata=dict(value),
        ).validate()

    def validate(self) -> "SourceDocument":
        required = (
            self.source,
            self.document_id,
            self.attachment_key,
            self.source_document_fingerprint,
            self.source_metadata_fingerprint,
            self.pdf_sha256,
        )
        if any(not value for value in required):
            raise ValueError("source document has empty required fields")
        if len(self.pdf_sha256) != 64:
            raise ValueError("invalid PDF SHA-256")
        try:
            int(self.pdf_sha256, 16)
        except ValueError as exc:
            raise ValueError("invalid PDF SHA-256") from exc
        return self


@dataclass(frozen=True, slots=True, kw_only=True)
class ParsedDocument:
    document_id: str
    parser_name: str
    parser_version: str
    parse_fingerprint: str
    markdown: str
    structured: Mapping[str, Any]
    page_count: int
    native: Any | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ChunkRecord:
    chunk_id: str
    document_id: str
    attachment_key: str
    chunk_index: int
    text: str
    text_sha256: str
    chunk_fingerprint: str
    token_count: int
    sparse_text: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    section_path: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attachment_key": self.attachment_key,
            "chunk_fingerprint": self.chunk_fingerprint,
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "document_id": self.document_id,
            "metadata": dict(self.metadata),
            "page_end": self.page_end,
            "page_start": self.page_start,
            "section_path": list(self.section_path),
            "sparse_text": self.sparse_text,
            "text": self.text,
            "text_sha256": self.text_sha256,
            "token_count": self.token_count,
        }
