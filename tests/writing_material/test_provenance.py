from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from knowledgehub.chunking.structural import CHUNK_PROVENANCE_VERSION, _doc_item_refs
from knowledgehub.pipeline.artifacts import safe_document_name, write_chunks_parquet
from knowledgehub.pipeline.models import ChunkRecord
from knowledgehub.writing_rag.provenance import (
    CHUNK_MAP_VERSION,
    PROVENANCE_CONTRACT_VERSION,
    ProvenanceDocumentReader,
    ProvenanceError,
    _match_heading,
    _paragraphs,
    _segments,
    resolve_selection,
)
from knowledgehub.writing_rag.sections import section_family

from .helpers import (
    DOCUMENT_ID,
    PARAGRAPH_TEXT,
    build_literature_fixture,
)


def test_docling_reconstruction_has_stable_offsets_and_zotero_identity(tmp_path) -> None:
    root = build_literature_fixture(tmp_path / "literature")
    first = ProvenanceDocumentReader(root).load(DOCUMENT_ID)
    second = ProvenanceDocumentReader(root).load(DOCUMENT_ID)
    paragraph = first.paragraphs[0]
    assert paragraph.paragraph_id == second.paragraphs[0].paragraph_id
    assert paragraph.text == PARAGRAPH_TEXT
    assert paragraph.section_path == ("Introduction",)
    assert paragraph.section_family == "introduction"
    assert paragraph.segments[0].page_no == 1
    assert paragraph.sentences[0].start == 0
    assert first.zotero_item_key == "ITEMKEY"
    assert first.attachment_key == "ATTACHKEY"
    assert first.structure_aligned is True
    assert first.provenance_coverage == 1.0
    assert first.coverage_for({"introduction"}) == 1.0


def test_selection_resolver_freezes_document_and_collection_sources(tmp_path) -> None:
    root = build_literature_fixture(tmp_path / "literature")
    reader = ProvenanceDocumentReader(root)
    direct = resolve_selection(reader, document_ids=[DOCUMENT_ID])
    assert direct.document_ids == (DOCUMENT_ID,)
    assert direct.records[0]["parse_fingerprint"] == "parse-1"
    by_key = resolve_selection(reader, collections=["COLLKEY"])
    by_path = resolve_selection(reader, collections=["Tests/Fixture"])
    assert by_key.records == by_path.records == direct.records
    with pytest.raises(ProvenanceError) as missing:
        resolve_selection(reader, collections=["missing-collection"])
    assert missing.value.code == "unknown_collection"


def test_sanitized_docling_fixture_reproduces_cross_page_unicode_and_orig_offsets() -> None:
    path = (
        Path(__file__).parent
        / "fixtures"
        / "provenance"
        / "docling-2.112-schema-1.10.sanitized.json"
    )
    fixture = json.loads(path.read_text(encoding="utf-8"))
    assert fixture["provenance"]["charspan_semantics"] == "python-orig-half-open"
    assert PROVENANCE_CONTRACT_VERSION == "docling-charspan-v1"
    paragraphs, aligned, counts = _paragraphs(
        fixture["document_id"],
        fixture["parse_fingerprint"],
        fixture["structured"],
        [(1, "Introduction")],
    )
    assert aligned is True
    assert counts == {"introduction": (52, 52)}
    assert paragraphs[0].text == "Alpha βeta.\nGamma delta。"
    assert [
        (value.page_no, value.paragraph_start, value.paragraph_end)
        for value in paragraphs[0].segments
    ] == [
        (1, 0, 11),
        (2, 11, 24),
    ]
    assert paragraphs[0].sentences[0].start == 0
    assert paragraphs[0].sentences[-1].end == 24
    assert paragraphs[1].text == "echo echo"
    assert paragraphs[2].text == "multi-\nmodal ﬁnding"
    assert paragraphs[2].text != fixture["structured"]["texts"][3]["text"]


def test_heading_alignment_normalizes_html_entities_and_markdown_links() -> None:
    assert _match_heading(
        "3. Formal Statements & Insights",
        [(2, "3. Formal Statements &amp; Insights")],
        0,
    ) == (2, 0)
    assert _match_heading(
        "Fahad Shahbaz Khan",
        [(3, "[Fahad Shahbaz Khan](https://orcid.org/0000-0002-4263-3143)")],
        0,
    ) == (3, 0)


def test_chinese_mvp_section_aliases_are_conservative_and_exact() -> None:
    assert section_family("引") == "introduction"
    assert section_family("未来展望") == "conclusion"
    assert section_family("5 结论与展望") == "conclusion"


def test_segment_map_preserves_gaps_but_rejects_overlap() -> None:
    text = "abcdefghij"
    item = {
        "self_ref": "#/texts/1",
        "prov": [
            {
                "page_no": 1,
                "bbox": {"l": 0, "t": 0, "r": 1, "b": 1},
                "charspan": [0, 4],
            },
            {
                "page_no": 2,
                "bbox": {"l": 0, "t": 0, "r": 1, "b": 1},
                "charspan": [5, 10],
            },
        ],
    }
    segments = _segments(item, text)
    assert [(value.paragraph_start, value.paragraph_end) for value in segments] == [
        (0, 4),
        (5, 10),
    ]
    item["prov"][1]["charspan"] = [3, 10]
    assert _segments(item, text) == ()


def test_segment_map_rejects_invalid_bbox_and_boolean_offsets() -> None:
    text = "abcdefghij"
    item = {
        "self_ref": "#/texts/1",
        "prov": [
            {
                "page_no": 1,
                "bbox": {"l": 0, "t": 0, "r": 1, "b": float("nan")},
                "charspan": [0, 10],
            }
        ],
    }
    assert _segments(item, text) == ()
    item["prov"][0]["bbox"]["b"] = 1
    item["prov"][0]["charspan"] = [False, 10]
    assert _segments(item, text) == ()


def test_docling_envelope_and_verified_version_are_fail_closed(tmp_path) -> None:
    root = build_literature_fixture(tmp_path / "literature")
    path = root / "parsed" / "json" / f"{safe_document_name(DOCUMENT_ID)}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["parser_version"] = "2.113.0"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ProvenanceError) as mismatch:
        ProvenanceDocumentReader(root).load(DOCUMENT_ID)
    assert mismatch.value.code == "parsed_contract_mismatch"

    value["parser_version"] = "2.112.0"
    value["structured"]["version"] = "1.11.0"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ProvenanceError) as schema:
        ProvenanceDocumentReader(root).load(DOCUMENT_ID)
    assert schema.value.code == "unsupported_docling_schema"

    with sqlite3.connect(root / "state" / "pipeline.sqlite3") as state:
        state.execute("UPDATE pipeline_documents SET parser_version='2.113.0'")
    value["parser_version"] = "2.113.0"
    value["structured"]["version"] = "1.10.0"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ProvenanceError) as parser:
        ProvenanceDocumentReader(root).load(DOCUMENT_ID)
    assert parser.value.code == "unsupported_parser_version"


def test_chunk_map_requires_versioned_doc_item_refs_and_never_guesses(tmp_path) -> None:
    root = build_literature_fixture(tmp_path / "literature")
    reader = ProvenanceDocumentReader(root)
    document = reader.load(DOCUMENT_ID)
    assert reader.chunk_map(document).to_dict() == {
        "schema_version": CHUNK_MAP_VERSION,
        "document_id": DOCUMENT_ID,
        "parse_fingerprint": "parse-1",
        "status": "not_available",
        "reason": "chunk_artifact_missing",
        "mappings": [],
    }

    paragraph = document.paragraphs[0]
    chunk = ChunkRecord(
        chunk_id="chunk-1",
        document_id=DOCUMENT_ID,
        attachment_key="ATTACHKEY",
        chunk_index=0,
        text=paragraph.text,
        text_sha256=paragraph.text_hash,
        chunk_fingerprint="chunk-fingerprint-1",
        token_count=10,
        page_start=1,
        page_end=1,
        section_path=paragraph.section_path,
        metadata={
            "chunk_provenance_version": CHUNK_PROVENANCE_VERSION,
            "doc_item_refs": ["#/texts/1"],
        },
    )
    write_chunks_parquet(root, DOCUMENT_ID, [chunk])
    result = reader.chunk_map(document)
    assert result.status == "available"
    assert result.reason is None
    assert len(result.mappings) == 1
    assert result.mappings[0].chunk_id == "chunk-1"
    assert result.mappings[0].source_spans[0].self_ref == "#/texts/1"

    write_chunks_parquet(
        root,
        DOCUMENT_ID,
        [chunk, replace(chunk, chunk_id="chunk-2", chunk_index=1)],
    )
    ambiguous = reader.chunk_map(document)
    assert ambiguous.status == "not_available"
    assert ambiguous.reason == "paragraph_source_ref_ambiguous"


def test_chunker_retains_only_stable_doc_item_references() -> None:
    metadata = {
        "doc_items": [
            {"self_ref": "#/texts/2", "text": "must not be copied"},
            {"self_ref": "#/texts/1", "prov": [{"charspan": [0, 2]}]},
            {"self_ref": "unsafe"},
        ]
    }
    assert _doc_item_refs(metadata) == ("#/texts/1", "#/texts/2")


def test_fallback_parser_and_incomplete_provenance_are_rejected(tmp_path) -> None:
    root = build_literature_fixture(tmp_path / "literature")
    state = sqlite3.connect(root / "state" / "pipeline.sqlite3")
    state.execute("UPDATE pipeline_documents SET parser_name='pymupdf'")
    state.commit()
    state.close()
    with pytest.raises(ProvenanceError, match="requires a Docling"):
        ProvenanceDocumentReader(root).load(DOCUMENT_ID)

    state = sqlite3.connect(root / "state" / "pipeline.sqlite3")
    state.execute("UPDATE pipeline_documents SET parser_name='docling'")
    state.commit()
    state.close()
    path = root / "parsed" / "json" / f"{safe_document_name(DOCUMENT_ID)}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["structured"]["texts"][1]["prov"][0].pop("bbox")
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ProvenanceError, match="No traceable paragraphs"):
        ProvenanceDocumentReader(root).load(DOCUMENT_ID)
