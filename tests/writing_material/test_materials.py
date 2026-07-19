from __future__ import annotations

import json
from dataclasses import replace

import pytest

from knowledgehub.core.hashing import sha256_text
from knowledgehub.writing_rag.materials import (
    RISK_FLAGS,
    ClassificationItem,
    MaterialValidationError,
    ProposedSpan,
    parse_abstraction_response,
    parse_classification_response,
    resolve_sentence_selection,
    validate_exact_span,
    validate_stored_record,
)
from knowledgehub.writing_rag.provenance import (
    ProvenanceDocumentReader,
    _segments,
    _sentences,
)

from .helpers import (
    DOCUMENT_ID,
    PARAGRAPH_TEXT,
    build_literature_fixture,
)


def _classification_decision(
    *selected_categories: str,
    confidence: float = 0.9,
    risk_decisions: dict[str, bool] | None = None,
) -> dict[str, object]:
    return {
        "category_decisions": {category: True for category in selected_categories},
        "claim_strength": "moderate",
        "risk_flag_decisions": risk_decisions
        if risk_decisions is not None
        else {flag: False for flag in RISK_FLAGS},
        "confidence": confidence,
    }


def test_closed_schema_and_exact_span_gate(tmp_path) -> None:
    document = ProvenanceDocumentReader(build_literature_fixture(tmp_path / "literature")).load(
        DOCUMENT_ID
    )
    paragraph = document.paragraphs[0]
    end = PARAGRAPH_TEXT.index(". ") + 1
    span = ProposedSpan(0, end, PARAGRAPH_TEXT[:end])
    item = ClassificationItem(
        paragraph_id=paragraph.paragraph_id,
        category="prior_work_limitation",
        sentence_ids=(paragraph.sentences[0].sentence_id,),
        claim_strength="moderate",
        risk_flags=(),
        confidence=0.9,
    )
    evidence = validate_exact_span(
        paragraph,
        item,
        span,
        document=document,
        provider="fake",
        model="fake-model",
        prompt_version="prompt-v1",
        prompt_hash="p" * 64,
        request_hash="q" * 64,
        response_hash="r" * 64,
    )
    assert evidence.original_text == PARAGRAPH_TEXT[:end]
    assert evidence.page_start == 1
    assert evidence.zotero_item_key == "ITEMKEY"
    assert evidence.sentence_ids

    decisions = {flag: flag == "vague_claim" for flag in RISK_FLAGS}
    abstraction = {
        "schema_version": "abstraction-v7",
        "strategies": [
            {
                "category_evidence_decisions": {
                    evidence.category: {evidence.evidence_id: True}
                },
                "label": "Scoped limitation",
                "description": "State a bounded limitation.",
                "steps": ["Name the scope"],
                "applicability": "Research-gap positioning",
                "claim_strength_guidance": "Keep the claim scoped.",
                "explanation_zh": "限定范围。",
                "explanation_en": "Bound the scope.",
                "risk_flag_decisions": decisions,
                "language": "en",
                "quality_score": 0.9,
            }
        ],
        "templates": [],
        "phrases": [],
    }
    strategies, _, _ = parse_abstraction_response(
        abstraction,
        {evidence.evidence_id: evidence},
        provider="fake",
        model="fake-model",
        prompt_version="prompt-v2",
        prompt_hash="p" * 64,
        request_hash="q" * 64,
        response_hash="r" * 64,
    )
    assert strategies[0].risk_flags == ("vague_claim",)

    malformed = json.loads(json.dumps(abstraction))
    malformed["strategies"][0]["risk_flag_decisions"].pop("vague_claim")
    with pytest.raises(MaterialValidationError, match="not closed-world"):
        parse_abstraction_response(
            malformed,
            {evidence.evidence_id: evidence},
            provider="fake",
            model="fake-model",
            prompt_version="prompt-v2",
            prompt_hash="p" * 64,
            request_hash="q" * 64,
            response_hash="r" * 64,
        )

    unknown_reference = json.loads(json.dumps(abstraction))
    unknown_reference["strategies"][0]["category_evidence_decisions"] = {
        evidence.category: {"evidence:unknown": True}
    }
    with pytest.raises(MaterialValidationError, match="unknown evidence"):
        parse_abstraction_response(
            unknown_reference,
            {evidence.evidence_id: evidence},
            provider="fake",
            model="fake-model",
            prompt_version="prompt-v3",
            prompt_hash="p" * 64,
            request_hash="q" * 64,
            response_hash="r" * 64,
        )

    false_reference = json.loads(json.dumps(abstraction))
    false_reference["strategies"][0]["category_evidence_decisions"][evidence.category][
        evidence.evidence_id
    ] = False
    with pytest.raises(MaterialValidationError, match="values must be true"):
        parse_abstraction_response(
            false_reference,
            {evidence.evidence_id: evidence},
            provider="fake",
            model="fake-model",
            prompt_version="prompt-v3",
            prompt_hash="p" * 64,
            request_hash="q" * 64,
            response_hash="r" * 64,
        )

    empty_reference = json.loads(json.dumps(abstraction))
    empty_reference["strategies"][0]["category_evidence_decisions"] = {
        evidence.category: {}
    }
    with pytest.raises(MaterialValidationError, match="must select evidence"):
        parse_abstraction_response(
            empty_reference,
            {evidence.evidence_id: evidence},
            provider="fake",
            model="fake-model",
            prompt_version="prompt-v3",
            prompt_hash="p" * 64,
            request_hash="q" * 64,
            response_hash="r" * 64,
        )

    unsupported_category = json.loads(json.dumps(abstraction))
    unsupported_category["strategies"][0]["category_evidence_decisions"] = {
        "gap_identification": {evidence.evidence_id: True}
    }
    with pytest.raises(MaterialValidationError, match="mismatched evidence"):
        parse_abstraction_response(
            unsupported_category,
            {evidence.evidence_id: evidence},
            provider="fake",
            model="fake-model",
            prompt_version="prompt-v3",
            prompt_hash="p" * 64,
            request_hash="q" * 64,
            response_hash="r" * 64,
        )

    duplicate_payload = json.loads(json.dumps(abstraction))
    duplicate_payload["strategies"].append(
        json.loads(json.dumps(duplicate_payload["strategies"][0]))
    )
    with pytest.raises(MaterialValidationError, match="duplicate strategy payload"):
        parse_abstraction_response(
            duplicate_payload,
            {evidence.evidence_id: evidence},
            provider="fake",
            model="fake-model",
            prompt_version="prompt-v4",
            prompt_hash="p" * 64,
            request_hash="q" * 64,
            response_hash="r" * 64,
        )

    for legacy_version in (
        "classification-v1",
        "classification-v2",
        "classification-v3",
        "classification-v4",
        "classification-v5",
    ):
        legacy = json.loads(json.dumps(evidence.to_dict()))
        legacy["response_schema_version"] = legacy_version
        validate_stored_record("evidence", legacy)

    with pytest.raises(MaterialValidationError, match="does not exactly match"):
        validate_exact_span(
            paragraph,
            item,
            replace(span, original_text="model rewrite"),
            document=document,
            provider="fake",
            model="fake-model",
            prompt_version="prompt-v1",
            prompt_hash="p" * 64,
            request_hash="q" * 64,
            response_hash="r" * 64,
        )


def test_classification_rejects_unknown_fields() -> None:
    with pytest.raises(MaterialValidationError, match="closed-world"):
        parse_classification_response(
            {"schema_version": "classification-v9", "items": {}, "surprise": True}
        )


def test_classification_v9_rejects_sentence_id_outside_request_batch() -> None:
    with pytest.raises(MaterialValidationError, match="not in the request batch"):
        parse_classification_response(
            {
                "schema_version": "classification-v9",
                "items": {
                    "sentence:not-in-request": _classification_decision("prior_work_limitation")
                },
            },
            sentence_lookup={"sentence:allowed": "paragraph:fixture"},
        )


def test_classification_v9_requires_complete_boolean_risk_map() -> None:
    decisions = {flag: False for flag in RISK_FLAGS}
    decisions.pop("vague_claim")
    with pytest.raises(MaterialValidationError, match="not closed-world"):
        parse_classification_response(
            {
                "schema_version": "classification-v9",
                "items": {
                    "sentence:allowed": _classification_decision(
                        "prior_work_limitation", risk_decisions=decisions
                    )
                },
            },
            sentence_lookup={"sentence:allowed": "paragraph:fixture"},
        )


def test_classification_v9_rejects_legacy_repeatable_array_shape() -> None:
    with pytest.raises(MaterialValidationError, match="bounded object"):
        parse_classification_response(
            {"schema_version": "classification-v9", "items": []},
            sentence_lookup={"sentence:allowed": "paragraph:fixture"},
        )


def test_classification_v9_derives_span_only_from_selected_source_sentence_id(
    tmp_path,
) -> None:
    document = ProvenanceDocumentReader(build_literature_fixture(tmp_path / "literature")).load(
        DOCUMENT_ID
    )
    paragraph = document.paragraphs[0]
    response = parse_classification_response(
        {
            "schema_version": "classification-v9",
            "items": {
                paragraph.sentences[0].sentence_id: _classification_decision(
                    "prior_work_limitation"
                )
            },
        },
        sentence_lookup={
            sentence.sentence_id: paragraph.paragraph_id for sentence in paragraph.sentences
        },
    )
    span = resolve_sentence_selection(paragraph, response.items[0])
    assert span.start == paragraph.sentences[0].start
    assert span.end == paragraph.sentences[0].end
    assert span.original_text == paragraph.text[span.start : span.end]

    unknown = replace(response.items[0], sentence_ids=("sentence:not-in-source",))
    with pytest.raises(MaterialValidationError, match="not in the source paragraph"):
        resolve_sentence_selection(paragraph, unknown)

    reordered = replace(
        response.items[0],
        sentence_ids=tuple(sentence.sentence_id for sentence in reversed(paragraph.sentences)),
    )
    with pytest.raises(MaterialValidationError, match="source-ordered and contiguous"):
        resolve_sentence_selection(paragraph, reordered)


def test_classification_v9_requires_nonempty_known_true_category_map() -> None:
    unknown = _classification_decision("unknown_category")
    with pytest.raises(MaterialValidationError, match="not closed-world"):
        parse_classification_response(
            {"schema_version": "classification-v9", "items": {"sentence:one": unknown}},
            sentence_lookup={"sentence:one": "paragraph:fixture"},
        )

    false_value = _classification_decision("prior_work_limitation")
    false_value["category_decisions"] = {"prior_work_limitation": False}
    with pytest.raises(MaterialValidationError, match="must be true"):
        parse_classification_response(
            {"schema_version": "classification-v9", "items": {"sentence:one": false_value}},
            sentence_lookup={"sentence:one": "paragraph:fixture"},
        )


def test_classification_v9_rejects_empty_and_expands_multilabel_decision() -> None:
    with pytest.raises(MaterialValidationError, match="select a category"):
        parse_classification_response(
            {
                "schema_version": "classification-v9",
                "items": {"sentence:one": _classification_decision()},
            },
            sentence_lookup={"sentence:one": "paragraph:fixture"},
        )

    response = parse_classification_response(
        {
            "schema_version": "classification-v9",
            "items": {
                "sentence:one": _classification_decision(
                    "gap_identification", "prior_work_limitation"
                )
            },
        },
        sentence_lookup={"sentence:one": "paragraph:fixture"},
    )
    assert [item.category for item in response.items] == [
        "gap_identification",
        "prior_work_limitation",
    ]


def test_exact_span_accepts_mapped_region_and_rejects_segment_gap(tmp_path) -> None:
    document = ProvenanceDocumentReader(build_literature_fixture(tmp_path / "literature")).load(
        DOCUMENT_ID
    )
    paragraph = document.paragraphs[0]
    segments = _segments(
        {
            "self_ref": "#/texts/1",
            "prov": [
                {
                    "page_no": 1,
                    "bbox": {"l": 0, "t": 0, "r": 1, "b": 1},
                    "charspan": [0, 20],
                },
                {
                    "page_no": 2,
                    "bbox": {"l": 0, "t": 0, "r": 1, "b": 1},
                    "charspan": [21, len(paragraph.text)],
                },
            ],
        },
        paragraph.text,
    )
    paragraph = replace(paragraph, segments=segments)
    safe_span = ProposedSpan(0, 10, paragraph.text[:10])
    safe_item = ClassificationItem(
        paragraph.paragraph_id,
        "prior_work_limitation",
        (paragraph.sentences[0].sentence_id,),
        "moderate",
        (),
        0.9,
    )
    evidence = validate_exact_span(
        paragraph,
        safe_item,
        safe_span,
        document=document,
        provider="fake",
        model="fake",
        prompt_version="v1",
        prompt_hash="p" * 64,
        request_hash="q" * 64,
        response_hash="r" * 64,
    )
    assert evidence.original_text == paragraph.text[:10]

    crossing = ProposedSpan(18, 23, paragraph.text[18:23])
    with pytest.raises(MaterialValidationError, match="not completely covered"):
        validate_exact_span(
            paragraph,
            safe_item,
            crossing,
            document=document,
            provider="fake",
            model="fake",
            prompt_version="v1",
            prompt_hash="p" * 64,
            request_hash="q" * 64,
            response_hash="r" * 64,
        )


def test_repeated_text_requires_an_exact_disambiguating_offset(tmp_path) -> None:
    document = ProvenanceDocumentReader(build_literature_fixture(tmp_path / "literature")).load(
        DOCUMENT_ID
    )
    original = document.paragraphs[0]
    text = "Repeat claim. Repeat claim."
    segments = _segments(
        {
            "self_ref": "#/texts/repeated",
            "prov": [
                {
                    "page_no": 1,
                    "bbox": {"l": 0, "t": 0, "r": 1, "b": 1},
                    "charspan": [0, len(text)],
                }
            ],
        },
        text,
    )
    paragraph = replace(
        original,
        text=text,
        text_hash=sha256_text(text),
        segments=segments,
        sentences=tuple(_sentences(original.paragraph_id, text)),
    )
    second_start = text.rindex("Repeat claim.")
    span = ProposedSpan(second_start, len(text), "Repeat claim.")
    item = ClassificationItem(
        paragraph.paragraph_id,
        "prior_work_limitation",
        (paragraph.sentences[-1].sentence_id,),
        "moderate",
        (),
        0.9,
    )
    evidence = validate_exact_span(
        paragraph,
        item,
        span,
        document=document,
        provider="fake",
        model="fake",
        prompt_version="v1",
        prompt_hash="p" * 64,
        request_hash="q" * 64,
        response_hash="r" * 64,
    )
    assert evidence.char_start == second_start

    with pytest.raises(MaterialValidationError, match="does not exactly match"):
        validate_exact_span(
            paragraph,
            item,
            replace(span, start=1, end=14),
            document=document,
            provider="fake",
            model="fake",
            prompt_version="v1",
            prompt_hash="p" * 64,
            request_hash="q" * 64,
            response_hash="r" * 64,
        )
    with pytest.raises(MaterialValidationError, match="closed-world"):
        parse_classification_response(
            {
                "schema_version": "classification-v9",
                "items": {
                    paragraph.sentences[0].sentence_id: {
                        **_classification_decision("prior_work_limitation"),
                        "original_text": "Repeat claim.",
                    }
                },
            },
            sentence_lookup={
                sentence.sentence_id: paragraph.paragraph_id for sentence in paragraph.sentences
            },
        )


def test_unicode_and_newline_matching_is_exact_and_not_normalized(tmp_path) -> None:
    document = ProvenanceDocumentReader(build_literature_fixture(tmp_path / "literature")).load(
        DOCUMENT_ID
    )
    original = document.paragraphs[0]
    text = "Prior work is limited—\nunder shift."
    paragraph = replace(
        original,
        text=text,
        text_hash=sha256_text(text),
        segments=_segments(
            {
                "self_ref": "#/texts/unicode",
                "prov": [
                    {
                        "page_no": 1,
                        "bbox": {"l": 0, "t": 0, "r": 1, "b": 1},
                        "charspan": [0, len(text)],
                    }
                ],
            },
            text,
        ),
        sentences=tuple(_sentences(original.paragraph_id, text)),
    )
    span = ProposedSpan(0, len(text), text)
    item = ClassificationItem(
        paragraph.paragraph_id,
        "prior_work_limitation",
        tuple(sentence.sentence_id for sentence in paragraph.sentences),
        "moderate",
        (),
        0.9,
    )
    evidence = validate_exact_span(
        paragraph,
        item,
        span,
        document=document,
        provider="fake",
        model="fake",
        prompt_version="v1",
        prompt_hash="p" * 64,
        request_hash="q" * 64,
        response_hash="r" * 64,
    )
    assert evidence.original_text == text
    with pytest.raises(MaterialValidationError, match="does not exactly match"):
        validate_exact_span(
            paragraph,
            item,
            replace(span, original_text="Prior work is limited— under shift."),
            document=document,
            provider="fake",
            model="fake",
            prompt_version="v1",
            prompt_hash="p" * 64,
            request_hash="q" * 64,
            response_hash="r" * 64,
        )
