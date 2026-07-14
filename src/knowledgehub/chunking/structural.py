"""Docling HybridChunker with a deterministic markdown fallback."""

from __future__ import annotations

import re
import uuid
from typing import Any, Iterable, Mapping

from knowledgehub.chunking.fingerprints import document_chunk_fingerprint
from knowledgehub.core.hashing import sha256_json, sha256_text
from knowledgehub.pipeline.config import RagConfig
from knowledgehub.pipeline.models import ChunkRecord, ParsedDocument, SourceDocument

_CHUNK_NAMESPACE = uuid.UUID("81ac64a2-e34c-5a35-b7f0-082fc48e8601")
_PAGE_MARKER = re.compile(r"<!--\s*page:(\d+)\s*-->")


class StructuralChunker:
    def __init__(self, config: RagConfig) -> None:
        self.config = config
        self._chunker: Any | None = None
        self._tokenizer: Any | None = None

    def _load(self) -> None:
        if self._chunker is not None:
            return
        from docling.chunking import HybridChunker
        from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            self.config.embedding_model,
            revision=self.config.embedding_revision,
            cache_dir=self.config.model_cache_dir / "tei",
            padding_side="left",
            local_files_only=True,
        )
        wrapper = HuggingFaceTokenizer(tokenizer=tokenizer, max_tokens=self.config.chunk_max_tokens)
        self._tokenizer = wrapper
        self._chunker = HybridChunker(tokenizer=wrapper, merge_peers=self.config.chunk_merge_peers)

    def chunk(self, document: SourceDocument, parsed: ParsedDocument) -> list[ChunkRecord]:
        rows: list[tuple[str, Mapping[str, Any]]] = []
        if parsed.native is not None:
            self._load()
            assert self._chunker is not None
            for value in self._chunker.chunk(dl_doc=parsed.native):
                text = str(value.text or "").strip()
                if text:
                    meta = (
                        value.meta.model_dump(mode="json")
                        if hasattr(value.meta, "model_dump")
                        else {}
                    )
                    rows.append((text, meta if isinstance(meta, Mapping) else {}))
        else:
            rows.extend(self._fallback_chunks(parsed.markdown))

        records: list[ChunkRecord] = []
        document_chunk_config = document_chunk_fingerprint(self.config, parsed.parse_fingerprint)
        for index, (text, metadata) in enumerate(rows):
            pages = _page_numbers(metadata)
            token_count = self._count_tokens(text)
            fingerprint = sha256_json(
                {
                    "document_id": document.document_id,
                    "index": index,
                    "chunk_config": document_chunk_config,
                    "text": text,
                }
            )
            chunk_id = str(
                uuid.uuid5(
                    _CHUNK_NAMESPACE,
                    f"{document.source}\0{document.document_id}\0{index}\0{fingerprint}",
                )
            )
            records.append(
                ChunkRecord(
                    chunk_id=chunk_id,
                    document_id=document.document_id,
                    attachment_key=document.attachment_key,
                    chunk_index=index,
                    text=text,
                    text_sha256=sha256_text(text),
                    chunk_fingerprint=fingerprint,
                    token_count=token_count,
                    page_start=min(pages) if pages else None,
                    page_end=max(pages) if pages else None,
                    section_path=_headings(metadata),
                    metadata={
                        "source": document.source,
                        "title": document.title,
                        "doi": document.doi,
                        "year": document.year,
                        "tags": list(document.tags),
                        "collection_keys": list(document.collection_keys),
                        "collection_paths": list(document.collection_paths),
                    },
                )
            )
        return records

    def _count_tokens(self, text: str) -> int:
        if self._tokenizer is not None and hasattr(self._tokenizer, "count_tokens"):
            return int(self._tokenizer.count_tokens(text))
        return max(1, len(text.split()))

    def _fallback_chunks(self, markdown: str) -> Iterable[tuple[str, Mapping[str, Any]]]:
        paragraphs = [value.strip() for value in re.split(r"\n\s*\n", markdown) if value.strip()]
        current: list[str] = []
        tokens = 0
        page: int | None = None
        for paragraph in paragraphs:
            marker = _PAGE_MARKER.search(paragraph)
            if marker:
                page = int(marker.group(1))
                paragraph = _PAGE_MARKER.sub("", paragraph).strip()
            count = max(1, len(paragraph.split()))
            if current and tokens + count > self.config.chunk_max_tokens:
                yield "\n\n".join(current), {"page": page}
                current, tokens = [], 0
            if paragraph:
                current.append(paragraph)
                tokens += count
        if current:
            yield "\n\n".join(current), {"page": page}


def _page_numbers(metadata: Mapping[str, Any]) -> list[int]:
    result: set[int] = set()
    direct = metadata.get("page")
    if isinstance(direct, int):
        result.add(direct)
    for item in metadata.get("doc_items") or []:
        if not isinstance(item, Mapping):
            continue
        for provenance in item.get("prov") or []:
            if isinstance(provenance, Mapping) and isinstance(provenance.get("page_no"), int):
                result.add(int(provenance["page_no"]))
    return sorted(result)


def _headings(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    headings = metadata.get("headings") or []
    return tuple(str(value) for value in headings) if isinstance(headings, list) else ()
