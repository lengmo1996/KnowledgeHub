"""Strict writing-material schemas, exact-span validation, scoring and deduplication."""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Mapping, Sequence

from knowledgehub.core.hashing import sha256_json, sha256_text

TAXONOMY_VERSION = "writing-taxonomy-v1"
CLASSIFICATION_SCHEMA_VERSION = "classification-v9"
SUPPORTED_CLASSIFICATION_SCHEMA_VERSIONS = frozenset(
    {
        "classification-v1",
        "classification-v2",
        "classification-v3",
        "classification-v4",
        "classification-v5",
        "classification-v6",
        "classification-v7",
        "classification-v8",
        CLASSIFICATION_SCHEMA_VERSION,
    }
)
ABSTRACTION_SCHEMA_VERSION = "abstraction-v7"
SUPPORTED_ABSTRACTION_SCHEMA_VERSIONS = frozenset(
    {
        "abstraction-v1",
        "abstraction-v2",
        "abstraction-v3",
        "abstraction-v4",
        "abstraction-v5",
        "abstraction-v6",
        ABSTRACTION_SCHEMA_VERSION,
    }
)
EVIDENCE_SCHEMA_VERSION = "evidence-v1"
MATERIAL_SCHEMA_VERSION = "writing-material-v1"
QUALITY_POLICY_VERSION = "quality-v1"

TAXONOMY = (
    "concept_introduction",
    "concept_definition",
    "context_setting",
    "importance_claim",
    "problem_statement",
    "gap_identification",
    "prior_work_limitation",
    "motivation",
    "incremental_novelty_positioning",
    "contribution_summary",
    "design_rationale",
    "mechanism_explanation",
    "transition",
    "comparison_with_prior_work",
    "result_reporting",
    "result_interpretation",
    "ablation_interpretation",
    "limitation_acknowledgment",
    "future_work",
)

MVP_TAXONOMY = (
    "context_setting",
    "importance_claim",
    "problem_statement",
    "gap_identification",
    "prior_work_limitation",
    "motivation",
    "incremental_novelty_positioning",
    "contribution_summary",
    "result_reporting",
    "result_interpretation",
    "limitation_acknowledgment",
    "future_work",
)

RISK_FLAGS = (
    "unsupported_superlative",
    "exaggerated_novelty",
    "vague_claim",
    "missing_comparison",
    "causal_overclaim",
)
CLAIM_STRENGTHS = ("cautious", "moderate", "strong")
ASSET_TYPES = ("strategy", "template", "phrase")

_NAMESPACE = uuid.UUID("c027b808-9af1-5bc0-930d-402eb61b78c2")
_SUPERLATIVE = re.compile(r"\b(?:best|first|only|unprecedented|state[- ]of[- ]the[- ]art)\b", re.I)
_NOVELTY = re.compile(r"\b(?:novel|groundbreaking|revolutionary|breakthrough)\b", re.I)
_VAGUE = re.compile(r"\b(?:significant|substantial|considerable|remarkable)\b", re.I)
_CAUSAL = re.compile(r"\b(?:causes?|proves?|ensures?|guarantees?|leads? to)\b", re.I)
_COMPARISON = re.compile(r"\b(?:than|compared|relative to|versus|baseline|prior work)\b", re.I)


class MaterialValidationError(ValueError):
    """A closed-world schema or provenance validation failure."""


@dataclass(frozen=True, slots=True)
class SourceSpan:
    self_ref: str
    source_start: int
    source_end: int
    paragraph_start: int
    paragraph_end: int
    page_no: int
    bbox: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    document_id: str
    zotero_item_key: str
    attachment_key: str
    language: str
    source_content_fingerprint: str
    parse_fingerprint: str
    parser_name: str
    parser_version: str
    provenance_coverage: float
    section_id: str
    section_title: str
    section_path: tuple[str, ...]
    section_family: str
    page_start: int
    page_end: int
    paragraph_id: str
    sentence_ids: tuple[str, ...]
    char_start: int
    char_end: int
    source_spans: tuple[SourceSpan, ...]
    original_text: str
    source_paragraph_hash: str
    category: str
    claim_strength: str
    risk_flags: tuple[str, ...]
    risk_flag_sources: Mapping[str, str]
    confidence: float
    quality_score: float
    validation_status: str
    analyzer_provider: str
    analyzer_model: str
    prompt_version: str
    prompt_hash: str
    analyzer_request_hash: str
    analyzer_response_hash: str
    response_schema_version: str
    taxonomy_version: str = TAXONOMY_VERSION
    schema_version: str = EVIDENCE_SCHEMA_VERSION
    cluster_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Strategy:
    strategy_id: str
    evidence_ids: tuple[str, ...]
    category: str
    label: str
    description: str
    steps: tuple[str, ...]
    applicability: str
    claim_strength_guidance: str
    explanation_zh: str
    explanation_en: str
    risk_flags: tuple[str, ...]
    language: str
    quality_score: float
    analyzer_provider: str
    analyzer_model: str
    prompt_version: str
    prompt_hash: str
    analyzer_request_hash: str
    analyzer_response_hash: str
    response_schema_version: str = ABSTRACTION_SCHEMA_VERSION
    taxonomy_version: str = TAXONOMY_VERSION
    schema_version: str = MATERIAL_SCHEMA_VERSION
    asset_type: str = "strategy"
    cluster_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TemplateSlot:
    name: str
    semantic_type: str
    required: bool


@dataclass(frozen=True, slots=True)
class Template:
    template_id: str
    evidence_ids: tuple[str, ...]
    category: str
    template_text: str
    slots: tuple[TemplateSlot, ...]
    constraints: tuple[str, ...]
    claim_strength_guidance: str
    language: str
    quality_score: float
    analyzer_provider: str
    analyzer_model: str
    prompt_version: str
    prompt_hash: str
    analyzer_request_hash: str
    analyzer_response_hash: str
    response_schema_version: str = ABSTRACTION_SCHEMA_VERSION
    taxonomy_version: str = TAXONOMY_VERSION
    schema_version: str = MATERIAL_SCHEMA_VERSION
    asset_type: str = "template"
    cluster_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Phrase:
    phrase_id: str
    evidence_ids: tuple[str, ...]
    category: str
    text: str
    function: str
    position: str
    register: str
    claim_strength: str
    constraints: tuple[str, ...]
    language: str
    quality_score: float
    analyzer_provider: str
    analyzer_model: str
    prompt_version: str
    prompt_hash: str
    analyzer_request_hash: str
    analyzer_response_hash: str
    response_schema_version: str = ABSTRACTION_SCHEMA_VERSION
    taxonomy_version: str = TAXONOMY_VERSION
    schema_version: str = MATERIAL_SCHEMA_VERSION
    asset_type: str = "phrase"
    cluster_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_STORED_FIELDS = {
    "evidence": frozenset(field.name for field in fields(Evidence)),
    "strategy": frozenset(field.name for field in fields(Strategy)),
    "template": frozenset(field.name for field in fields(Template)),
    "phrase": frozenset(field.name for field in fields(Phrase)),
}
_SOURCE_SPAN_FIELDS = frozenset(field.name for field in fields(SourceSpan))


@dataclass(frozen=True, slots=True)
class ProposedSpan:
    start: int
    end: int
    original_text: str


@dataclass(frozen=True, slots=True)
class ClassificationItem:
    paragraph_id: str
    category: str
    sentence_ids: tuple[str, ...]
    claim_strength: str
    risk_flags: tuple[str, ...]
    confidence: float


@dataclass(frozen=True, slots=True)
class ClassificationResponse:
    items: tuple[ClassificationItem, ...]
    schema_version: str = CLASSIFICATION_SCHEMA_VERSION


def parse_classification_response(
    value: Mapping[str, Any],
    *,
    enabled_categories: Sequence[str] = MVP_TAXONOMY,
    sentence_lookup: Mapping[str, str] | None = None,
) -> ClassificationResponse:
    _closed(value, {"schema_version", "items"}, "classification response")
    if value.get("schema_version") != CLASSIFICATION_SCHEMA_VERSION:
        raise MaterialValidationError("unsupported classification schema version")
    raw_items = value.get("items")
    if not isinstance(raw_items, Mapping) or len(raw_items) > 200:
        raise MaterialValidationError("classification items must be a bounded object")
    if sentence_lookup is None:
        raise MaterialValidationError("classification sentence lookup is required")
    enabled = set(enabled_categories)
    items: list[ClassificationItem] = []
    for sentence_key, raw in raw_items.items():
        sentence_id = _nonempty(sentence_key, "sentence_id", 512)
        paragraph_id = sentence_lookup.get(sentence_id)
        if paragraph_id is None:
            raise MaterialValidationError("classification sentence_id is not in the request batch")
        if not isinstance(raw, Mapping):
            raise MaterialValidationError("classification decision must be an object")
        _closed(
            raw,
            {
                "category_decisions",
                "claim_strength",
                "risk_flag_decisions",
                "confidence",
            },
            "classification decision",
        )
        category_decisions = raw.get("category_decisions")
        if not isinstance(category_decisions, Mapping):
            raise MaterialValidationError("category_decisions must be an object")
        unknown_categories = set(category_decisions) - enabled
        if unknown_categories:
            raise MaterialValidationError(
                "category_decisions is not closed-world: unknown "
                + ", ".join(sorted(unknown_categories))
            )
        if not category_decisions:
            raise MaterialValidationError("classification decision must select a category")
        if any(selected is not True for selected in category_decisions.values()):
            raise MaterialValidationError("selected category decisions must be true")
        categories = [category for category in enabled_categories if category in category_decisions]
        claim_strength = _enum(raw.get("claim_strength"), set(CLAIM_STRENGTHS), "claim_strength")
        confidence = _score(raw.get("confidence"), "confidence")
        flags = _risk_flag_decisions(raw.get("risk_flag_decisions"))
        for category in categories:
            items.append(
                ClassificationItem(
                    paragraph_id=paragraph_id,
                    category=category,
                    sentence_ids=(sentence_id,),
                    claim_strength=claim_strength,
                    risk_flags=flags,
                    confidence=confidence,
                )
            )
            if len(items) > 200:
                raise MaterialValidationError("classification decisions exceed the bounded limit")
    return ClassificationResponse(items=tuple(items))


def resolve_sentence_selection(paragraph: Any, item: ClassificationItem) -> ProposedSpan:
    """Derive one exact source span from ordered, contiguous authoritative sentence IDs."""

    if item.paragraph_id != paragraph.paragraph_id:
        raise MaterialValidationError("classification paragraph_id mismatch")
    positions = {
        sentence.sentence_id: (index, sentence)
        for index, sentence in enumerate(paragraph.sentences)
    }
    selected: list[tuple[int, Any]] = []
    for sentence_id in item.sentence_ids:
        value = positions.get(sentence_id)
        if value is None:
            raise MaterialValidationError(
                "classification sentence_id is not in the source paragraph"
            )
        selected.append(value)
    indexes = [index for index, _sentence in selected]
    if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
        raise MaterialValidationError(
            "classification sentence_ids must be source-ordered and contiguous"
        )
    start = selected[0][1].start
    end = selected[-1][1].end
    if start < 0 or end <= start or end > len(paragraph.text):
        raise MaterialValidationError("authoritative sentence range is invalid")
    original_text = paragraph.text[start:end]
    if not original_text:
        raise MaterialValidationError("authoritative sentence range is empty")
    return ProposedSpan(start=start, end=end, original_text=original_text)


def validate_exact_span(
    paragraph: Any,
    item: ClassificationItem,
    span: ProposedSpan,
    *,
    document: Any,
    provider: str,
    model: str,
    prompt_version: str,
    prompt_hash: str,
    request_hash: str,
    response_hash: str,
) -> Evidence:
    """Validate one exact slice and map it to immutable Docling provenance."""

    if item.paragraph_id != paragraph.paragraph_id:
        raise MaterialValidationError("classification paragraph_id mismatch")
    if span.end > len(paragraph.text):
        raise MaterialValidationError("span is outside source paragraph")
    if paragraph.text[span.start : span.end] != span.original_text:
        raise MaterialValidationError("span text does not exactly match source paragraph")
    if sha256_text(paragraph.text) != paragraph.text_hash:
        raise MaterialValidationError("source paragraph hash changed")
    source_spans = paragraph.map_range(span.start, span.end)
    if not source_spans:
        raise MaterialValidationError("span has no source provenance")
    if any(not value.bbox or value.page_no <= 0 for value in source_spans):
        raise MaterialValidationError("span provenance lacks page or bbox")
    covered = sum(value.paragraph_end - value.paragraph_start for value in source_spans)
    if covered != span.end - span.start:
        raise MaterialValidationError("span is not completely covered by source provenance")
    sentence_ids = tuple(
        sentence.sentence_id
        for sentence in paragraph.sentences
        if sentence.start < span.end and sentence.end > span.start
    )
    if not sentence_ids:
        raise MaterialValidationError("span does not overlap a sentence")
    pages = [value.page_no for value in source_spans]
    flags = tuple(
        sorted(set(item.risk_flags) | set(detect_risk_flags(span.original_text, item.category)))
    )
    risk_sources = {
        flag: "model_assessment" if flag in item.risk_flags else "deterministic_heuristic"
        for flag in flags
    }
    identity = {
        "document_id": document.document_id,
        "parse_fingerprint": document.parse_fingerprint,
        "paragraph_id": paragraph.paragraph_id,
        "start": span.start,
        "end": span.end,
        "text_hash": sha256_text(span.original_text),
        "category": item.category,
        "taxonomy": TAXONOMY_VERSION,
    }
    quality = quality_score(
        confidence=item.confidence,
        text=span.original_text,
        category=item.category,
        section_family=paragraph.section_family,
        risk_flags=flags,
    )
    return Evidence(
        evidence_id=f"evidence:{sha256_json(identity)}",
        document_id=document.document_id,
        zotero_item_key=document.zotero_item_key,
        attachment_key=document.attachment_key,
        language=detect_language(span.original_text),
        source_content_fingerprint=document.source_content_fingerprint,
        parse_fingerprint=document.parse_fingerprint,
        parser_name=document.parser_name,
        parser_version=document.parser_version,
        provenance_coverage=document.coverage_for({paragraph.section_family}),
        section_id=paragraph.section_id,
        section_title=paragraph.section_title,
        section_path=paragraph.section_path,
        section_family=paragraph.section_family,
        page_start=min(pages),
        page_end=max(pages),
        paragraph_id=paragraph.paragraph_id,
        sentence_ids=sentence_ids,
        char_start=span.start,
        char_end=span.end,
        source_spans=tuple(source_spans),
        original_text=span.original_text,
        source_paragraph_hash=paragraph.text_hash,
        category=item.category,
        claim_strength=item.claim_strength,
        risk_flags=flags,
        risk_flag_sources=risk_sources,
        confidence=item.confidence,
        quality_score=quality,
        validation_status="validated" if quality >= 0.65 else "rejected_candidate",
        analyzer_provider=provider,
        analyzer_model=model,
        prompt_version=prompt_version,
        prompt_hash=prompt_hash,
        analyzer_request_hash=request_hash,
        analyzer_response_hash=response_hash,
        response_schema_version=CLASSIFICATION_SCHEMA_VERSION,
    )


def detect_language(text: str) -> str:
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk > latin * 0.2:
        return "zh"
    return "en" if latin else "und"


def detect_risk_flags(text: str, category: str) -> tuple[str, ...]:
    flags: set[str] = set()
    if _SUPERLATIVE.search(text):
        flags.add("unsupported_superlative")
    if _NOVELTY.search(text):
        flags.add("exaggerated_novelty")
    if _VAGUE.search(text) and not re.search(r"\d", text):
        flags.add("vague_claim")
    if _CAUSAL.search(text):
        flags.add("causal_overclaim")
    if category == "incremental_novelty_positioning" and not _COMPARISON.search(text):
        flags.add("missing_comparison")
    return tuple(sorted(flags))


def quality_score(
    *, confidence: float, text: str, category: str, section_family: str, risk_flags: Sequence[str]
) -> float:
    words = re.findall(r"[A-Za-z0-9-]+|[\u3400-\u4dbf\u4e00-\u9fff]", text)
    transferability = 1.0 if 8 <= len(words) <= 80 else 0.6
    context_independence = 0.6 if re.match(r"^(?:This|It|These|Those|They)\b", text) else 1.0
    completeness = (
        1.0 if text.rstrip().endswith((".", "?", "!", "\u3002", "\uff1f", "\uff01")) else 0.7
    )
    language_quality = 1.0 if len(text.strip()) >= 20 else 0.5
    expected = {
        "result_reporting": "experiment",
        "result_interpretation": "experiment",
        "limitation_acknowledgment": "conclusion",
        "future_work": "conclusion",
    }.get(category)
    consistency = 1.0 if expected is None or section_family == expected else 0.5
    value = (
        0.20 * confidence
        + 0.20 * transferability
        + 0.15 * context_independence
        + 0.15 * completeness
        + 0.15 * language_quality
        + 0.15 * consistency
        - min(0.25, 0.05 * len(set(risk_flags)))
    )
    return round(max(0.0, min(1.0, value)), 6)


def parse_abstraction_response(
    value: Mapping[str, Any],
    evidences: Mapping[str, Evidence],
    *,
    provider: str,
    model: str,
    prompt_version: str,
    prompt_hash: str,
    request_hash: str,
    response_hash: str,
) -> tuple[list[Strategy], list[Template], list[Phrase]]:
    """Validate an abstraction response that cannot contain source-text fields."""

    _closed(value, {"schema_version", "strategies", "templates", "phrases"}, "abstraction response")
    if value.get("schema_version") != ABSTRACTION_SCHEMA_VERSION:
        raise MaterialValidationError("unsupported abstraction schema version")
    metadata = {
        "analyzer_provider": provider,
        "analyzer_model": model,
        "prompt_version": prompt_version,
        "prompt_hash": prompt_hash,
        "analyzer_request_hash": request_hash,
        "analyzer_response_hash": response_hash,
    }
    strategies = [
        _parse_strategy(item, evidences, metadata) for item in _array(value, "strategies", 200)
    ]
    templates = [
        _parse_template(item, evidences, metadata) for item in _array(value, "templates", 200)
    ]
    phrases = [_parse_phrase(item, evidences, metadata) for item in _array(value, "phrases", 400)]
    for label, records in (
        ("strategy", strategies),
        ("template", templates),
        ("phrase", phrases),
    ):
        identifiers = [_record_id(record) for record in records]
        if len(set(identifiers)) != len(identifiers):
            raise MaterialValidationError(f"duplicate {label} payload")
    return strategies, templates, phrases


def validate_stored_record(asset_type: str, value: Mapping[str, Any]) -> None:
    """Revalidate one serialized run asset before review or indexing.

    Provider responses are validated when they are created, but run JSONL is a
    durable trust boundary of its own.  This validator deliberately accepts
    only the exact v1 serialized dataclass shape and fails closed on version,
    provenance, trace or type drift.
    """

    if asset_type not in {"evidence", *ASSET_TYPES}:
        raise MaterialValidationError(f"unsupported stored asset type: {asset_type}")
    if not isinstance(value, Mapping):
        raise MaterialValidationError(f"stored {asset_type} must be an object")
    _closed(value, set(_STORED_FIELDS[asset_type]), f"stored {asset_type}")
    if asset_type == "evidence":
        _validate_stored_evidence(value)
    else:
        _validate_stored_material(asset_type, value)


def _validate_stored_evidence(value: Mapping[str, Any]) -> None:
    _prefixed(value.get("evidence_id"), "evidence:", "evidence_id")
    _prefixed(value.get("document_id"), "zotero:", "document_id")
    for key in (
        "zotero_item_key",
        "attachment_key",
        "source_content_fingerprint",
        "parse_fingerprint",
        "parser_name",
        "parser_version",
        "section_id",
        "section_title",
        "section_family",
        "paragraph_id",
        "original_text",
        "source_paragraph_hash",
    ):
        _nonempty(value.get(key), key, 5000 if key == "original_text" else 512)
    _enum(value.get("language"), {"en", "zh", "und"}, "language")
    _enum(value.get("category"), set(TAXONOMY), "category")
    _enum(value.get("claim_strength"), set(CLAIM_STRENGTHS), "claim_strength")
    _enum(
        value.get("validation_status"),
        {"validated", "rejected_candidate"},
        "validation_status",
    )
    _score(value.get("provenance_coverage"), "provenance_coverage")
    _score(value.get("confidence"), "confidence")
    _score(value.get("quality_score"), "quality_score")
    section_path = _stored_strings(value.get("section_path"), "section_path", 32, 500)
    sentence_ids = _stored_strings(value.get("sentence_ids"), "sentence_ids", 100, 256)
    if not section_path or not sentence_ids:
        raise MaterialValidationError("stored evidence lacks section or sentence identity")
    flags = _stored_string_enums(value.get("risk_flags"), set(RISK_FLAGS), "risk_flags", 5)
    sources = value.get("risk_flag_sources")
    if not isinstance(sources, Mapping) or set(sources) != set(flags):
        raise MaterialValidationError("risk_flag_sources must exactly match risk_flags")
    if any(
        source not in {"model_assessment", "deterministic_heuristic"} for source in sources.values()
    ):
        raise MaterialValidationError("invalid risk flag source")

    page_start = _stored_int(value.get("page_start"), "page_start", minimum=1)
    page_end = _stored_int(value.get("page_end"), "page_end", minimum=page_start)
    char_start = _stored_int(value.get("char_start"), "char_start", minimum=0)
    char_end = _stored_int(value.get("char_end"), "char_end", minimum=char_start + 1)
    original_text = str(value["original_text"])
    if char_end - char_start != len(original_text):
        raise MaterialValidationError("stored evidence range does not match original_text length")
    raw_spans = value.get("source_spans")
    if not isinstance(raw_spans, list) or not raw_spans or len(raw_spans) > 64:
        raise MaterialValidationError("source_spans must be a non-empty bounded array")
    cursor = char_start
    pages: list[int] = []
    for raw_span in raw_spans:
        if not isinstance(raw_span, Mapping):
            raise MaterialValidationError("source span must be an object")
        _closed(raw_span, set(_SOURCE_SPAN_FIELDS), "source span")
        _nonempty(raw_span.get("self_ref"), "source span self_ref", 512)
        source_start = _stored_int(raw_span.get("source_start"), "source_start", minimum=0)
        source_end = _stored_int(raw_span.get("source_end"), "source_end", minimum=source_start + 1)
        paragraph_start = _stored_int(
            raw_span.get("paragraph_start"), "paragraph_start", minimum=char_start
        )
        paragraph_end = _stored_int(
            raw_span.get("paragraph_end"),
            "paragraph_end",
            minimum=paragraph_start + 1,
        )
        if (
            paragraph_start != cursor
            or source_end - source_start != paragraph_end - paragraph_start
        ):
            raise MaterialValidationError("source spans do not provide contiguous exact coverage")
        if paragraph_end > char_end:
            raise MaterialValidationError("source span exceeds evidence range")
        bbox = raw_span.get("bbox")
        if not isinstance(bbox, Mapping) or not bbox:
            raise MaterialValidationError("source span bbox is missing")
        pages.append(_stored_int(raw_span.get("page_no"), "source span page_no", minimum=1))
        cursor = paragraph_end
    if cursor != char_end:
        raise MaterialValidationError("source spans do not cover the complete evidence range")
    if page_start != min(pages) or page_end != max(pages):
        raise MaterialValidationError("stored evidence page range differs from source spans")

    _validate_stored_trace(value, response_schema=SUPPORTED_CLASSIFICATION_SCHEMA_VERSIONS)
    if value.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise MaterialValidationError("unsupported stored evidence schema version")


def _validate_stored_material(asset_type: str, value: Mapping[str, Any]) -> None:
    _prefixed(value.get(f"{asset_type}_id"), f"{asset_type}:", f"{asset_type}_id")
    if value.get("asset_type") != asset_type:
        raise MaterialValidationError("stored material asset_type mismatch")
    evidence_ids = _stored_strings(value.get("evidence_ids"), "evidence_ids", 20, 256)
    if not evidence_ids or len(set(evidence_ids)) != len(evidence_ids):
        raise MaterialValidationError("stored material evidence_ids are empty or duplicated")
    for evidence_id in evidence_ids:
        _prefixed(evidence_id, "evidence:", "evidence_id")
    _enum(value.get("category"), set(TAXONOMY), "category")
    _enum(value.get("language"), {"en", "zh", "und"}, "language")
    _score(value.get("quality_score"), "quality_score")
    _validate_cluster(value.get("cluster_id"))
    _validate_stored_trace(value, response_schema=SUPPORTED_ABSTRACTION_SCHEMA_VERSIONS)
    if value.get("schema_version") != MATERIAL_SCHEMA_VERSION:
        raise MaterialValidationError("unsupported stored material schema version")

    if asset_type == "strategy":
        for key, maximum in (
            ("label", 160),
            ("description", 2000),
            ("applicability", 1000),
            ("claim_strength_guidance", 1000),
            ("explanation_zh", 2000),
            ("explanation_en", 2000),
        ):
            _nonempty(value.get(key), key, maximum)
        _stored_strings(value.get("steps"), "steps", 12, 300)
        _stored_string_enums(value.get("risk_flags"), set(RISK_FLAGS), "risk_flags", 5)
    elif asset_type == "template":
        _nonempty(value.get("template_text"), "template_text", 2000)
        _nonempty(value.get("claim_strength_guidance"), "claim_strength_guidance", 1000)
        _stored_strings(value.get("constraints"), "constraints", 20, 500)
        slots = value.get("slots")
        if not isinstance(slots, list) or len(slots) > 20:
            raise MaterialValidationError("slots must be a bounded array")
        for slot in slots:
            if not isinstance(slot, Mapping):
                raise MaterialValidationError("template slot must be an object")
            _closed(slot, {"name", "semantic_type", "required"}, "template slot")
            _nonempty(slot.get("name"), "slot name", 80)
            _nonempty(slot.get("semantic_type"), "semantic_type", 120)
            if not isinstance(slot.get("required"), bool):
                raise MaterialValidationError("template slot required must be boolean")
    else:
        for key, maximum in (
            ("text", 500),
            ("function", 300),
            ("position", 120),
            ("register", 120),
        ):
            _nonempty(value.get(key), key, maximum)
        _enum(value.get("claim_strength"), set(CLAIM_STRENGTHS), "claim_strength")
        _stored_strings(value.get("constraints"), "constraints", 20, 500)


def _validate_stored_trace(
    value: Mapping[str, Any], *, response_schema: str | frozenset[str]
) -> None:
    for key in (
        "analyzer_provider",
        "analyzer_model",
        "prompt_version",
        "prompt_hash",
        "analyzer_request_hash",
        "analyzer_response_hash",
    ):
        _nonempty(value.get(key), key, 256)
    supported = {response_schema} if isinstance(response_schema, str) else response_schema
    if value.get("response_schema_version") not in supported:
        raise MaterialValidationError("stored response schema version mismatch")
    if value.get("taxonomy_version") != TAXONOMY_VERSION:
        raise MaterialValidationError("unsupported stored taxonomy version")
    _validate_cluster(value.get("cluster_id"))


def _validate_cluster(value: Any) -> None:
    if value is not None:
        _prefixed(value, "cluster:", "cluster_id")


def _prefixed(value: Any, prefix: str, label: str) -> str:
    result = _nonempty(value, label, 512)
    if not result.startswith(prefix):
        raise MaterialValidationError(f"invalid {label}")
    return result


def _stored_int(value: Any, label: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise MaterialValidationError(f"invalid {label}")
    return value


def _stored_strings(value: Any, label: str, maximum: int, item_maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise MaterialValidationError(f"stored {label} must be an array")
    return _strings(value, label, maximum, item_maximum)


def _stored_string_enums(
    value: Any, allowed: set[str], label: str, maximum: int
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise MaterialValidationError(f"stored {label} must be an array")
    return _string_enums(value, allowed, label, maximum)


def _parse_strategy(
    value: Any, evidences: Mapping[str, Evidence], metadata: Mapping[str, str]
) -> Strategy:
    allowed = {
        "category_evidence_decisions",
        "label",
        "description",
        "steps",
        "applicability",
        "claim_strength_guidance",
        "explanation_zh",
        "explanation_en",
        "risk_flag_decisions",
        "language",
        "quality_score",
    }
    raw = _object(value, "strategy")
    _closed(raw, allowed, "strategy")
    category, evidence_ids = _category_evidence_ids(raw, evidences)
    payload = {key: raw.get(key) for key in sorted(allowed)}
    return Strategy(
        strategy_id=f"strategy:{sha256_json(payload)}",
        evidence_ids=evidence_ids,
        category=category,
        label=_nonempty(raw.get("label"), "label", 160),
        description=_nonempty(raw.get("description"), "description", 2000),
        steps=_strings(raw.get("steps"), "steps", 12, 300),
        applicability=_nonempty(raw.get("applicability"), "applicability", 1000),
        claim_strength_guidance=_nonempty(
            raw.get("claim_strength_guidance"), "claim_strength_guidance", 1000
        ),
        explanation_zh=_nonempty(raw.get("explanation_zh"), "explanation_zh", 2000),
        explanation_en=_nonempty(raw.get("explanation_en"), "explanation_en", 2000),
        risk_flags=_risk_flag_decisions(raw.get("risk_flag_decisions")),
        language=_enum(raw.get("language"), {"en", "zh", "und"}, "language"),
        quality_score=_score(raw.get("quality_score"), "quality_score"),
        **metadata,
    )


def _parse_template(
    value: Any, evidences: Mapping[str, Evidence], metadata: Mapping[str, str]
) -> Template:
    allowed = {
        "category_evidence_decisions",
        "template_text",
        "slots",
        "constraints",
        "claim_strength_guidance",
        "language",
        "quality_score",
    }
    raw = _object(value, "template")
    _closed(raw, allowed, "template")
    category, evidence_ids = _category_evidence_ids(raw, evidences)
    slots_raw = _array(raw, "slots", 20)
    slots: list[TemplateSlot] = []
    for item in slots_raw:
        slot = _object(item, "template slot")
        _closed(slot, {"name", "semantic_type", "required"}, "template slot")
        required = slot.get("required")
        if not isinstance(required, bool):
            raise MaterialValidationError("template slot required must be boolean")
        slots.append(
            TemplateSlot(
                _nonempty(slot.get("name"), "slot name", 80),
                _nonempty(slot.get("semantic_type"), "semantic_type", 120),
                required,
            )
        )
    payload = {key: raw.get(key) for key in sorted(allowed)}
    return Template(
        template_id=f"template:{sha256_json(payload)}",
        evidence_ids=evidence_ids,
        category=category,
        template_text=_nonempty(raw.get("template_text"), "template_text", 2000),
        slots=tuple(slots),
        constraints=_strings(raw.get("constraints"), "constraints", 20, 500),
        claim_strength_guidance=_nonempty(
            raw.get("claim_strength_guidance"), "claim_strength_guidance", 1000
        ),
        language=_enum(raw.get("language"), {"en", "zh", "und"}, "language"),
        quality_score=_score(raw.get("quality_score"), "quality_score"),
        **metadata,
    )


def _parse_phrase(
    value: Any, evidences: Mapping[str, Evidence], metadata: Mapping[str, str]
) -> Phrase:
    allowed = {
        "category_evidence_decisions",
        "text",
        "function",
        "position",
        "register",
        "claim_strength",
        "constraints",
        "language",
        "quality_score",
    }
    raw = _object(value, "phrase")
    _closed(raw, allowed, "phrase")
    category, evidence_ids = _category_evidence_ids(raw, evidences)
    payload = {key: raw.get(key) for key in sorted(allowed)}
    return Phrase(
        phrase_id=f"phrase:{sha256_json(payload)}",
        evidence_ids=evidence_ids,
        category=category,
        text=_nonempty(raw.get("text"), "phrase text", 500),
        function=_nonempty(raw.get("function"), "function", 300),
        position=_nonempty(raw.get("position"), "position", 120),
        register=_nonempty(raw.get("register"), "register", 120),
        claim_strength=_enum(raw.get("claim_strength"), set(CLAIM_STRENGTHS), "claim_strength"),
        constraints=_strings(raw.get("constraints"), "constraints", 20, 500),
        language=_enum(raw.get("language"), {"en", "zh", "und"}, "language"),
        quality_score=_score(raw.get("quality_score"), "quality_score"),
        **metadata,
    )


def assign_clusters(records: Sequence[Any], *, threshold: float = 0.85) -> list[Any]:
    """Assign deterministic lexical clusters without dropping source evidence."""

    if not 0.0 < threshold <= 1.0:
        raise ValueError("threshold must be in (0, 1]")
    groups: dict[tuple[str, str, str], list[int]] = {}
    for index, record in enumerate(records):
        asset_type = getattr(record, "asset_type", "evidence")
        groups.setdefault((asset_type, record.category, record.language), []).append(index)
    parents = list(range(len(records)))

    def find(value: int) -> int:
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    for indexes in groups.values():
        for offset, left in enumerate(indexes):
            left_tokens = _shingles(_record_text(records[left]))
            for right in indexes[offset + 1 :]:
                right_tokens = _shingles(_record_text(records[right]))
                union = left_tokens | right_tokens
                score = len(left_tokens & right_tokens) / len(union) if union else 1.0
                if score >= threshold:
                    parents[find(right)] = find(left)
    members: dict[int, list[int]] = {}
    for index in range(len(records)):
        members.setdefault(find(index), []).append(index)
    result = list(records)
    for indexes in members.values():
        identities = sorted(_record_id(records[index]) for index in indexes)
        cluster = f"cluster:{sha256_json(identities)}"
        for index in indexes:
            result[index] = replace(records[index], cluster_id=cluster)
    return result


def _record_text(value: Any) -> str:
    if isinstance(value, Evidence):
        return value.original_text
    if isinstance(value, Strategy):
        return f"{value.label} {value.description} {' '.join(value.steps)}"
    if isinstance(value, Template):
        return re.sub(r"\[[^\]]+\]", "[SLOT]", value.template_text)
    return str(value.text)


def _record_id(value: Any) -> str:
    for field_name in ("evidence_id", "strategy_id", "template_id", "phrase_id"):
        if hasattr(value, field_name):
            return str(getattr(value, field_name))
    raise TypeError("unsupported material record")


def _shingles(text: str, n: int = 3) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]", text.lower())
    if len(tokens) < n:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[index : index + n]) for index in range(len(tokens) - n + 1)}


def _closed(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    missing = sorted(allowed - set(value))
    extra = sorted(set(value) - allowed)
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if extra:
            detail.append(f"unknown {', '.join(extra)}")
        raise MaterialValidationError(f"{label} is not closed-world: {'; '.join(detail)}")


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MaterialValidationError(f"{label} must be an object")
    if "original_text" in value:
        raise MaterialValidationError(f"{label} must not contain original_text")
    return value


def _array(value: Mapping[str, Any], key: str, maximum: int) -> list[Any]:
    result = value.get(key)
    if not isinstance(result, list) or len(result) > maximum:
        raise MaterialValidationError(f"{key} must be a bounded array")
    return result


def _nonempty(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise MaterialValidationError(f"{label} must be a non-empty bounded string")
    return value


def _enum(value: Any, allowed: set[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise MaterialValidationError(f"invalid {label}")
    return value


def _score(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= float(value) <= 1:
        raise MaterialValidationError(f"{label} must be in [0, 1]")
    return float(value)


def _strings(value: Any, label: str, maximum: int, item_maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise MaterialValidationError(f"{label} must be a bounded string array")
    return tuple(_nonempty(item, label, item_maximum) for item in value)


def _string_enums(value: Any, allowed: set[str], label: str, maximum: int) -> tuple[str, ...]:
    values = _strings(value, label, maximum, 128)
    if any(item not in allowed for item in values):
        raise MaterialValidationError(f"invalid {label}")
    if len(set(values)) != len(values):
        raise MaterialValidationError(f"duplicate {label}")
    return tuple(values)


def _risk_flag_decisions(value: Any) -> tuple[str, ...]:
    """Decode the current provider contracts' fixed boolean risk map.

    vLLM/xgrammar does not support JSON Schema ``uniqueItems``. A closed object
    with one required boolean per known flag makes duplicate array members
    structurally impossible while retaining strict, closed-world validation.
    """

    if not isinstance(value, Mapping):
        raise MaterialValidationError("risk_flag_decisions must be an object")
    _closed(value, set(RISK_FLAGS), "risk_flag_decisions")
    if set(value) != set(RISK_FLAGS) or any(not isinstance(item, bool) for item in value.values()):
        raise MaterialValidationError("risk_flag_decisions must contain every risk flag boolean")
    return tuple(flag for flag in RISK_FLAGS if value[flag])


def _category(value: Any) -> str:
    return _enum(value, set(TAXONOMY), "category")


def _evidence_ids(value: Mapping[str, Any], evidences: Mapping[str, Evidence]) -> tuple[str, ...]:
    decisions = value.get("evidence_decisions")
    if not isinstance(decisions, Mapping):
        raise MaterialValidationError("evidence_decisions must be an object")
    if not decisions:
        raise MaterialValidationError("evidence_decisions must select at least one evidence")
    if len(decisions) > 20:
        raise MaterialValidationError("evidence_decisions contains too many references")
    if any(identifier not in evidences for identifier in decisions):
        raise MaterialValidationError("material references unknown evidence")
    if any(selected is not True for selected in decisions.values()):
        raise MaterialValidationError("evidence_decisions values must be true")
    # Preserve request order in the durable evidence_ids tuple. Provider object
    # key order is not semantic and must not affect material identity.
    return tuple(identifier for identifier in evidences if identifier in decisions)


def _category_evidence_ids(
    value: Mapping[str, Any], evidences: Mapping[str, Evidence]
) -> tuple[str, tuple[str, ...]]:
    selections = value.get("category_evidence_decisions")
    if not isinstance(selections, Mapping):
        raise MaterialValidationError("category_evidence_decisions must be an object")
    if len(selections) != 1:
        raise MaterialValidationError(
            "category_evidence_decisions must select exactly one category"
        )
    raw_category, decisions = next(iter(selections.items()))
    category = _category(raw_category)
    if not isinstance(decisions, Mapping):
        raise MaterialValidationError("category evidence decisions must be an object")
    if not decisions:
        raise MaterialValidationError("category evidence decisions must select evidence")
    if len(decisions) > 20:
        raise MaterialValidationError("category evidence decisions contain too many references")
    if any(identifier not in evidences for identifier in decisions):
        raise MaterialValidationError("material references unknown evidence")
    if any(selected is not True for selected in decisions.values()):
        raise MaterialValidationError("category evidence decision values must be true")
    if any(evidences[identifier].category != category for identifier in decisions):
        raise MaterialValidationError("category evidence selection contains mismatched evidence")
    return category, tuple(identifier for identifier in evidences if identifier in decisions)


def _material_category(
    value: Any,
    evidence_ids: Sequence[str],
    evidences: Mapping[str, Evidence],
) -> str:
    category = _category(value)
    if category not in {evidences[evidence_id].category for evidence_id in evidence_ids}:
        raise MaterialValidationError("material category is not supported by referenced evidence")
    return category


def stable_uuid(value: Mapping[str, Any]) -> str:
    return str(uuid.uuid5(_NAMESPACE, sha256_json(value)))
