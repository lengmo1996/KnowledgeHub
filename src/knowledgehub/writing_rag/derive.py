"""Derive provenance-preserving writing patterns from parsed Literature artifacts."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from knowledgehub.core.atomic import atomic_write_jsonl
from knowledgehub.core.hashing import sha256_json, sha256_text
from knowledgehub.core.models import KnowledgeDocument
from knowledgehub.indexing.incremental import IncrementalChunkIndexer, IndexInput
from knowledgehub.pipeline.artifacts import safe_document_name
from knowledgehub.pipeline.models import ChunkRecord
from knowledgehub.writing_rag.analyzer import RuleWritingAnalyzer, WritingAnalyzer
from knowledgehub.writing_rag.sections import normalize_section_heading
from knowledgehub.writing_rag.v2 import paragraph_features, paragraph_structure

_NAMESPACE = uuid.UUID("d04ea279-19a5-5e05-afab-cc25f389369f")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_REFERENCE = re.compile(r"^(references|bibliography|acknowledg(?:e)?ments?)$", re.I)
_CAPTION = re.compile(
    r"^(?:[*_]{0,2})(?:fig(?:ure)?|table)\s*"
    r"(?:[A-Z]?\d+(?:\.\d+)*|[IVX]+)\s*[:.\-\u2013\u2014]",
    re.I,
)
_LATIN_WORD = re.compile(r"\b[A-Za-z][A-Za-z0-9-]*\b")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


@dataclass(frozen=True, slots=True)
class WritingEntry:
    writing_id: str
    knowledge_base: str
    source_paper_id: str
    source_title: str
    source_section: str
    source_location: Mapping[str, int | None]
    source_collections: tuple[str, ...]
    venue: str | None
    research_domain: tuple[str, ...]
    writing_function: str
    original_text: str
    normalized_text: str
    abstract_pattern: str
    rhetorical_structure: tuple[str, ...]
    paragraph_pattern: str
    moves: tuple[str, ...]
    transition_relations: tuple[str, ...]
    sentence_roles: tuple[Mapping[str, int | str], ...]
    usage_context: str
    expression_strength: str
    tone: str
    paragraph_word_count: int
    contains_math: bool
    usage_notes: str
    quality_score: float
    confidence: float
    content_hash: str
    analyzer_name: str
    processor_version: str
    prompt_version: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["research_domain"] = list(self.research_domain)
        value["rhetorical_structure"] = list(self.rhetorical_structure)
        value["source_collections"] = list(self.source_collections)
        value["moves"] = list(self.moves)
        value["transition_relations"] = list(self.transition_relations)
        value["sentence_roles"] = list(self.sentence_roles)
        return value


class WritingDerivationService:
    def __init__(
        self,
        *,
        literature_data_dir: Path,
        data_root: Path,
        rag_config: Any,
        analyzer: WritingAnalyzer | None = None,
        processor_version: str = "rules-v1",
        minimum_quality: float = 0.45,
        indexer: IncrementalChunkIndexer | None = None,
    ) -> None:
        self.literature_data_dir = literature_data_dir
        self.data_root = data_root
        self.analyzer = analyzer or RuleWritingAnalyzer()
        self.processor_version = processor_version
        self.minimum_quality = minimum_quality
        self.rag_config = rag_config
        self.indexer = indexer

    def close(self) -> None:
        if self.indexer is not None:
            self.indexer.close()

    def derive(
        self,
        *,
        paper_id: str | None = None,
        collection: str | None = None,
        limit: int | None = 5,
        dry_run: bool = False,
        prune: bool = False,
    ) -> dict[str, Any]:
        if prune and (paper_id is not None or collection is not None or limit is not None):
            raise ValueError("prune requires an unfiltered complete Writing derivation")
        rows = self._literature_documents()
        selected: list[tuple[str, dict[str, Any]]] = []
        for document_id, row in sorted(rows.items()):
            metadata = json.loads(str(row.get("metadata_json") or "{}"))
            if paper_id and document_id != paper_id:
                continue
            if collection and collection not in self._collection_values(metadata):
                continue
            parsed = (
                self.literature_data_dir
                / "parsed"
                / "markdown"
                / f"{safe_document_name(document_id)}.md"
            )
            if parsed.is_file():
                selected.append((document_id, metadata))
            if limit is not None and len(selected) >= limit:
                break
        entries: list[WritingEntry] = []
        for document_id, metadata in selected:
            path = (
                self.literature_data_dir
                / "parsed"
                / "markdown"
                / f"{safe_document_name(document_id)}.md"
            )
            entries.extend(
                self._paper_entries(document_id, metadata, path.read_text(encoding="utf-8"))
            )
        unique = {
            entry.writing_id: entry
            for entry in entries
            if entry.quality_score >= self.minimum_quality
        }
        ordered = [unique[key] for key in sorted(unique)]
        derived_path = self.data_root / "derived" / "writing_entries.jsonl"
        if not dry_run:
            atomic_write_jsonl(derived_path, [entry.to_dict() for entry in ordered])
        inputs = [self._index_input(entry) for entry in ordered]
        indexer = self.indexer or IncrementalChunkIndexer(self.rag_config, initialize=not dry_run)
        summary = indexer.build(inputs, knowledge_base="writing", dry_run=dry_run, prune=prune)
        result = summary.to_dict()
        result.update(
            {
                "papers_selected": len(selected),
                "selected_paper_ids": [document_id for document_id, _metadata in selected],
                "entries_derived": len(ordered),
                "derived_manifest": str(derived_path),
                "analyzer": self.analyzer.name,
                "processor_version": self.processor_version,
            }
        )
        return result

    def _literature_documents(self) -> dict[str, dict[str, Any]]:
        path = self.literature_data_dir / "state" / "pipeline.sqlite3"
        if not path.is_file():
            raise RuntimeError(f"Literature pipeline state is missing: {path}")
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            values = connection.execute(
                "SELECT * FROM pipeline_documents WHERE source_status='ready'"
            ).fetchall()
        finally:
            connection.close()
        return {str(row["document_id"]): dict(row) for row in values}

    def _paper_entries(
        self, document_id: str, metadata: Mapping[str, Any], markdown: str
    ) -> Iterable[WritingEntry]:
        title = str(metadata.get("title") or document_id)
        collections = tuple(sorted(self._collection_values(metadata)))
        venue = self._venue(metadata)
        domains = tuple(
            sorted(
                {
                    str(value).lower().replace(" ", "_")
                    for value in [
                        *(metadata.get("tags") or []),
                        *self._collection_domains(metadata),
                    ]
                    if value
                }
            )
        )[:12]
        for section, paragraph_index, text in self._paragraphs(markdown):
            if self._exclude_source_material(text):
                continue
            if normalize_section_heading(section) == normalize_section_heading(title):
                continue
            analysis = self.analyzer.analyze(text, section=section, domains=domains)
            if analysis is None:
                continue
            structure = paragraph_structure(text, section)
            features = paragraph_features(text)
            content_hash = sha256_text(text)
            identity = sha256_json(
                {
                    "paper": document_id,
                    "section": section,
                    "paragraph": paragraph_index,
                    "content_hash": content_hash,
                    "processor": self.processor_version,
                }
            )
            yield WritingEntry(
                writing_id=f"writing:{identity}",
                knowledge_base="writing",
                source_paper_id=document_id,
                source_title=title,
                source_section=section,
                source_location={"page": None, "paragraph": paragraph_index},
                source_collections=collections,
                venue=venue,
                research_domain=domains,
                writing_function=analysis.writing_function,
                original_text=text,
                normalized_text=analysis.normalized_text,
                abstract_pattern=analysis.abstract_pattern,
                rhetorical_structure=analysis.rhetorical_structure,
                paragraph_pattern=str(structure["paragraph_pattern"]),
                moves=tuple(str(value) for value in structure["moves"]),
                transition_relations=tuple(
                    str(value) for value in structure["transition_relations"]
                ),
                sentence_roles=tuple(structure["sentence_roles"]),
                usage_context=str(structure["usage_context"]),
                expression_strength=str(features["expression_strength"]),
                tone=str(features["tone"]),
                paragraph_word_count=int(features["paragraph_word_count"]),
                contains_math=bool(features["contains_math"]),
                usage_notes=analysis.usage_notes,
                quality_score=analysis.quality_score,
                confidence=analysis.confidence,
                content_hash=content_hash,
                analyzer_name=self.analyzer.name,
                processor_version=self.processor_version,
                prompt_version=None,
                created_at=datetime.now(timezone.utc).isoformat(),
            )

    @staticmethod
    def _exclude_source_material(text: str) -> bool:
        normalized = " ".join(text.split())
        if _CAPTION.match(normalized):
            return True
        return not _CJK.search(normalized) and len(_LATIN_WORD.findall(normalized)) < 12

    @staticmethod
    def _collection_values(metadata: Mapping[str, Any]) -> set[str]:
        values = {
            str(value)
            for value in [
                *(metadata.get("collection_keys") or []),
                *(metadata.get("collection_paths") or []),
            ]
            if value
        }
        for collection in metadata.get("collections") or []:
            if not isinstance(collection, Mapping):
                continue
            values.update(
                str(collection[key]) for key in ("key", "name", "path") if collection.get(key)
            )
        return values

    @staticmethod
    def _collection_domains(metadata: Mapping[str, Any]) -> set[str]:
        values = {str(value) for value in metadata.get("collection_paths") or [] if value}
        for collection in metadata.get("collections") or []:
            if isinstance(collection, Mapping):
                values.update(
                    str(collection[key]) for key in ("name", "path") if collection.get(key)
                )
        return values

    @staticmethod
    def _venue(metadata: Mapping[str, Any]) -> str | None:
        publication = str(metadata.get("publication_title") or "").strip()
        if publication:
            return publication
        candidates = [
            *(metadata.get("tags") or []),
            *(metadata.get("collection_paths") or []),
            *(
                value
                for collection in metadata.get("collections") or []
                if isinstance(collection, Mapping)
                for value in (collection.get("name"), collection.get("path"))
                if value
            ),
        ]
        known = re.compile(
            r"\b(?:NeurIPS|NIPS|ICLR|ICML|CVPR|ECCV|ICCV|AAAI|ACL|EMNLP|IEEE\s+TIP)\b",
            re.I,
        )
        for candidate in candidates:
            match = known.search(str(candidate))
            if match:
                value = match.group(0)
                return "NeurIPS" if value.upper() == "NIPS" else value
        return None

    @staticmethod
    def _paragraphs(markdown: str) -> Iterable[tuple[str, int, str]]:
        section = ""
        paragraph = 0
        buffer: list[str] = []

        def flush() -> tuple[str, int, str] | None:
            nonlocal paragraph, buffer
            text = " ".join(value.strip() for value in buffer if value.strip()).strip()
            buffer = []
            if not text or not section or _REFERENCE.match(section):
                return None
            paragraph += 1
            return section, paragraph, text

        for line in markdown.splitlines():
            heading = _HEADING.match(line)
            if heading:
                value = flush()
                if value:
                    yield value
                section = heading.group(2).strip()
                paragraph = 0
            elif not line.strip():
                value = flush()
                if value:
                    yield value
            elif not line.lstrip().startswith(("|", "```", "<!--")):
                buffer.append(line)
        value = flush()
        if value:
            yield value

    def _index_input(self, entry: WritingEntry) -> IndexInput:
        text = (
            f"Writing function: {entry.writing_function}\n"
            f"Pattern: {entry.abstract_pattern}\n"
            f"Rhetorical structure: {', '.join(entry.rhetorical_structure)}\n"
            f"Usage: {entry.usage_notes}\n"
            f"Research domains: {', '.join(entry.research_domain)}"
        )
        metadata = {
            "knowledge_base": "writing",
            "writing_id": entry.writing_id,
            "source": "literature_derived",
            "source_type": "writing_pattern",
            "source_paper_id": entry.source_paper_id,
            "source_title": entry.source_title,
            "source_location": dict(entry.source_location),
            "source_collections": list(entry.source_collections),
            "section": entry.source_section,
            "venue": entry.venue,
            "writing_function": entry.writing_function,
            "research_domain": list(entry.research_domain),
            "abstract_pattern": entry.abstract_pattern,
            "rhetorical_structure": list(entry.rhetorical_structure),
            "paragraph_pattern": entry.paragraph_pattern,
            "moves": list(entry.moves),
            "transition_relations": list(entry.transition_relations),
            "sentence_roles": list(entry.sentence_roles),
            "usage_context": entry.usage_context,
            "expression_strength": entry.expression_strength,
            "tone": entry.tone,
            "paragraph_word_count": entry.paragraph_word_count,
            "contains_math": entry.contains_math,
            "usage_notes": entry.usage_notes,
            "original_text": entry.original_text,
            "source_excerpt": entry.original_text[:320],
            "quality_score": entry.quality_score,
            "confidence": entry.confidence,
            "processor_version": entry.processor_version,
        }
        document = KnowledgeDocument(
            document_id=entry.writing_id,
            knowledge_base="writing",
            source_type="writing_pattern",
            title=f"{entry.source_section}: {entry.writing_function}",
            content_hash=sha256_text(text),
            source_url="",
            retrieved_at=entry.created_at,
            content=text,
            metadata=metadata,
        ).validate()
        fingerprint = sha256_json(
            {"document": document.document_id, "text": text, "processor": self.processor_version}
        )
        chunk = ChunkRecord(
            chunk_id=str(uuid.uuid5(_NAMESPACE, fingerprint)),
            document_id=document.document_id,
            attachment_key="",
            chunk_index=0,
            text=text,
            text_sha256=sha256_text(text),
            chunk_fingerprint=fingerprint,
            token_count=max(1, len(text.split())),
            section_path=(entry.source_section,),
            metadata=metadata,
        )
        return IndexInput(document, (chunk,), self.processor_version)
