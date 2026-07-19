"""Read-only Docling provenance reconstruction for writing-material extraction."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any, Iterable, Mapping

from packaging.version import InvalidVersion, Version

from knowledgehub.chunking.structural import CHUNK_PROVENANCE_VERSION
from knowledgehub.core.hashing import sha256_json, sha256_text
from knowledgehub.pipeline.artifacts import read_chunks_parquet, safe_document_name
from knowledgehub.writing_rag.materials import SourceSpan
from knowledgehub.writing_rag.sections import normalize_section_heading, section_family

RECONSTRUCTION_VERSION = "docling-provenance-v3"
PROVENANCE_CONTRACT_VERSION = "docling-charspan-v1"
CHUNK_MAP_VERSION = "writing-chunk-map-v1"
_SUPPORTED_DOCLING_MIN = Version("2.112")
_SUPPORTED_DOCLING_MAX = Version("2.113")
_SUPPORTED_DOCLING_DOCUMENT_SCHEMAS = {"1.10.0"}
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^()]*\))*\)")
_SENTENCE = re.compile(r"\S(?:.*?)(?:[.!?]+(?=\s|$)|[\u3002\uff01\uff1f]+|$)", re.S)
_REFERENCE = re.compile(r"^(?:references|bibliography|acknowledg(?:e)?ments?)$", re.I)


class ProvenanceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Sentence:
    sentence_id: str
    index: int
    start: int
    end: int
    text_hash: str


@dataclass(frozen=True, slots=True)
class Segment:
    paragraph_start: int
    paragraph_end: int
    self_ref: str
    source_start: int
    source_end: int
    page_no: int
    bbox: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Paragraph:
    paragraph_id: str
    index: int
    text: str
    text_hash: str
    section_id: str
    section_title: str
    section_path: tuple[str, ...]
    section_family: str
    segments: tuple[Segment, ...]
    sentences: tuple[Sentence, ...]

    def map_range(self, start: int, end: int) -> tuple[SourceSpan, ...]:
        result: list[SourceSpan] = []
        for segment in self.segments:
            overlap_start = max(start, segment.paragraph_start)
            overlap_end = min(end, segment.paragraph_end)
            if overlap_start >= overlap_end:
                continue
            source_start = segment.source_start + (overlap_start - segment.paragraph_start)
            source_end = source_start + (overlap_end - overlap_start)
            result.append(
                SourceSpan(
                    self_ref=segment.self_ref,
                    source_start=source_start,
                    source_end=source_end,
                    paragraph_start=overlap_start,
                    paragraph_end=overlap_end,
                    page_no=segment.page_no,
                    bbox=segment.bbox,
                )
            )
        return tuple(result)


@dataclass(frozen=True, slots=True)
class ChunkParagraphMap:
    chunk_id: str
    paragraph_id: str
    sentence_ids: tuple[str, ...]
    source_spans: tuple[SourceSpan, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "paragraph_id": self.paragraph_id,
            "sentence_ids": list(self.sentence_ids),
            "source_spans": [
                {
                    "self_ref": value.self_ref,
                    "source_start": value.source_start,
                    "source_end": value.source_end,
                    "paragraph_start": value.paragraph_start,
                    "paragraph_end": value.paragraph_end,
                    "page_no": value.page_no,
                    "bbox": dict(value.bbox),
                }
                for value in self.source_spans
            ],
        }


@dataclass(frozen=True, slots=True)
class ChunkMapResult:
    document_id: str
    parse_fingerprint: str
    status: str
    reason: str | None
    mappings: tuple[ChunkParagraphMap, ...]
    schema_version: str = CHUNK_MAP_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "document_id": self.document_id,
            "parse_fingerprint": self.parse_fingerprint,
            "status": self.status,
            "reason": self.reason,
            "mappings": [value.to_dict() for value in self.mappings],
        }


@dataclass(frozen=True, slots=True)
class SelectionSnapshot:
    document_ids: tuple[str, ...]
    records: tuple[Mapping[str, str], ...]
    sources: Mapping[str, Any]

    @property
    def sha256(self) -> str:
        return sha256_json([dict(value) for value in self.records])


@dataclass(frozen=True, slots=True)
class ProvenanceDocument:
    document_id: str
    zotero_item_key: str
    attachment_key: str
    title: str
    source_content_fingerprint: str
    parse_fingerprint: str
    parser_name: str
    parser_version: str
    paragraphs: tuple[Paragraph, ...]
    structure_aligned: bool
    provenance_coverage: float
    provenance_characters_by_section: Mapping[str, tuple[int, int]]

    def coverage_for(self, section_families: Iterable[str]) -> float:
        selected = set(section_families)
        eligible = sum(
            counts[0]
            for family, counts in self.provenance_characters_by_section.items()
            if family in selected
        )
        covered = sum(
            counts[1]
            for family, counts in self.provenance_characters_by_section.items()
            if family in selected
        )
        return covered / eligible if eligible else 0.0


class ProvenanceDocumentReader:
    """Consume Literature state and parsed artifacts without touching Qdrant or Zotero state."""

    def __init__(self, literature_data_dir: Path) -> None:
        self.root = literature_data_dir
        self.state_path = literature_data_dir / "state" / "pipeline.sqlite3"

    def documents(self) -> dict[str, dict[str, Any]]:
        if not self.state_path.is_file():
            raise ProvenanceError(
                "missing_literature_state", f"Literature state is missing: {self.state_path}"
            )
        connection = sqlite3.connect(f"file:{self.state_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT * FROM pipeline_documents WHERE source_status='ready' ORDER BY document_id"
            ).fetchall()
        finally:
            connection.close()
        return {str(row["document_id"]): dict(row) for row in rows}

    def checkpoint(self) -> dict[str, Any] | None:
        if not self.state_path.is_file():
            return None
        with sqlite3.connect(f"file:{self.state_path}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT sequence,sync_id,delta_sha256 FROM consumed_deltas "
                "WHERE source='zotero' AND status='success' ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def load(self, document_id: str) -> ProvenanceDocument:
        if not self.state_path.is_file():
            raise ProvenanceError(
                "missing_literature_state", f"Literature state is missing: {self.state_path}"
            )
        with sqlite3.connect(f"file:{self.state_path}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            selected = connection.execute(
                "SELECT * FROM pipeline_documents WHERE document_id=? AND source_status='ready'",
                (document_id,),
            ).fetchone()
        if selected is None:
            raise ProvenanceError(
                "unknown_document", f"Literature document is not ready: {document_id}"
            )
        row = dict(selected)
        if row.get("parse_status") != "ready":
            raise ProvenanceError("parse_not_ready", f"Parsed artifact is not ready: {document_id}")
        if row.get("parser_name") != "docling":
            raise ProvenanceError(
                "unsupported_provenance", "MVP requires a Docling parsed document"
            )
        name = safe_document_name(document_id)
        json_path = self.root / "parsed" / "json" / f"{name}.json"
        markdown_path = self.root / "parsed" / "markdown" / f"{name}.md"
        if not json_path.is_file() or not markdown_path.is_file():
            raise ProvenanceError(
                "missing_parsed_artifact", f"Parsed artifacts are missing: {document_id}"
            )
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or payload.get("document_id") != document_id:
            raise ProvenanceError(
                "parsed_identity_mismatch", f"Parsed identity mismatch: {document_id}"
            )
        structured = payload.get("structured")
        if not isinstance(structured, Mapping):
            raise ProvenanceError(
                "invalid_parsed_structure", f"Invalid Docling structure: {document_id}"
            )
        _validate_docling_contract(row, payload, structured)
        metadata = json.loads(str(row.get("metadata_json") or "{}"))
        headings = _markdown_headings(markdown_path.read_text(encoding="utf-8"))
        paragraphs, aligned, character_counts = _paragraphs(
            document_id,
            str(row.get("parse_fingerprint") or ""),
            structured,
            headings,
        )
        if not paragraphs:
            raise ProvenanceError(
                "no_provenance_paragraphs", f"No traceable paragraphs: {document_id}"
            )
        eligible_characters = sum(counts[0] for counts in character_counts.values())
        covered_characters = sum(counts[1] for counts in character_counts.values())
        coverage = covered_characters / eligible_characters if eligible_characters else 0.0
        if not aligned:
            raise ProvenanceError(
                "section_alignment_failed",
                "Docling section headers do not align with canonical Markdown",
            )
        item_key = str(metadata.get("item_key") or "")
        attachment_key = str(metadata.get("attachment_key") or row.get("attachment_key") or "")
        if not item_key or not attachment_key:
            raise ProvenanceError(
                "missing_zotero_identity", "Zotero item or attachment key is missing"
            )
        return ProvenanceDocument(
            document_id=document_id,
            zotero_item_key=item_key,
            attachment_key=attachment_key,
            title=str(metadata.get("title") or document_id),
            source_content_fingerprint=str(row.get("source_content_fingerprint") or ""),
            parse_fingerprint=str(row.get("parse_fingerprint") or ""),
            parser_name=str(row.get("parser_name") or ""),
            parser_version=str(row.get("parser_version") or ""),
            paragraphs=tuple(paragraphs),
            structure_aligned=aligned,
            provenance_coverage=coverage,
            provenance_characters_by_section=character_counts,
        )

    def chunk_map(self, document: ProvenanceDocument) -> ChunkMapResult:
        """Join canonical chunks to exact Docling items when the contract proves it.

        Existing canonical chunks that predate ``CHUNK_PROVENANCE_VERSION``
        intentionally return ``not_available``.  Text similarity is never used
        to manufacture a mapping.
        """

        path = self.root / "chunks" / f"{safe_document_name(document.document_id)}.parquet"

        def unavailable(reason: str) -> ChunkMapResult:
            return ChunkMapResult(
                document_id=document.document_id,
                parse_fingerprint=document.parse_fingerprint,
                status="not_available",
                reason=reason,
                mappings=(),
            )

        if not path.is_file():
            return unavailable("chunk_artifact_missing")
        try:
            chunks = read_chunks_parquet(path)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            return unavailable("chunk_artifact_invalid")
        if not chunks:
            return unavailable("chunk_artifact_empty")

        chunks_by_ref: dict[str, set[str]] = {}
        for chunk in chunks:
            if chunk.get("document_id") != document.document_id:
                return unavailable("chunk_document_identity_mismatch")
            chunk_id = chunk.get("chunk_id")
            metadata = chunk.get("metadata")
            if not isinstance(chunk_id, str) or not chunk_id or not isinstance(metadata, Mapping):
                return unavailable("chunk_contract_invalid")
            if metadata.get("chunk_provenance_version") != CHUNK_PROVENANCE_VERSION:
                return unavailable("chunk_provenance_contract_missing_or_unsupported")
            refs = metadata.get("doc_item_refs")
            if (
                not isinstance(refs, list)
                or not refs
                or any(not isinstance(ref, str) or not ref.startswith("#/") for ref in refs)
            ):
                return unavailable("chunk_doc_item_refs_invalid")
            for ref in refs:
                chunks_by_ref.setdefault(ref, set()).add(chunk_id)

        mappings: list[ChunkParagraphMap] = []
        for paragraph in document.paragraphs:
            refs = {segment.self_ref for segment in paragraph.segments}
            if not refs or any(ref not in chunks_by_ref for ref in refs):
                return unavailable("paragraph_source_ref_not_mapped")
            chunk_ids = {chunk_id for ref in refs for chunk_id in chunks_by_ref[ref]}
            if len(chunk_ids) != 1:
                return unavailable("paragraph_source_ref_ambiguous")
            mappings.append(
                ChunkParagraphMap(
                    chunk_id=next(iter(chunk_ids)),
                    paragraph_id=paragraph.paragraph_id,
                    sentence_ids=tuple(value.sentence_id for value in paragraph.sentences),
                    source_spans=tuple(
                        SourceSpan(
                            self_ref=value.self_ref,
                            source_start=value.source_start,
                            source_end=value.source_end,
                            paragraph_start=value.paragraph_start,
                            paragraph_end=value.paragraph_end,
                            page_no=value.page_no,
                            bbox=value.bbox,
                        )
                        for value in paragraph.segments
                    ),
                )
            )
        return ChunkMapResult(
            document_id=document.document_id,
            parse_fingerprint=document.parse_fingerprint,
            status="available",
            reason=None,
            mappings=tuple(mappings),
        )


def load_selection(path: Path, *, limit: int | None = None) -> tuple[str, ...]:
    if not path.is_file():
        raise ValueError(f"selection manifest is missing: {path}")
    identifiers: list[str] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid selection JSON at line {line_number}") from exc
        document_id = (
            value
            if isinstance(value, str)
            else value.get("document_id")
            if isinstance(value, Mapping)
            else None
        )
        if not isinstance(document_id, str) or not document_id.startswith("zotero:"):
            raise ValueError(f"invalid selection document_id at line {line_number}")
        if document_id not in identifiers:
            identifiers.append(document_id)
        if limit is not None and len(identifiers) >= limit:
            break
    if not identifiers:
        raise ValueError("selection manifest contains no document IDs")
    return tuple(identifiers)


def resolve_selection(
    reader: ProvenanceDocumentReader,
    *,
    selection: Path | None = None,
    document_ids: Iterable[str] = (),
    collections: Iterable[str] = (),
    limit: int | None = None,
) -> SelectionSnapshot:
    """Resolve explicit selectors into a deterministic, source-pinned snapshot."""

    requested_documents = list(load_selection(selection) if selection is not None else ())
    requested_documents.extend(document_ids)
    requested_collections = tuple(
        value.strip() for value in collections if isinstance(value, str) and value.strip()
    )
    available = reader.documents()
    selected: set[str] = set()
    for document_id in requested_documents:
        if not isinstance(document_id, str) or not document_id.startswith("zotero:"):
            raise ValueError(f"invalid selected document ID: {document_id}")
        if document_id not in available:
            raise ProvenanceError(
                "unknown_document", f"Literature document is not ready: {document_id}"
            )
        selected.add(document_id)
    for document_id, row in available.items():
        metadata = json.loads(str(row.get("metadata_json") or "{}"))
        raw_collections = metadata.get("collections") if isinstance(metadata, Mapping) else None
        values: set[str] = set()
        if isinstance(raw_collections, list):
            for item in raw_collections:
                if isinstance(item, Mapping):
                    values.update(
                        str(item.get(key) or "").strip() for key in ("key", "name", "path")
                    )
        if any(value in values for value in requested_collections):
            selected.add(document_id)
    if requested_collections:
        matched_values: set[str] = set()
        for document_id in selected:
            metadata = json.loads(str(available[document_id].get("metadata_json") or "{}"))
            for item in metadata.get("collections") or []:
                if isinstance(item, Mapping):
                    matched_values.update(
                        str(item.get(key) or "").strip() for key in ("key", "name", "path")
                    )
        missing = set(requested_collections) - matched_values
        if missing:
            raise ProvenanceError(
                "unknown_collection",
                f"No ready documents found for collection(s): {sorted(missing)}",
            )
    ordered = sorted(selected)
    if limit is not None:
        if limit <= 0:
            raise ValueError("selection limit must be positive")
        ordered = ordered[:limit]
    if not ordered:
        raise ValueError("at least one selection, document ID or collection is required")
    records = tuple(
        {
            "document_id": document_id,
            "source_content_fingerprint": str(
                available[document_id].get("source_content_fingerprint") or ""
            ),
            "parse_fingerprint": str(available[document_id].get("parse_fingerprint") or ""),
            "parser_name": str(available[document_id].get("parser_name") or ""),
            "parser_version": str(available[document_id].get("parser_version") or ""),
        }
        for document_id in ordered
    )
    return SelectionSnapshot(
        document_ids=tuple(ordered),
        records=records,
        sources={
            "selection": str(selection.resolve()) if selection is not None else None,
            "document_ids": sorted(set(requested_documents)),
            "collections": sorted(set(requested_collections)),
            "limit": limit,
        },
    )


def _markdown_headings(markdown: str) -> list[tuple[int, str]]:
    return [
        (len(match.group(1)), match.group(2).strip())
        for line in markdown.splitlines()
        if (match := _HEADING.match(line))
    ]


def _paragraphs(
    document_id: str,
    parse_fingerprint: str,
    structured: Mapping[str, Any],
    headings: list[tuple[int, str]],
) -> tuple[list[Paragraph], bool, dict[str, tuple[int, int]]]:
    texts = structured.get("texts")
    groups = structured.get("groups")
    body = structured.get("body")
    if not isinstance(texts, list) or not isinstance(groups, list) or not isinstance(body, Mapping):
        raise ProvenanceError(
            "invalid_docling_structure", "Docling body, texts or groups are invalid"
        )
    ordered = list(_walk_refs(body.get("children"), texts, groups, set()))
    current_title = ""
    current_path: list[str] = []
    section_id = "section:root"
    paragraph_index = 0
    heading_cursor = 0
    aligned = True
    eligible_characters: dict[str, int] = {}
    covered_characters: dict[str, int] = {}
    paragraphs: list[Paragraph] = []
    for item in ordered:
        label = str(item.get("label") or "")
        original = item.get("orig")
        text = original if isinstance(original, str) else str(item.get("text") or "")
        if not text.strip():
            continue
        if label == "section_header":
            level, matched = _match_heading(text, headings, heading_cursor)
            if matched is not None:
                heading_cursor = matched + 1
            else:
                aligned = False
                level = 1
            current_path = current_path[: max(0, level - 1)]
            current_path.append(text.strip())
            current_title = text.strip()
            section_id = f"section:{sha256_json({'document': document_id, 'path': current_path, 'parse': parse_fingerprint})}"
            paragraph_index = 0
            continue
        if (
            label not in {"text", "list_item"}
            or not current_title
            or _REFERENCE.match(normalize_section_heading(current_title))
        ):
            continue
        if not isinstance(original, str):
            raise ProvenanceError(
                "unsupported_charspan_contract",
                "Docling text item lacks the exact orig string required by charspan",
            )
        family = section_family(current_title)
        eligible_characters[family] = eligible_characters.get(family, 0) + len(text)
        segments = _segments(item, text)
        covered_characters[family] = covered_characters.get(family, 0) + sum(
            segment.paragraph_end - segment.paragraph_start for segment in segments
        )
        if not segments:
            continue
        paragraph_index += 1
        text_hash = sha256_text(text)
        identity = {
            "document": document_id,
            "parse": parse_fingerprint,
            "section": section_id,
            "index": paragraph_index,
            "text": text_hash,
            "reconstruction": RECONSTRUCTION_VERSION,
        }
        paragraph_id = f"paragraph:{sha256_json(identity)}"
        sentences = tuple(_sentences(paragraph_id, text))
        paragraphs.append(
            Paragraph(
                paragraph_id=paragraph_id,
                index=paragraph_index,
                text=text,
                text_hash=text_hash,
                section_id=section_id,
                section_title=current_title,
                section_path=tuple(current_path),
                section_family=family,
                segments=segments,
                sentences=sentences,
            )
        )
    character_counts = {
        family: (eligible, covered_characters.get(family, 0))
        for family, eligible in eligible_characters.items()
    }
    return paragraphs, aligned, character_counts


def _walk_refs(
    raw: Any,
    texts: list[Any],
    groups: list[Any],
    seen_groups: set[int],
) -> Iterable[Mapping[str, Any]]:
    if not isinstance(raw, list):
        return
    for child in raw:
        if not isinstance(child, Mapping):
            continue
        ref = str(child.get("cref") or "")
        if ref.startswith("#/texts/"):
            try:
                item = texts[int(ref.rsplit("/", 1)[1])]
            except (ValueError, IndexError):
                continue
            if isinstance(item, Mapping):
                yield item
        elif ref.startswith("#/groups/"):
            try:
                index = int(ref.rsplit("/", 1)[1])
                group = groups[index]
            except (ValueError, IndexError):
                continue
            if index in seen_groups or not isinstance(group, Mapping):
                continue
            seen_groups.add(index)
            yield from _walk_refs(group.get("children"), texts, groups, seen_groups)


def _match_heading(
    text: str, headings: list[tuple[int, str]], cursor: int
) -> tuple[int, int | None]:
    expected = _heading_key(text)
    for index in range(cursor, min(len(headings), cursor + 8)):
        level, heading = headings[index]
        if _heading_key(heading) == expected:
            return level, index
    return 1, None


def _heading_key(value: str) -> str:
    unescaped = unescape(value)
    link_text = _MARKDOWN_LINK.sub(r"\1", unescaped)
    return normalize_section_heading(link_text)


def _segments(item: Mapping[str, Any], text: str) -> tuple[Segment, ...]:
    provenance = item.get("prov")
    if not isinstance(provenance, list) or not provenance:
        return ()
    self_ref = str(item.get("self_ref") or "")
    if not self_ref:
        return ()
    result: list[Segment] = []
    for value in provenance:
        if not isinstance(value, Mapping):
            return ()
        page_no, bbox, charspan = value.get("page_no"), value.get("bbox"), value.get("charspan")
        if (
            not isinstance(page_no, int)
            or isinstance(page_no, bool)
            or page_no <= 0
            or not _valid_bbox(bbox)
        ):
            return ()
        assert isinstance(bbox, Mapping)
        if (
            not isinstance(charspan, list)
            or len(charspan) != 2
            or not all(
                isinstance(offset, int) and not isinstance(offset, bool) for offset in charspan
            )
        ):
            return ()
        source_start, source_end = int(charspan[0]), int(charspan[1])
        if source_start < 0 or source_end <= source_start or source_end > len(text):
            return ()
        result.append(
            Segment(
                source_start,
                source_end,
                self_ref,
                source_start,
                source_end,
                page_no,
                dict(bbox),
            )
        )
    result.sort(key=lambda value: (value.paragraph_start, value.paragraph_end))
    previous_end = -1
    for segment in result:
        if segment.paragraph_start < previous_end:
            return ()
        previous_end = segment.paragraph_end
    return tuple(result)


def _valid_bbox(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    for key in ("l", "t", "r", "b"):
        coordinate = value.get(key)
        if (
            not isinstance(coordinate, (int, float))
            or isinstance(coordinate, bool)
            or not math.isfinite(float(coordinate))
        ):
            return False
    return True


def _validate_docling_contract(
    row: Mapping[str, Any], payload: Mapping[str, Any], structured: Mapping[str, Any]
) -> None:
    for field in ("parser_name", "parser_version", "parse_fingerprint"):
        if payload.get(field) != row.get(field):
            raise ProvenanceError(
                "parsed_contract_mismatch", f"Parsed artifact {field} differs from Literature state"
            )
    raw_version = row.get("parser_version")
    if not isinstance(raw_version, str) or not raw_version:
        raise ProvenanceError("unsupported_parser_version", "Docling parser version is missing")
    try:
        parser_version = Version(raw_version)
    except InvalidVersion as exc:
        raise ProvenanceError(
            "unsupported_parser_version", f"Invalid Docling parser version: {raw_version}"
        ) from exc
    if not _SUPPORTED_DOCLING_MIN <= parser_version < _SUPPORTED_DOCLING_MAX:
        raise ProvenanceError(
            "unsupported_parser_version",
            f"Docling {raw_version} is outside the verified [2.112,2.113) range",
        )
    if structured.get("schema_name") != "DoclingDocument":
        raise ProvenanceError(
            "unsupported_docling_schema", "Structured artifact is not a DoclingDocument"
        )
    schema_version = structured.get("version")
    if schema_version not in _SUPPORTED_DOCLING_DOCUMENT_SCHEMAS:
        raise ProvenanceError(
            "unsupported_docling_schema",
            f"Unsupported DoclingDocument schema version: {schema_version}",
        )


def _sentences(paragraph_id: str, text: str) -> Iterable[Sentence]:
    index = 0
    for match in _SENTENCE.finditer(text):
        raw_start, raw_end = match.span()
        value = match.group(0)
        leading = len(value) - len(value.lstrip())
        trailing = len(value) - len(value.rstrip())
        start = raw_start + leading
        end = raw_end - trailing
        if start >= end:
            continue
        text_hash = sha256_text(text[start:end])
        identity = {
            "paragraph": paragraph_id,
            "index": index,
            "start": start,
            "end": end,
            "text": text_hash,
        }
        yield Sentence(f"sentence:{sha256_json(identity)}", index, start, end, text_hash)
        index += 1
