"""Incremental, review-gated writing-material extraction from Literature artifacts."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

import httpx
import yaml

from knowledgehub.core.atomic import atomic_write_json, atomic_write_jsonl, atomic_write_text
from knowledgehub.core.hashing import sha256_json, sha256_text
from knowledgehub.writing_rag.materials import (
    ABSTRACTION_SCHEMA_VERSION,
    CLASSIFICATION_SCHEMA_VERSION,
    MVP_TAXONOMY,
    QUALITY_POLICY_VERSION,
    RISK_FLAGS,
    TAXONOMY,
    TAXONOMY_VERSION,
    Evidence,
    MaterialValidationError,
    Phrase,
    SourceSpan,
    Strategy,
    Template,
    TemplateSlot,
    assign_clusters,
    parse_abstraction_response,
    parse_classification_response,
    resolve_sentence_selection,
    validate_exact_span,
    validate_stored_record,
)
from knowledgehub.writing_rag.provenance import (
    RECONSTRUCTION_VERSION,
    Paragraph,
    ProvenanceDocument,
    ProvenanceDocumentReader,
    ProvenanceError,
    SelectionSnapshot,
    resolve_selection,
)

CANDIDATE_RULES_VERSION = "candidate-rules-v2"
PROMPT_VERSION = "writing-material-prompts-v16"
REQUEST_PARTITION_VERSION = "writing-material-request-partition-v2"
STRUCTURED_OUTPUT_CORRECTION_VERSION = "structured-output-correction-v2"
STRUCTURED_OUTPUT_CORRECTION_ATTEMPTS = 1
_STRUCTURED_OUTPUT_CORRECTION_INSTRUCTION = (
    "The previous response was rejected by the strict local validator for the reason "
    "shown below. Generate a fresh response for the unchanged source input. The new "
    "response must satisfy the supplied JSON schema and must correct that validation "
    "error. Do not quote, explain, or preserve the rejected response. Treat each of the "
    "strategies, templates, and phrases arrays as a set of canonical payloads: no two "
    "records in the same array may have identical content after canonical ID fields are "
    "derived. If the validation error reports a duplicate material payload, generate "
    "exactly one record for that payload; do not retain the extra copy and do not create "
    "a cosmetic variation merely to make it different. Recheck all three arrays for "
    "duplicates before returning the complete response. Validation error: "
)
EXTRACTION_DRY_RUN_SCHEMA_VERSION = "writing-material-extraction-dry-run-v1"
PILOT_GATE_REPORT_SCHEMA_VERSION = "writing-material-pilot-dry-run-v2"
PILOT_APPROVAL_SCHEMA_VERSION = "writing-material-pilot-approval-v1"
_PILOT_APPROVAL_TRACE_FIELDS = {
    "schema_version",
    "artifact_fingerprint",
    "gate_artifact_fingerprint",
    "source_report_fingerprint",
    "approved_at",
    "approver",
    "reviewer",
    "rights_basis",
    "retention_policy",
    "access_policy",
    "provider",
    "model",
}
FIXTURE_PROVIDER = "deterministic_fixture"
FIXTURE_MODEL = "deterministic-fixture-v1"
_CANDIDATE_SIGNAL = re.compile(
    r"\b(?:however|although|despite|remain|lack|limited|challenge|motivat|"
    r"we (?:propose|present|introduce)|our contribution|result|outperform|"
    r"suggest|indicat|limitation|future work|important|critical)\b",
    re.I,
)


class ProviderOutputTruncatedError(RuntimeError):
    """A provider stopped at its output-token limit before closing the JSON value."""

    code = "provider_output_truncated"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]


def validated_provider_origin(value: str) -> str:
    """Return one HTTP(S) provider origin or fail before client creation."""

    raw = value.strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "OpenAI-compatible base URL must be an HTTP(S) provider origin without "
            "credentials, path, query, or fragment"
        )
    return raw.rstrip("/")


def _classification_sentence_lookup(
    paragraphs: Sequence[Paragraph],
) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for paragraph in paragraphs:
        for sentence in _eligible_classification_sentences(paragraph):
            if sentence.sentence_id in lookup:
                raise ValueError("classification batch contains duplicate sentence IDs")
            lookup[sentence.sentence_id] = paragraph.paragraph_id
    return lookup


def _eligible_classification_sentences(paragraph: Paragraph) -> tuple[Any, ...]:
    """Return only authoritative sentences with complete source provenance."""

    eligible: list[Any] = []
    for sentence in paragraph.sentences:
        if (
            sentence.start < 0
            or sentence.end <= sentence.start
            or sentence.end > len(paragraph.text)
        ):
            continue
        source_spans = paragraph.map_range(sentence.start, sentence.end)
        if not source_spans:
            continue
        if any(not value.bbox or value.page_no <= 0 for value in source_spans):
            continue
        covered = sum(value.paragraph_end - value.paragraph_start for value in source_spans)
        if covered != sentence.end - sentence.start:
            continue
        eligible.append(sentence)
    return tuple(eligible)


def _classification_batches(
    paragraphs: Sequence[Paragraph],
    *,
    max_paragraphs: int,
    max_sentences: int,
) -> tuple[tuple[Paragraph, ...], ...]:
    """Partition requests by both paragraph and authoritative-sentence count.

    Paragraph-only batching left one long paragraph able to exhaust the entire
    generation budget. Slices retain the immutable paragraph identity/text and
    only narrow the authoritative sentence IDs exposed to the provider.
    """

    if max_paragraphs <= 0 or max_sentences <= 0:
        raise ValueError("classification batch limits must be positive")
    batches: list[tuple[Paragraph, ...]] = []
    current: list[Paragraph] = []
    current_sentence_count = 0
    current_paragraph_ids: set[str] = set()

    def flush() -> None:
        nonlocal current, current_sentence_count, current_paragraph_ids
        if current:
            batches.append(tuple(current))
        current = []
        current_sentence_count = 0
        current_paragraph_ids = set()

    for paragraph in paragraphs:
        eligible = _eligible_classification_sentences(paragraph)
        for offset in range(0, len(eligible), max_sentences):
            sentences = tuple(eligible[offset : offset + max_sentences])
            if current and (
                len(current) >= max_paragraphs
                or current_sentence_count + len(sentences) > max_sentences
                or paragraph.paragraph_id in current_paragraph_ids
            ):
                flush()
            current.append(replace(paragraph, sentences=sentences))
            current_sentence_count += len(sentences)
            current_paragraph_ids.add(paragraph.paragraph_id)
    flush()
    return tuple(batches)


def _pilot_approval_trace(
    approval: Mapping[str, Any],
    *,
    selection: SelectionSnapshot,
    sections: set[str],
    version_bundle: str,
    literature_checkpoint: Mapping[str, Any] | None,
    provider: str,
    model: str,
) -> dict[str, str]:
    """Validate explicit human approval before any provider or mutable work."""

    fingerprinted = dict(approval)
    fingerprint = fingerprinted.pop("artifact_fingerprint", None)
    if not isinstance(fingerprint, str) or fingerprint != sha256_json(fingerprinted):
        raise ValueError("pilot approval artifact fingerprint is invalid")
    expected_fields = {
        "schema_version",
        "status",
        "scope",
        "approved_at",
        "approver",
        "reviewer",
        "rights_basis",
        "retention_policy",
        "access_policy",
        "provider",
        "model",
        "provider_execution_authorized",
        "secret_included",
        "production_index_authorized",
        "automatic_expansion_authorized",
        "selection_sha256",
        "selected_documents",
        "sections",
        "literature_checkpoint",
        "version_bundle",
        "gate_artifact_fingerprint",
        "source_report_fingerprint",
    }
    if set(fingerprinted) != expected_fields:
        raise ValueError("pilot approval fields do not match the closed contract")
    if (
        approval.get("schema_version") != PILOT_APPROVAL_SCHEMA_VERSION
        or approval.get("status") != "approved_for_small_batch_extraction"
        or approval.get("scope") != "controlled_pilot_30_50"
        or approval.get("provider_execution_authorized") is not True
        or approval.get("secret_included") is not False
        or approval.get("production_index_authorized") is not False
        or approval.get("automatic_expansion_authorized") is not False
    ):
        raise ValueError("pilot approval does not authorize the controlled extraction")
    for field in (
        "approved_at",
        "approver",
        "reviewer",
        "rights_basis",
        "retention_policy",
        "access_policy",
        "provider",
        "model",
    ):
        value = approval.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 1000:
            raise ValueError(f"pilot approval {field} is invalid")
    try:
        approved_at = datetime.fromisoformat(str(approval["approved_at"]))
    except ValueError as exc:
        raise ValueError("pilot approval timestamp is invalid") from exc
    if approved_at.tzinfo is None:
        raise ValueError("pilot approval timestamp must include a timezone")
    selected = approval.get("selected_documents")
    if (
        not isinstance(selected, int)
        or isinstance(selected, bool)
        or selected != len(selection.document_ids)
    ):
        raise ValueError("pilot approval selected count differs from the current selection")
    if approval.get("selection_sha256") != selection.sha256:
        raise ValueError(
            "pilot approval selection differs from the current source-pinned selection"
        )
    if approval.get("sections") != sorted(sections):
        raise ValueError("pilot approval sections differ from the current extraction")
    if approval.get("version_bundle") != version_bundle:
        raise ValueError("pilot approval version bundle differs from the current extraction")
    if approval.get("literature_checkpoint") != literature_checkpoint:
        raise ValueError("pilot approval Literature checkpoint differs from the current extraction")
    if approval.get("provider") != provider or approval.get("model") != model:
        raise ValueError("pilot approval provider/model differs from the current extraction")
    for field in ("gate_artifact_fingerprint", "source_report_fingerprint"):
        value = approval.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(f"pilot approval {field} is invalid")
    return {
        "schema_version": PILOT_APPROVAL_SCHEMA_VERSION,
        "artifact_fingerprint": fingerprint,
        "gate_artifact_fingerprint": str(approval["gate_artifact_fingerprint"]),
        "source_report_fingerprint": str(approval["source_report_fingerprint"]),
        "approved_at": str(approval["approved_at"]),
        "approver": str(approval["approver"]),
        "reviewer": str(approval["reviewer"]),
        "rights_basis": str(approval["rights_basis"]),
        "retention_policy": str(approval["retention_policy"]),
        "access_policy": str(approval["access_policy"]),
        "provider": str(approval["provider"]),
        "model": str(approval["model"]),
    }


@dataclass(frozen=True, slots=True)
class WritingMaterialRuntimeConfig:
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
    enabled_categories: tuple[str, ...] = MVP_TAXONOMY
    allowed_sections: tuple[str, ...] = ("introduction", "experiment", "conclusion")

    def validate(self, *, require_provider: bool = False) -> "WritingMaterialRuntimeConfig":
        if self.provider not in {"openai_compatible", FIXTURE_PROVIDER}:
            raise ValueError("unsupported writing-material provider")
        if self.provider == FIXTURE_PROVIDER and self.model not in {"", FIXTURE_MODEL}:
            raise ValueError("deterministic_fixture uses the fixed deterministic-fixture-v1 model")
        if not 0 <= self.minimum_quality <= 1:
            raise ValueError("minimum_quality must be in [0, 1]")
        if not 0 <= self.minimum_provenance_coverage <= 1:
            raise ValueError("minimum_provenance_coverage must be in [0, 1]")
        if self.batch_size <= 0 or self.batch_size > 50:
            raise ValueError("batch_size must be in [1, 50]")
        if not 1 <= self.classification_max_sentences_per_request <= 64:
            raise ValueError("classification_max_sentences_per_request must be in [1, 64]")
        if not 1 <= self.abstraction_batch_size <= 64:
            raise ValueError("abstraction_batch_size must be in [1, 64]")
        if not 128 <= self.classification_max_tokens <= 32768:
            raise ValueError("classification_max_tokens must be in [128, 32768]")
        if not 128 <= self.abstraction_max_tokens <= 32768:
            raise ValueError("abstraction_max_tokens must be in [128, 32768]")
        if self.max_retries < 0 or self.timeout_seconds <= 0:
            raise ValueError("invalid provider retry or timeout configuration")
        if not self.enabled_categories or any(
            value not in TAXONOMY for value in self.enabled_categories
        ):
            raise ValueError("enabled_categories must be a non-empty taxonomy subset")
        if any(
            value not in {"introduction", "experiment", "conclusion"}
            for value in self.allowed_sections
        ):
            raise ValueError("allowed_sections contains an unsupported MVP section")
        for path, label in (
            (self.taxonomy_path, "taxonomy"),
            (self.classify_prompt_path, "classification prompt"),
            (self.abstract_prompt_path, "abstraction prompt"),
        ):
            if not path.is_file():
                raise ValueError(f"writing-material {label} is missing: {path}")
        taxonomy = yaml.safe_load(self.taxonomy_path.read_text(encoding="utf-8")) or {}
        if (
            not isinstance(taxonomy, Mapping)
            or taxonomy.get("schema_version") != TAXONOMY_VERSION
            or tuple(taxonomy.get("categories") or ()) != TAXONOMY
            or tuple(taxonomy.get("risk_flags") or ()) != RISK_FLAGS
        ):
            raise ValueError("writing-material taxonomy file does not match the code contract")
        if (
            require_provider
            and self.provider == "openai_compatible"
            and (not self.model or not os.environ.get(self.base_url_env))
        ):
            raise ValueError(f"provider model and {self.base_url_env} are required for extraction")
        return self

    @property
    def effective_model(self) -> str:
        return FIXTURE_MODEL if self.provider == FIXTURE_PROVIDER else self.model

    @property
    def version_manifest(self) -> dict[str, Any]:
        return {
            "reconstruction": RECONSTRUCTION_VERSION,
            "taxonomy": TAXONOMY_VERSION,
            "candidate_rules": CANDIDATE_RULES_VERSION,
            "prompt": PROMPT_VERSION,
            "taxonomy_hash": sha256_text(self.taxonomy_path.read_text(encoding="utf-8")),
            "classify_prompt_hash": sha256_text(
                self.classify_prompt_path.read_text(encoding="utf-8")
            ),
            "abstract_prompt_hash": sha256_text(
                self.abstract_prompt_path.read_text(encoding="utf-8")
            ),
            "classification_schema": CLASSIFICATION_SCHEMA_VERSION,
            "abstraction_schema": ABSTRACTION_SCHEMA_VERSION,
            "quality": QUALITY_POLICY_VERSION,
            "minimum_provenance_coverage": self.minimum_provenance_coverage,
            "provider": self.provider,
            "model": self.effective_model,
            "provider_timeout_seconds": self.timeout_seconds,
            "provider_max_retries": self.max_retries,
            "request_partition": REQUEST_PARTITION_VERSION,
            "structured_output_correction": STRUCTURED_OUTPUT_CORRECTION_VERSION,
            "structured_output_correction_attempts": STRUCTURED_OUTPUT_CORRECTION_ATTEMPTS,
            "structured_output_correction_prompt_hash": sha256_text(
                _STRUCTURED_OUTPUT_CORRECTION_INSTRUCTION
            ),
            "abstraction_adaptive_split_on_truncation": True,
            "abstraction_min_evidence_per_retry": 1,
            "classification_batch_size": self.batch_size,
            "classification_max_sentences_per_request": (
                self.classification_max_sentences_per_request
            ),
            "abstraction_batch_size": self.abstraction_batch_size,
            "classification_max_tokens": self.classification_max_tokens,
            "abstraction_max_tokens": self.abstraction_max_tokens,
            "enabled_categories": self.enabled_categories,
        }

    @property
    def version_bundle(self) -> str:
        return sha256_json(self.version_manifest)


class WritingMaterialAnalyzer(Protocol):
    provider: str
    model: str

    def classify(
        self, paragraphs: Sequence[Paragraph], *, refresh_cache: bool = False
    ) -> Mapping[str, Any]: ...

    def abstract(self, evidences: Sequence[Evidence]) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


class LLMCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get(self, key: str) -> Mapping[str, Any] | None:
        path = self.root / f"{key}.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, Mapping) else None

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        atomic_write_json(self.root / f"{key}.json", dict(value), mode=0o600)


class OpenAICompatibleAnalyzer:
    provider = "openai_compatible"

    def __init__(
        self, config: WritingMaterialRuntimeConfig, *, transport: Any | None = None
    ) -> None:
        self.config = config.validate(require_provider=True)
        self.model = config.model
        base_url = validated_provider_origin(os.environ[config.base_url_env])
        api_key = os.environ.get(config.api_key_env, "")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=config.timeout_seconds,
            transport=transport,
        )
        self.cache = LLMCache(config.data_root / "cache" / "llm")
        self.classify_prompt = config.classify_prompt_path.read_text(encoding="utf-8")
        self.abstract_prompt = config.abstract_prompt_path.read_text(encoding="utf-8")

    def close(self) -> None:
        self.client.close()

    def classify(
        self, paragraphs: Sequence[Paragraph], *, refresh_cache: bool = False
    ) -> Mapping[str, Any]:
        sentence_lookup = _classification_sentence_lookup(paragraphs)
        payload = {
            "taxonomy_version": TAXONOMY_VERSION,
            "enabled_categories": list(self.config.enabled_categories),
            "paragraphs": [
                {
                    "paragraph_id": value.paragraph_id,
                    "section": value.section_title,
                    "section_family": value.section_family,
                    "sentences": [
                        {
                            "sentence_id": sentence.sentence_id,
                            "text": value.text[sentence.start : sentence.end],
                        }
                        for sentence in _eligible_classification_sentences(value)
                    ],
                }
                for value in paragraphs
            ],
        }
        return self._request(
            operation="classify",
            prompt=self.classify_prompt,
            value=payload,
            schema_name="writing_material_classification",
            schema=_classification_json_schema(
                self.config.enabled_categories,
                sentence_ids=tuple(sentence_lookup),
            ),
            max_tokens=self.config.classification_max_tokens,
            refresh_cache=refresh_cache,
            validator=lambda response: parse_classification_response(
                response,
                enabled_categories=self.config.enabled_categories,
                sentence_lookup=sentence_lookup,
            ),
        )

    def abstract(self, evidences: Sequence[Evidence]) -> Mapping[str, Any]:
        payload = {
            "taxonomy_version": TAXONOMY_VERSION,
            "evidence": [
                {
                    "evidence_id": value.evidence_id,
                    "category": value.category,
                    "language": value.language,
                    "section": value.section_title,
                    "text": value.original_text,
                    "claim_strength": value.claim_strength,
                    "risk_flags": list(value.risk_flags),
                }
                for value in evidences
            ],
        }
        lookup = {value.evidence_id: value for value in evidences}
        return self._request(
            operation="abstract",
            prompt=self.abstract_prompt,
            value=payload,
            schema_name="writing_material_abstraction",
            schema=_abstraction_json_schema(
                evidence_ids=tuple(lookup),
                categories=tuple(sorted({value.category for value in evidences})),
                evidence_categories={
                    value.evidence_id: value.category for value in evidences
                },
            ),
            max_tokens=self.config.abstraction_max_tokens,
            validator=lambda response: parse_abstraction_response(
                response,
                lookup,
                provider=self.provider,
                model=self.model,
                prompt_version=PROMPT_VERSION,
                prompt_hash=sha256_text(self.abstract_prompt),
                request_hash=sha256_json(payload),
                response_hash=sha256_json(response),
            ),
        )

    def _request(
        self,
        *,
        operation: str,
        prompt: str,
        value: Mapping[str, Any],
        schema_name: str,
        schema: Mapping[str, Any],
        max_tokens: int,
        refresh_cache: bool = False,
        validator: Callable[[Mapping[str, Any]], Any],
    ) -> Mapping[str, Any]:
        request_hash = sha256_json(
            {
                "provider": self.provider,
                "model": self.model,
                "prompt_hash": sha256_text(prompt),
                "schema": schema,
                "input": value,
                "temperature": 0,
                "max_tokens": max_tokens,
            }
        )
        if not refresh_cache and (cached := self.cache.get(request_hash)):
            response = cached.get("response")
            if isinstance(response, Mapping):
                validator(response)
                return response
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        }
        error: Exception | None = None
        transient_retries = 0
        correction_attempts = 0
        while True:
            try:
                response = self.client.post("/v1/chat/completions", json=body)
                response.raise_for_status()
                result = response.json()
                choice = result["choices"][0]
                if not isinstance(choice, Mapping):
                    raise TypeError("provider choice must be an object")
                if choice.get("finish_reason") == "length":
                    raise ProviderOutputTruncatedError(
                        "provider exhausted max_tokens before completing structured output"
                    )
                content = choice["message"]["content"]
                parsed = (
                    json.loads(content, object_pairs_hook=_strict_json_object)
                    if isinstance(content, str)
                    else content
                )
                if not isinstance(parsed, Mapping):
                    raise ValueError("provider structured response is not an object")
                validator(parsed)
                self.cache.put(
                    request_hash,
                    {
                        "operation": operation,
                        "provider": self.provider,
                        "model": self.model,
                        "prompt_hash": sha256_text(prompt),
                        "request_hash": request_hash,
                        "response_hash": sha256_json(parsed),
                        "response": dict(parsed),
                        "created_at": _now(),
                    },
                )
                return parsed
            except MaterialValidationError as exc:
                error = exc
                if correction_attempts >= STRUCTURED_OUTPUT_CORRECTION_ATTEMPTS:
                    break
                correction_attempts += 1
                correction_prompt = (
                    prompt
                    + "\n\n"
                    + _STRUCTURED_OUTPUT_CORRECTION_INSTRUCTION
                    + json.dumps(str(exc), ensure_ascii=False)
                )
                body["messages"][0]["content"] = correction_prompt
            except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                error = exc
                if (
                    transient_retries >= self.config.max_retries
                    or not _retryable_provider_error(exc)
                ):
                    break
                transient_retries += 1
        raise RuntimeError(f"structured provider request failed: {type(error).__name__}: {error}")


class DeterministicFixtureAnalyzer:
    """Network-free provider for bounded fixtures; it is never selected implicitly."""

    provider = FIXTURE_PROVIDER
    model = FIXTURE_MODEL

    def __init__(self, config: WritingMaterialRuntimeConfig) -> None:
        self.config = config.validate(require_provider=True)
        if config.provider != FIXTURE_PROVIDER:
            raise ValueError("fixture analyzer requires the deterministic_fixture provider")

    def close(self) -> None:
        return None

    def classify(
        self,
        paragraphs: Sequence[Paragraph],
        *,
        refresh_cache: bool = False,
    ) -> Mapping[str, Any]:
        del refresh_cache
        items: dict[str, dict[str, Any]] = {}
        for paragraph in paragraphs:
            eligible_sentences = _eligible_classification_sentences(paragraph)
            if not eligible_sentences:
                continue
            sentence = eligible_sentences[0]
            if not paragraph.text[sentence.start : sentence.end]:
                continue
            category = _fixture_category(paragraph.text, self.config.enabled_categories)
            items[sentence.sentence_id] = {
                "category_decisions": {category: True},
                "claim_strength": "moderate",
                "risk_flag_decisions": {flag: False for flag in RISK_FLAGS},
                "confidence": 1.0,
            }
        return {"schema_version": CLASSIFICATION_SCHEMA_VERSION, "items": items}

    def abstract(self, evidences: Sequence[Evidence]) -> Mapping[str, Any]:
        if len(evidences) > 200:
            raise ValueError("deterministic fixture abstraction is limited to 200 evidences")
        strategies: list[dict[str, Any]] = []
        templates: list[dict[str, Any]] = []
        phrases: list[dict[str, Any]] = []
        for evidence in evidences:
            common = {
                "category_evidence_decisions": {
                    evidence.category: {evidence.evidence_id: True}
                },
                "language": evidence.language,
                "quality_score": 0.9,
            }
            strategies.append(
                common
                | {
                    "label": f"Fixture move: {evidence.category}",
                    "description": "Use a bounded rhetorical move with an explicit scope.",
                    "steps": ["State the local context", "Make one bounded claim"],
                    "applicability": "Controlled pipeline fixtures only",
                    "claim_strength_guidance": "Keep the claim scoped and verifiable.",
                    "explanation_zh": "仅用于验证受控流程, 不作为真实写作建议。",
                    "explanation_en": "For controlled pipeline validation, not writing advice.",
                    "risk_flag_decisions": {flag: False for flag in RISK_FLAGS},
                }
            )
            templates.append(
                common
                | {
                    "template_text": "Within [SCOPE], the evidence supports [BOUNDED_CLAIM].",
                    "slots": [
                        {"name": "SCOPE", "semantic_type": "scope", "required": True},
                        {
                            "name": "BOUNDED_CLAIM",
                            "semantic_type": "claim",
                            "required": True,
                        },
                    ],
                    "constraints": ["Do not broaden beyond the named scope"],
                    "claim_strength_guidance": "Prefer cautious or moderate wording.",
                }
            )
            phrases.append(
                common
                | {
                    "text": "within the evaluated scope",
                    "function": "bound a claim",
                    "position": "clause modifier",
                    "register": "academic",
                    "claim_strength": "moderate",
                    "constraints": ["Name the scope explicitly"],
                }
            )
        return {
            "schema_version": ABSTRACTION_SCHEMA_VERSION,
            "strategies": strategies,
            "templates": templates,
            "phrases": phrases,
        }


def _retryable_provider_error(error: Exception) -> bool:
    """Retry only failures that can plausibly change without changing the request.

    A read timeout commonly means a deterministic local model is still emitting
    a schema-valid but excessively long response. Replaying the same
    temperature-zero request only repeats that cost, so it is deliberately not
    retried. Connection failures and transient HTTP statuses remain retryable.
    """

    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {408, 409, 425, 429} or (
            500 <= error.response.status_code < 600
        )
    if isinstance(error, httpx.ReadTimeout):
        return False
    return isinstance(
        error,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
            httpx.WriteError,
            httpx.WriteTimeout,
            httpx.RemoteProtocolError,
            httpx.ReadError,
        ),
    )


class ExtractionState:
    def __init__(self, data_root: Path, *, initialize: bool = True) -> None:
        self.path = data_root / "state" / "extraction.sqlite3"
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.connect() as connection:
                connection.executescript(_STATE_SCHEMA)
                self._migrate(connection)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        document_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(documents)").fetchall()
        }
        if "version_manifest_json" not in document_columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN version_manifest_json TEXT NOT NULL DEFAULT '{}'"
            )
        attempt_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(attempts)").fetchall()
        }
        for name, definition in (
            ("stage", "TEXT NOT NULL DEFAULT 'complete'"),
            ("version_bundle", "TEXT NOT NULL DEFAULT ''"),
            ("output_hash", "TEXT"),
        ):
            if name not in attempt_columns:
                connection.execute(f"ALTER TABLE attempts ADD COLUMN {name} {definition}")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS attempts_run_stage "
            "ON attempts(document_id,run_id,stage)"
        )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def document(self, document_id: str) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE document_id=?", (document_id,)
            ).fetchone()
        return dict(row) if row else None

    def disposition(
        self,
        document: ProvenanceDocument,
        version_bundle: str,
        version_manifest: Mapping[str, Any] | None = None,
    ) -> str:
        return self.disposition_detail(document, version_bundle, version_manifest)[0]

    def disposition_detail(
        self,
        document: ProvenanceDocument,
        version_bundle: str,
        version_manifest: Mapping[str, Any] | None = None,
    ) -> tuple[str, str | None, str | None]:
        row = self.document(document.document_id)
        if row is None:
            return "new", None, None
        if row["source_content_fingerprint"] != document.source_content_fingerprint:
            return "changed", "source_content_changed", "provenance"
        if row["parse_fingerprint"] != document.parse_fingerprint:
            return "changed", "parse_changed", "provenance"
        if row["version_bundle"] != version_bundle:
            previous = json.loads(str(row.get("version_manifest_json") or "{}"))
            changed = {
                key
                for key in set(previous) | set(version_manifest or {})
                if previous.get(key) != (version_manifest or {}).get(key)
            }
            abstraction_only = {
                "abstract_prompt_hash",
                "abstraction_schema",
                "abstraction_max_tokens",
            }
            stage = "abstraction" if changed and changed <= abstraction_only else "classification"
            reason = "version_changed:" + ",".join(sorted(changed or {"unknown"}))
            return "changed", reason, stage
        if row["status"] in {"failed", "partial"}:
            return "failed", f"prior_{row['status']}", "failed_stage"
        if row["status"] == "inactive":
            return "changed", "prior_inactive", "provenance"
        return "unchanged", None, None

    def mark_unavailable(
        self,
        document_id: str,
        *,
        run_id: str,
        error_code: str,
        error: str,
    ) -> None:
        if not self.path.is_file():
            return
        with self.connect() as connection:
            connection.execute(
                """UPDATE documents SET status='inactive',last_run_id=?,last_error_code=?,
                   last_error=?,updated_at=? WHERE document_id=?""",
                (run_id, error_code, error[:2000], _now(), document_id),
            )

    def record(
        self,
        document: ProvenanceDocument,
        *,
        run_id: str,
        version_bundle: str,
        version_manifest: Mapping[str, Any] | None = None,
        status: str,
        output_hash: str | None = None,
        error_code: str | None = None,
        error: str | None = None,
        stage: str = "complete",
    ) -> None:
        with self.connect() as connection:
            attempt = int(
                connection.execute(
                    "SELECT COALESCE(MAX(attempt),0)+1 FROM attempts WHERE document_id=?",
                    (document.document_id,),
                ).fetchone()[0]
            )
            now = _now()
            connection.execute(
                """INSERT INTO documents(document_id,source_content_fingerprint,parse_fingerprint,
                   version_bundle,status,last_run_id,output_hash,last_error_code,last_error,updated_at,
                   version_manifest_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET
                   source_content_fingerprint=excluded.source_content_fingerprint,
                   parse_fingerprint=excluded.parse_fingerprint,version_bundle=excluded.version_bundle,
                   status=excluded.status,last_run_id=excluded.last_run_id,output_hash=excluded.output_hash,
                   last_error_code=excluded.last_error_code,last_error=excluded.last_error,
                   updated_at=excluded.updated_at,version_manifest_json=excluded.version_manifest_json""",
                (
                    document.document_id,
                    document.source_content_fingerprint,
                    document.parse_fingerprint,
                    version_bundle,
                    status,
                    run_id,
                    output_hash,
                    error_code,
                    error,
                    now,
                    json.dumps(version_manifest or {}, sort_keys=True, separators=(",", ":")),
                ),
            )
            connection.execute(
                """INSERT INTO attempts(document_id,run_id,attempt,status,error_code,error,created_at,
                   stage,version_bundle,output_hash) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(document_id,run_id,stage) DO UPDATE SET
                   status=excluded.status,error_code=excluded.error_code,error=excluded.error,
                   version_bundle=excluded.version_bundle,output_hash=excluded.output_hash""",
                (
                    document.document_id,
                    run_id,
                    attempt,
                    status,
                    error_code,
                    error,
                    now,
                    stage,
                    version_bundle,
                    output_hash,
                ),
            )


@dataclass(slots=True)
class ExtractionSummary:
    run_id: str
    status: str
    dry_run: bool
    selected: int = 0
    planned: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    candidates: int = 0
    evidence: int = 0
    strategies: int = 0
    templates: int = 0
    phrases: int = 0
    run_dir: str | None = None
    dispositions: dict[str, int] | None = None
    stale_reasons: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WritingMaterialExtractionService:
    def __init__(
        self,
        config: WritingMaterialRuntimeConfig,
        *,
        analyzer: WritingMaterialAnalyzer | None = None,
    ) -> None:
        self.config = config.validate()
        self.reader = ProvenanceDocumentReader(config.literature_data_dir)
        self.analyzer = analyzer

    def close(self) -> None:
        if self.analyzer is not None:
            self.analyzer.close()

    def validate_execution_authorization(
        self,
        *,
        selection: Path | None = None,
        document_ids: Sequence[str] = (),
        collections: Sequence[str] = (),
        sections: Sequence[str] = (),
        limit: int | None = None,
        resume_run_id: str | None = None,
        pilot_approval: Mapping[str, Any] | None = None,
    ) -> None:
        """Fail closed on an execution request before task/state persistence.

        ``extract()`` repeats these checks at the durable trust boundary.  The
        CLI calls this read-only precondition before creating its TaskStore
        audit row, so a missing, stale or drifted approval cannot mutate task,
        extraction, run or cache state.
        """

        self.config.validate(require_provider=True)
        if self.config.provider == "openai_compatible":
            validated_provider_origin(os.environ[self.config.base_url_env])
        if resume_run_id:
            if pilot_approval is not None:
                raise ValueError("a resumed run reuses its checkpointed pilot approval")
            self._resume_selection(resume_run_id)
            return
        resolved_selection = resolve_selection(
            self.reader,
            selection=selection,
            document_ids=document_ids,
            collections=collections,
            limit=limit,
        )
        selected_sections = {_section_alias(value) for value in sections} or set(
            self.config.allowed_sections
        )
        if pilot_approval is not None:
            _pilot_approval_trace(
                pilot_approval,
                selection=resolved_selection,
                sections=selected_sections,
                version_bundle=self.config.version_bundle,
                literature_checkpoint=self.reader.checkpoint(),
                provider=self.config.provider,
                model=self.config.effective_model,
            )
        elif self.config.provider != FIXTURE_PROVIDER:
            raise ValueError("real provider extraction requires an explicit pilot approval")

    def _write_checkpoint(
        self,
        *,
        run_dir: Path,
        summary: ExtractionSummary,
        selection: SelectionSnapshot,
        selected_sections: set[str],
        evidences: Sequence[Evidence],
        strategies: Sequence[Strategy],
        templates: Sequence[Template],
        phrases: Sequence[Phrase],
        failures: Sequence[Mapping[str, Any]],
        finished: bool,
        pilot_approval: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically refresh all review assets, publishing the manifest last."""

        if self.analyzer is None:  # pragma: no cover - guarded by extract()
            raise RuntimeError("writing-material analyzer is not initialized")
        clustered_evidence = list(assign_clusters(_unique_records(evidences, "evidence_id")))
        clustered_strategies = list(assign_clusters(_unique_records(strategies, "strategy_id")))
        clustered_templates = list(assign_clusters(_unique_records(templates, "template_id")))
        clustered_phrases = list(assign_clusters(_unique_records(phrases, "phrase_id")))
        summary.evidence = len(clustered_evidence)
        summary.strategies = len(clustered_strategies)
        summary.templates = len(clustered_templates)
        summary.phrases = len(clustered_phrases)
        summary.status = ("partial" if failures else "success") if finished else "running"
        checkpointed_at = _now()
        frozen_selection = run_dir / "selection.jsonl"
        atomic_write_jsonl(frozen_selection, selection.records, mode=0o600)
        manifest = summary.to_dict() | {
            "schema_name": "writing_material_extraction_run",
            "schema_version": "1.0",
            "selection": str(frozen_selection.resolve()),
            "selection_sha256": selection.sha256,
            "selection_sources": dict(selection.sources),
            "sections": sorted(selected_sections),
            "literature_checkpoint": self.reader.checkpoint(),
            "version_bundle": self.config.version_bundle,
            "version_manifest": self.config.version_manifest,
            "versions": {
                "reconstruction": RECONSTRUCTION_VERSION,
                "taxonomy": TAXONOMY_VERSION,
                "candidate_rules": CANDIDATE_RULES_VERSION,
                "prompt": PROMPT_VERSION,
                "classification_schema": CLASSIFICATION_SCHEMA_VERSION,
                "abstraction_schema": ABSTRACTION_SCHEMA_VERSION,
                "quality_policy": QUALITY_POLICY_VERSION,
                "provider": self.analyzer.provider,
                "model": self.analyzer.model,
            },
            "generation_limits": {
                "provider_timeout_seconds": self.config.timeout_seconds,
                "provider_max_retries": self.config.max_retries,
                "request_partition": REQUEST_PARTITION_VERSION,
                "structured_output_correction": STRUCTURED_OUTPUT_CORRECTION_VERSION,
                "structured_output_correction_attempts": (
                    STRUCTURED_OUTPUT_CORRECTION_ATTEMPTS
                ),
                "abstraction_adaptive_split_on_truncation": True,
                "abstraction_min_evidence_per_retry": 1,
                "classification_batch_size": self.config.batch_size,
                "classification_max_sentences_per_request": (
                    self.config.classification_max_sentences_per_request
                ),
                "abstraction_batch_size": self.config.abstraction_batch_size,
                "classification_max_tokens": self.config.classification_max_tokens,
                "abstraction_max_tokens": self.config.abstraction_max_tokens,
            },
            "pilot_approval": dict(pilot_approval) if pilot_approval is not None else None,
            "checkpointed_at": checkpointed_at,
        }
        if finished:
            manifest["finished_at"] = checkpointed_at
        atomic_write_jsonl(
            run_dir / "evidence.jsonl",
            [value.to_dict() for value in clustered_evidence],
            mode=0o600,
        )
        atomic_write_jsonl(
            run_dir / "strategies.jsonl",
            [value.to_dict() for value in clustered_strategies],
            mode=0o600,
        )
        atomic_write_jsonl(
            run_dir / "templates.jsonl",
            [value.to_dict() for value in clustered_templates],
            mode=0o600,
        )
        atomic_write_jsonl(
            run_dir / "phrases.jsonl",
            [value.to_dict() for value in clustered_phrases],
            mode=0o600,
        )
        atomic_write_jsonl(run_dir / "failures.jsonl", failures, mode=0o600)
        atomic_write_text(
            run_dir / "review.md",
            _review_markdown(
                clustered_evidence,
                clustered_strategies,
                clustered_templates,
                clustered_phrases,
            ),
            mode=0o600,
        )
        checkpoint_files = (
            "selection.jsonl",
            "evidence.jsonl",
            "strategies.jsonl",
            "templates.jsonl",
            "phrases.jsonl",
            "failures.jsonl",
            "review.md",
        )
        previous_sequence = 0
        previous_manifest = run_dir / "manifest.json"
        if previous_manifest.is_file():
            try:
                previous = json.loads(previous_manifest.read_text(encoding="utf-8"))
                previous_sequence = int(previous.get("checkpoint", {}).get("sequence", 0))
            except (json.JSONDecodeError, TypeError, ValueError):
                previous_sequence = 0
        manifest["checkpoint"] = {
            "schema_version": "writing-material-checkpoint-v1",
            "sequence": previous_sequence + 1,
            "asset_sha256": {
                name: sha256_text((run_dir / name).read_text(encoding="utf-8"))
                for name in checkpoint_files
            },
        }
        # The manifest is the commit marker: readers never observe it ahead of
        # the asset files that it describes.
        atomic_write_json(run_dir / "manifest.json", manifest, mode=0o600)
        return manifest

    def _resume_selection(
        self, run_id: str
    ) -> tuple[SelectionSnapshot, set[str], dict[str, str] | None]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
            raise ValueError("invalid resume run ID")
        run_dir = self.config.data_root / "runs" / run_id
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"resume run manifest is missing: {run_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "running" or manifest.get("finished_at"):
            raise ValueError("only an unfinished running extraction may be resumed")
        if manifest.get("version_bundle") != self.config.version_bundle:
            raise ValueError("resume version bundle differs from the current runtime")
        _verify_checkpoint(run_dir, manifest)
        records = tuple(_read_jsonl_values(run_dir / "selection.jsonl"))
        required = {
            "document_id",
            "source_content_fingerprint",
            "parse_fingerprint",
            "parser_name",
            "parser_version",
        }
        if not records or any(set(record) != required for record in records):
            raise ValueError("resume selection snapshot is invalid")
        snapshot = SelectionSnapshot(
            document_ids=tuple(str(value["document_id"]) for value in records),
            records=records,
            sources={"resume_run_id": run_id},
        )
        if manifest.get("selection_sha256") != snapshot.sha256:
            raise ValueError("resume selection hash differs from the commit marker")
        available = self.reader.documents()
        for record in records:
            document_id = str(record["document_id"])
            current = available.get(document_id)
            if current is None or any(
                str(current.get(field) or "") != str(record[field])
                for field in (
                    "source_content_fingerprint",
                    "parse_fingerprint",
                    "parser_name",
                    "parser_version",
                )
            ):
                raise ValueError(f"resume source changed since checkpoint: {document_id}")
        raw_sections = manifest.get("sections")
        if not isinstance(raw_sections, list) or not raw_sections:
            raise ValueError("resume section selection is invalid")
        selected_sections = {str(value) for value in raw_sections}
        if not selected_sections <= {"introduction", "experiment", "conclusion"}:
            raise ValueError("resume section selection is unsupported")
        raw_pilot_approval = manifest.get("pilot_approval")
        pilot_approval: dict[str, str] | None = None
        if raw_pilot_approval is not None:
            if (
                not isinstance(raw_pilot_approval, Mapping)
                or set(raw_pilot_approval) != _PILOT_APPROVAL_TRACE_FIELDS
            ):
                raise ValueError("resume pilot approval trace is invalid")
            pilot_approval = {str(key): str(value) for key, value in raw_pilot_approval.items()}
            if (
                pilot_approval["schema_version"] != PILOT_APPROVAL_SCHEMA_VERSION
                or any(not pilot_approval[field].strip() for field in _PILOT_APPROVAL_TRACE_FIELDS)
                or any(
                    not re.fullmatch(r"[0-9a-f]{64}", pilot_approval[field])
                    for field in (
                        "artifact_fingerprint",
                        "gate_artifact_fingerprint",
                        "source_report_fingerprint",
                    )
                )
            ):
                raise ValueError("resume pilot approval trace is invalid")
        return snapshot, selected_sections, pilot_approval

    def extract(
        self,
        *,
        selection: Path | None = None,
        document_ids: Sequence[str] = (),
        collections: Sequence[str] = (),
        sections: Sequence[str] = (),
        limit: int | None = None,
        dry_run: bool = False,
        retry_failed: bool = False,
        run_id: str | None = None,
        resume_run_id: str | None = None,
        pilot_approval: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if run_id and resume_run_id:
            raise ValueError("run_id and resume_run_id are mutually exclusive")
        if dry_run and pilot_approval is not None:
            raise ValueError("pilot_approval is only valid for a non-dry-run extraction")
        if resume_run_id and pilot_approval is not None:
            raise ValueError("a resumed run reuses its checkpointed pilot approval")
        resumed_sections: set[str] | None = None
        pilot_approval_trace: dict[str, str] | None = None
        if resume_run_id:
            (
                resolved_selection,
                resumed_sections,
                pilot_approval_trace,
            ) = self._resume_selection(resume_run_id)
        else:
            resolved_selection = resolve_selection(
                self.reader,
                selection=selection,
                document_ids=document_ids,
                collections=collections,
                limit=limit,
            )
        identifiers = resolved_selection.document_ids
        selected_sections = resumed_sections or (
            {_section_alias(value) for value in sections} or set(self.config.allowed_sections)
        )
        actual_run_id = resume_run_id or run_id or _run_id()
        summary = ExtractionSummary(
            run_id=actual_run_id,
            status="planned" if dry_run else "running",
            dry_run=dry_run,
            selected=len(identifiers),
            dispositions={"new": 0, "changed": 0, "failed": 0, "unchanged": 0},
            stale_reasons={},
        )
        # Planning and approval validation are read-only. Mutable state is
        # initialized only after an optional pilot approval has been verified.
        state = ExtractionState(self.config.data_root, initialize=False)
        documents: list[tuple[ProvenanceDocument, str, list[Paragraph]]] = []
        planning_failures: list[dict[str, str]] = []
        provenance_passed = 0
        section_candidate_documents = 0
        planned_classification_requests = 0
        planned_max_sentences_per_request = 0
        for document_id in identifiers:
            try:
                document = self.reader.load(document_id)
                coverage = document.coverage_for(selected_sections)
                if coverage < self.config.minimum_provenance_coverage:
                    raise ProvenanceError(
                        "low_provenance_coverage",
                        "Selected-section Docling provenance coverage is below "
                        f"{self.config.minimum_provenance_coverage:.2f}: {coverage:.3f}",
                    )
                candidates = detect_candidates(document, selected_sections)
                provenance_passed += 1
                if candidates:
                    section_candidate_documents += 1
                disposition, stale_reason, _reprocess_stage = state.disposition_detail(
                    document,
                    self.config.version_bundle,
                    self.config.version_manifest,
                )
                assert summary.dispositions is not None
                summary.dispositions[disposition] += 1
                if stale_reason:
                    assert summary.stale_reasons is not None
                    summary.stale_reasons[stale_reason] = (
                        summary.stale_reasons.get(stale_reason, 0) + 1
                    )
                summary.candidates += len(candidates)
                if disposition == "unchanged" or (disposition == "failed" and not retry_failed):
                    summary.skipped += 1
                    continue
                summary.planned += 1
                documents.append((document, disposition, candidates))
                request_batches = _classification_batches(
                    candidates,
                    max_paragraphs=self.config.batch_size,
                    max_sentences=self.config.classification_max_sentences_per_request,
                )
                planned_classification_requests += len(request_batches)
                for request_batch in request_batches:
                    planned_max_sentences_per_request = max(
                        planned_max_sentences_per_request,
                        len(_classification_sentence_lookup(request_batch)),
                    )
            except (ProvenanceError, ValueError, OSError, json.JSONDecodeError) as exc:
                planning_failures.append(
                    {
                        "document_id": document_id,
                        "error_code": getattr(exc, "code", "source_validation_error"),
                        "error": str(exc),
                    }
                )
                summary.failed += 1
        if dry_run:
            summary.status = "planned" if not planning_failures else "partial"
            report = summary.to_dict() | {
                "schema_version": EXTRACTION_DRY_RUN_SCHEMA_VERSION,
                "failures": planning_failures,
                "selection_sha256": resolved_selection.sha256,
                "selection_sources": dict(resolved_selection.sources),
                "sections": sorted(selected_sections),
                "literature_checkpoint": self.reader.checkpoint(),
                "version_bundle": self.config.version_bundle,
                "version_manifest": self.config.version_manifest,
                "planning_gates": {
                    "provenance_passed": provenance_passed,
                    "provenance_failed": len(planning_failures),
                    "section_candidate_documents": section_candidate_documents,
                    "zero_candidate_documents": provenance_passed - section_candidate_documents,
                },
                "request_partition_plan": {
                    "version": REQUEST_PARTITION_VERSION,
                    "structured_output_correction": STRUCTURED_OUTPUT_CORRECTION_VERSION,
                    "structured_output_correction_attempts": (
                        STRUCTURED_OUTPUT_CORRECTION_ATTEMPTS
                    ),
                    "classification_requests": planned_classification_requests,
                    "classification_max_paragraphs_per_request": self.config.batch_size,
                    "classification_max_sentences_per_request": (
                        self.config.classification_max_sentences_per_request
                    ),
                    "observed_max_sentences_per_request": planned_max_sentences_per_request,
                    "abstraction_max_evidence_per_request": self.config.abstraction_batch_size,
                    "abstraction_adaptive_split_on_truncation": True,
                    "abstraction_min_evidence_per_retry": 1,
                },
            }
            report["artifact_fingerprint"] = sha256_json(report)
            return report
        if pilot_approval is not None:
            pilot_approval_trace = _pilot_approval_trace(
                pilot_approval,
                selection=resolved_selection,
                sections=selected_sections,
                version_bundle=self.config.version_bundle,
                literature_checkpoint=self.reader.checkpoint(),
                provider=self.config.provider,
                model=self.config.effective_model,
            )
        if (
            self.analyzer is None
            and self.config.provider != FIXTURE_PROVIDER
            and pilot_approval_trace is None
        ):
            raise ValueError("real provider extraction requires an explicit pilot approval")
        state = ExtractionState(self.config.data_root, initialize=True)
        if self.analyzer is None:
            self.analyzer = (
                DeterministicFixtureAnalyzer(self.config)
                if self.config.provider == FIXTURE_PROVIDER
                else OpenAICompatibleAnalyzer(self.config)
            )
        run_dir = self.config.data_root / "runs" / actual_run_id
        if run_dir.exists() and not resume_run_id:
            raise ValueError(f"writing-material run already exists: {actual_run_id}")
        run_dir.mkdir(parents=True, exist_ok=bool(resume_run_id), mode=0o700)
        summary.run_dir = str(run_dir)
        if resume_run_id:
            evidences, strategies, templates, phrases, prior_failures = _load_checkpoint_assets(
                run_dir
            )
            failures = [*prior_failures, *planning_failures]
        else:
            evidences, strategies, templates, phrases = [], [], [], []
            failures = list(planning_failures)
        for failure in planning_failures:
            state.mark_unavailable(
                failure["document_id"],
                run_id=actual_run_id,
                error_code=failure["error_code"],
                error=failure["error"],
            )
        self._write_checkpoint(
            run_dir=run_dir,
            summary=summary,
            selection=resolved_selection,
            selected_sections=selected_sections,
            evidences=evidences,
            strategies=strategies,
            templates=templates,
            phrases=phrases,
            failures=failures,
            finished=False,
            pilot_approval=pilot_approval_trace,
        )
        for document, disposition, candidates in documents:
            document_evidence: list[Evidence] = []
            document_strategies: list[Strategy] = []
            document_templates: list[Template] = []
            document_phrases: list[Phrase] = []
            document_failure_start = len(failures)
            classification_complete = False
            try:
                classification_batches = _classification_batches(
                    candidates,
                    max_paragraphs=self.config.batch_size,
                    max_sentences=self.config.classification_max_sentences_per_request,
                )
                for batch in classification_batches:
                    raw = self.analyzer.classify(
                        batch,
                        refresh_cache=retry_failed and disposition == "failed",
                    )
                    classification_request_hash = sha256_json(
                        {
                            "provider": self.analyzer.provider,
                            "model": self.analyzer.model,
                            "prompt_hash": sha256_text(
                                self.config.classify_prompt_path.read_text(encoding="utf-8")
                            ),
                            "paragraphs": [value.paragraph_id for value in batch],
                            "input_hashes": [value.text_hash for value in batch],
                            "sentence_ids": list(_classification_sentence_lookup(batch)),
                        }
                    )
                    classification_response_hash = sha256_json(raw)
                    sentence_lookup = _classification_sentence_lookup(batch)
                    response = parse_classification_response(
                        raw,
                        enabled_categories=self.config.enabled_categories,
                        sentence_lookup=sentence_lookup,
                    )
                    by_id = {value.paragraph_id: value for value in batch}
                    for item in response.items:
                        paragraph = by_id.get(item.paragraph_id)
                        if paragraph is None:
                            raise ValueError("model returned a paragraph outside the request batch")
                        try:
                            span = resolve_sentence_selection(paragraph, item)
                            evidence = validate_exact_span(
                                paragraph,
                                item,
                                span,
                                document=document,
                                provider=self.analyzer.provider,
                                model=self.analyzer.model,
                                prompt_version=PROMPT_VERSION,
                                prompt_hash=sha256_text(
                                    self.config.classify_prompt_path.read_text(encoding="utf-8")
                                ),
                                request_hash=classification_request_hash,
                                response_hash=classification_response_hash,
                            )
                        except MaterialValidationError as exc:
                            failures.append(
                                {
                                    "document_id": document.document_id,
                                    "paragraph_id": paragraph.paragraph_id,
                                    "error_code": "exact_span_rejected",
                                    "error": str(exc),
                                }
                            )
                            continue
                        if evidence.quality_score < self.config.minimum_quality:
                            failures.append(
                                {
                                    "document_id": document.document_id,
                                    "evidence_id": evidence.evidence_id,
                                    "error_code": "low_quality_candidate",
                                    "error": "candidate is below the configured quality threshold",
                                }
                            )
                            continue
                        document_evidence.append(evidence)
                unique = {value.evidence_id: value for value in document_evidence}
                document_evidence = list(unique.values())
                classification_complete = True
                if document_evidence:
                    abstraction_prompt_hash = sha256_text(
                        self.config.abstract_prompt_path.read_text(encoding="utf-8")
                    )
                    pending_abstraction_batches = [
                        document_evidence[offset : offset + self.config.abstraction_batch_size]
                        for offset in range(
                            0, len(document_evidence), self.config.abstraction_batch_size
                        )
                    ]
                    while pending_abstraction_batches:
                        evidence_batch = pending_abstraction_batches.pop(0)
                        try:
                            abstracted = self.analyzer.abstract(evidence_batch)
                        except ProviderOutputTruncatedError:
                            if len(evidence_batch) == 1:
                                raise
                            midpoint = len(evidence_batch) // 2
                            pending_abstraction_batches[0:0] = [
                                evidence_batch[:midpoint],
                                evidence_batch[midpoint:],
                            ]
                            continue
                        abstraction_request_hash = sha256_json(
                            {
                                "provider": self.analyzer.provider,
                                "model": self.analyzer.model,
                                "prompt_hash": abstraction_prompt_hash,
                                "evidence_ids": [value.evidence_id for value in evidence_batch],
                                "evidence_hashes": [
                                    sha256_text(value.original_text) for value in evidence_batch
                                ],
                            }
                        )
                        lookup = {value.evidence_id: value for value in evidence_batch}
                        batch_strategies, batch_templates, batch_phrases = (
                            parse_abstraction_response(
                                abstracted,
                                lookup,
                                provider=self.analyzer.provider,
                                model=self.analyzer.model,
                                prompt_version=PROMPT_VERSION,
                                prompt_hash=abstraction_prompt_hash,
                                request_hash=abstraction_request_hash,
                                response_hash=sha256_json(abstracted),
                            )
                        )
                        document_strategies.extend(batch_strategies)
                        document_templates.extend(batch_templates)
                        document_phrases.extend(batch_phrases)
                    _require_unique_records(document_strategies, "strategy_id")
                    _require_unique_records(document_templates, "template_id")
                    _require_unique_records(document_phrases, "phrase_id")
            except Exception as exc:
                error_code = getattr(exc, "code", "extraction_error")
                failures.append(
                    {
                        "document_id": document.document_id,
                        "error_code": str(error_code),
                        "error": f"{type(exc).__name__}: {exc}"[:2000],
                    }
                )
                summary.failed += 1
                next_evidences = (
                    evidences + document_evidence if classification_complete else evidences
                )
                self._write_checkpoint(
                    run_dir=run_dir,
                    summary=summary,
                    selection=resolved_selection,
                    selected_sections=selected_sections,
                    evidences=next_evidences,
                    strategies=strategies,
                    templates=templates,
                    phrases=phrases,
                    failures=failures,
                    finished=False,
                    pilot_approval=pilot_approval_trace,
                )
                evidences = next_evidences
                state.record(
                    document,
                    run_id=actual_run_id,
                    version_bundle=self.config.version_bundle,
                    version_manifest=self.config.version_manifest,
                    status="failed",
                    error_code=str(error_code),
                    error=str(exc)[:2000],
                )
                if pilot_approval_trace is not None:
                    break
                continue

            next_evidences = evidences + document_evidence
            next_strategies = strategies + document_strategies
            next_templates = templates + document_templates
            next_phrases = phrases + document_phrases
            summary.processed += 1
            document_has_failures = len(failures) > document_failure_start
            if document_has_failures:
                summary.failed += 1
            self._write_checkpoint(
                run_dir=run_dir,
                summary=summary,
                selection=resolved_selection,
                selected_sections=selected_sections,
                evidences=next_evidences,
                strategies=next_strategies,
                templates=next_templates,
                phrases=next_phrases,
                failures=failures,
                finished=False,
                pilot_approval=pilot_approval_trace,
            )
            evidences = next_evidences
            strategies = next_strategies
            templates = next_templates
            phrases = next_phrases
            output_hash = sha256_json(
                {
                    "evidence": [value.evidence_id for value in document_evidence],
                    "strategies": [value.strategy_id for value in document_strategies],
                    "templates": [value.template_id for value in document_templates],
                    "phrases": [value.phrase_id for value in document_phrases],
                    "version_bundle": self.config.version_bundle,
                }
            )
            state.record(
                document,
                run_id=actual_run_id,
                version_bundle=self.config.version_bundle,
                version_manifest=self.config.version_manifest,
                status=(
                    "partial"
                    if document_has_failures and document_evidence
                    else "failed"
                    if document_has_failures
                    else "success"
                ),
                output_hash=output_hash,
            )
            if pilot_approval_trace is not None and document_has_failures:
                break
        return self._write_checkpoint(
            run_dir=run_dir,
            summary=summary,
            selection=resolved_selection,
            selected_sections=selected_sections,
            evidences=evidences,
            strategies=strategies,
            templates=templates,
            phrases=phrases,
            failures=failures,
            finished=True,
            pilot_approval=pilot_approval_trace,
        )


def detect_candidates(document: ProvenanceDocument, selected_sections: set[str]) -> list[Paragraph]:
    candidates: list[Paragraph] = []
    for paragraph in document.paragraphs:
        if paragraph.section_family not in selected_sections:
            continue
        if not _eligible_classification_sentences(paragraph):
            continue
        lexical = re.findall(r"[A-Za-z0-9-]+|[\u3400-\u4dbf\u4e00-\u9fff]", paragraph.text)
        if len(lexical) < 8 or len(paragraph.text) > 5000:
            continue
        if _CANDIDATE_SIGNAL.search(paragraph.text) or len(lexical) >= 20:
            candidates.append(paragraph)
    return candidates


def _fixture_category(text: str, enabled: Sequence[str]) -> str:
    lowered = text.lower()
    preferred = (
        "prior_work_limitation"
        if re.search(r"\b(?:however|although|limited|limitation|lack)\b", lowered)
        else "result_reporting"
        if re.search(r"\b(?:result|outperform|improv)\w*\b", lowered)
        else "contribution_summary"
        if re.search(r"\bwe (?:propose|present|introduce)\b", lowered)
        else "context_setting"
    )
    return preferred if preferred in enabled else str(enabled[0])


def _section_alias(value: str) -> str:
    normalized = value.strip().lower().replace("/", "_")
    aliases = {
        "results": "experiment",
        "result": "experiment",
        "discussion": "conclusion",
        "results_discussion": "experiment",
        "conclusions": "conclusion",
    }
    result = aliases.get(normalized, normalized)
    if result not in {"introduction", "experiment", "conclusion"}:
        raise ValueError(f"unsupported MVP section: {value}")
    return result


def _review_markdown(
    evidences: Sequence[Evidence],
    strategies: Sequence[Strategy],
    templates: Sequence[Template],
    phrases: Sequence[Phrase],
) -> str:
    by_evidence: dict[str, dict[str, list[Any]]] = {}
    for kind, values in (
        ("strategies", strategies),
        ("templates", templates),
        ("phrases", phrases),
    ):
        for value in values:
            for evidence_id in value.evidence_ids:
                by_evidence.setdefault(evidence_id, {}).setdefault(kind, []).append(value)
    lines = [
        "# Writing material review",
        "",
        "Evidence text is immutable. Reject incorrect provenance instead of editing it.",
        "",
    ]
    for evidence in sorted(
        evidences, key=lambda value: (value.document_id, value.section_title, value.char_start)
    ):
        lines.extend(
            [
                f"## {evidence.section_title} — {evidence.category}",
                "",
                f"- Evidence ID: `{evidence.evidence_id}`",
                f"- Document: `{evidence.document_id}`",
                f"- Zotero item / attachment: `{evidence.zotero_item_key}` / `{evidence.attachment_key}`",
                f"- Page: {evidence.page_start}-{evidence.page_end}",
                f"- Paragraph range: `{evidence.paragraph_id}[{evidence.char_start}:{evidence.char_end}]`",
                f"- Quality: {evidence.quality_score}; risks: {', '.join(evidence.risk_flags) or 'none'}",
                "",
                "> " + evidence.original_text.replace("\n", " "),
                "",
            ]
        )
        related = by_evidence.get(evidence.evidence_id, {})
        for strategy in related.get("strategies", []):
            lines.extend(
                [
                    f"- Strategy `{strategy.strategy_id}`: {strategy.label} — {strategy.description}",
                    "",
                ]
            )
        for template in related.get("templates", []):
            lines.extend([f"- Template `{template.template_id}`: `{template.template_text}`", ""])
        for phrase in related.get("phrases", []):
            lines.extend([f"- Phrase `{phrase.phrase_id}`: `{phrase.text}`", ""])
    return "\n".join(lines).rstrip() + "\n"


def _classification_json_schema(
    categories: Sequence[str], *, sentence_ids: Sequence[str] = ()
) -> dict[str, Any]:
    decision_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "category_decisions",
            "claim_strength",
            "risk_flag_decisions",
            "confidence",
        ],
        "properties": {
            "category_decisions": {
                "type": "object",
                "additionalProperties": False,
                "minProperties": 1,
                "properties": {category: {"const": True} for category in categories},
            },
            "claim_strength": {"enum": ["cautious", "moderate", "strong"]},
            "risk_flag_decisions": {
                "type": "object",
                "additionalProperties": False,
                "required": list(RISK_FLAGS),
                "properties": {flag: {"type": "boolean"} for flag in RISK_FLAGS},
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "items"],
        "properties": {
            "schema_version": {"const": CLASSIFICATION_SCHEMA_VERSION},
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {sentence_id: decision_schema for sentence_id in sentence_ids},
            },
        },
    }


def _strict_json_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"provider structured response contains duplicate object key: {key}")
        result[key] = value
    return result


def _abstraction_json_schema(
    *,
    evidence_ids: Sequence[str] = (),
    categories: Sequence[str] = (),
    evidence_categories: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    allowed_categories = list(categories or TAXONOMY)
    maximum_materials = max(1, len(evidence_ids))
    category_evidence_properties = {
        category: {
            "type": "object",
            "additionalProperties": False,
            "minProperties": 1,
            "properties": {
                evidence_id: {"const": True}
                for evidence_id in evidence_ids
                if evidence_categories is None
                or evidence_categories.get(evidence_id) == category
            },
        }
        for category in allowed_categories
    }
    common = {
        # The sole category key owns a closed evidence-selection object. This
        # makes category/evidence mismatches and duplicate references
        # structurally impossible during constrained generation.
        "category_evidence_decisions": {
            "type": "object",
            "additionalProperties": False,
            "minProperties": 1,
            "maxProperties": 1,
            "properties": category_evidence_properties,
        },
        "language": {"enum": ["en", "zh", "und"]},
        "quality_score": {"type": "number", "minimum": 0, "maximum": 1},
    }

    def obj(required: list[str], properties: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": dict(properties),
        }

    def string(maximum: int) -> dict[str, Any]:
        return {"type": "string", "minLength": 1, "maxLength": maximum}

    def strings(maximum: int, item_maximum: int) -> dict[str, Any]:
        return {
            "type": "array",
            "maxItems": maximum,
            "items": string(item_maximum),
        }

    strategy_properties = common | {
        "label": string(160),
        "description": string(2000),
        "steps": strings(12, 300),
        "applicability": string(1000),
        "claim_strength_guidance": string(1000),
        "explanation_zh": string(2000),
        "explanation_en": string(2000),
        # xgrammar does not support ``uniqueItems``. A fixed boolean object
        # makes duplicate risk flags structurally impossible.
        "risk_flag_decisions": {
            "type": "object",
            "additionalProperties": False,
            "required": list(RISK_FLAGS),
            "properties": {flag: {"type": "boolean"} for flag in RISK_FLAGS},
        },
    }
    template_properties = common | {
        "template_text": string(2000),
        "slots": {
            "type": "array",
            "maxItems": 20,
            "items": obj(
                ["name", "semantic_type", "required"],
                {
                    "name": string(80),
                    "semantic_type": string(120),
                    "required": {"type": "boolean"},
                },
            ),
        },
        "constraints": strings(20, 500),
        "claim_strength_guidance": string(1000),
    }
    phrase_properties = common | {
        "text": string(500),
        "function": string(300),
        "position": string(120),
        "register": string(120),
        "claim_strength": {"enum": ["cautious", "moderate", "strong"]},
        "constraints": strings(20, 500),
    }
    strategy_required = list(strategy_properties)
    template_required = list(template_properties)
    phrase_required = list(phrase_properties)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "strategies", "templates", "phrases"],
        "properties": {
            "schema_version": {"const": ABSTRACTION_SCHEMA_VERSION},
            "strategies": {
                "type": "array",
                "maxItems": maximum_materials,
                "items": obj(strategy_required, strategy_properties),
            },
            "templates": {
                "type": "array",
                "maxItems": maximum_materials,
                "items": obj(template_required, template_properties),
            },
            "phrases": {
                "type": "array",
                "maxItems": maximum_materials,
                "items": obj(phrase_required, phrase_properties),
            },
        },
    }


def _read_jsonl_values(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"checkpoint JSONL object expected at {path}:{line_number}")
        values.append(value)
    return values


def _unique_records(values: Sequence[Any], field: str) -> list[Any]:
    result: dict[str, Any] = {}
    for value in values:
        result[str(getattr(value, field))] = value
    return list(result.values())


def _require_unique_records(values: Sequence[Any], field: str) -> None:
    identifiers = [str(getattr(value, field)) for value in values]
    if len(set(identifiers)) != len(identifiers):
        raise MaterialValidationError(f"duplicate generated {field} across request batches")


def _verify_checkpoint(run_dir: Path, manifest: Mapping[str, Any]) -> None:
    checkpoint = manifest.get("checkpoint")
    if (
        not isinstance(checkpoint, Mapping)
        or checkpoint.get("schema_version") != "writing-material-checkpoint-v1"
        or not isinstance(checkpoint.get("sequence"), int)
    ):
        raise ValueError("resume checkpoint commit marker is missing or invalid")
    assets = checkpoint.get("asset_sha256")
    if not isinstance(assets, Mapping) or not assets:
        raise ValueError("resume checkpoint asset hashes are missing")
    for filename, expected in assets.items():
        path = run_dir / str(filename)
        if (
            not path.is_file()
            or not isinstance(expected, str)
            or sha256_text(path.read_text(encoding="utf-8")) != expected
        ):
            raise ValueError(f"resume checkpoint asset changed or is missing: {filename}")


def _load_checkpoint_assets(
    run_dir: Path,
) -> tuple[list[Evidence], list[Strategy], list[Template], list[Phrase], list[dict[str, Any]]]:
    evidences: list[Evidence] = []
    for raw in _read_jsonl_values(run_dir / "evidence.jsonl"):
        validate_stored_record("evidence", raw)
        value = dict(raw)
        value["section_path"] = tuple(value["section_path"])
        value["sentence_ids"] = tuple(value["sentence_ids"])
        value["risk_flags"] = tuple(value["risk_flags"])
        value["source_spans"] = tuple(SourceSpan(**span) for span in value["source_spans"])
        evidences.append(Evidence(**value))
    strategies: list[Strategy] = []
    for raw in _read_jsonl_values(run_dir / "strategies.jsonl"):
        validate_stored_record("strategy", raw)
        value = dict(raw)
        for field in ("evidence_ids", "steps", "risk_flags"):
            value[field] = tuple(value[field])
        strategies.append(Strategy(**value))
    templates: list[Template] = []
    for raw in _read_jsonl_values(run_dir / "templates.jsonl"):
        validate_stored_record("template", raw)
        value = dict(raw)
        value["evidence_ids"] = tuple(value["evidence_ids"])
        value["constraints"] = tuple(value["constraints"])
        value["slots"] = tuple(TemplateSlot(**slot) for slot in value["slots"])
        templates.append(Template(**value))
    phrases: list[Phrase] = []
    for raw in _read_jsonl_values(run_dir / "phrases.jsonl"):
        validate_stored_record("phrase", raw)
        value = dict(raw)
        value["evidence_ids"] = tuple(value["evidence_ids"])
        value["constraints"] = tuple(value["constraints"])
        phrases.append(Phrase(**value))
    failures = _read_jsonl_values(run_dir / "failures.jsonl")
    return evidences, strategies, templates, phrases, failures


_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  document_id TEXT PRIMARY KEY,
  source_content_fingerprint TEXT NOT NULL,
  parse_fingerprint TEXT NOT NULL,
  version_bundle TEXT NOT NULL,
  status TEXT NOT NULL,
  last_run_id TEXT NOT NULL,
  output_hash TEXT,
  last_error_code TEXT,
  last_error TEXT,
  updated_at TEXT NOT NULL,
  version_manifest_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS attempts (
  document_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL,
  error_code TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT 'complete',
  version_bundle TEXT NOT NULL DEFAULT '',
  output_hash TEXT,
  PRIMARY KEY(document_id, attempt)
);
"""
