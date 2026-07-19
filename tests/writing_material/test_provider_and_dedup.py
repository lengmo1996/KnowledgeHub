from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from knowledgehub.core.hashing import sha256_json, sha256_text
from knowledgehub.writing_rag.extract import (
    OpenAICompatibleAnalyzer,
    ProviderOutputTruncatedError,
    WritingMaterialRuntimeConfig,
    _abstraction_json_schema,
    _classification_json_schema,
    _classification_sentence_lookup,
)
from knowledgehub.writing_rag.materials import (
    MVP_TAXONOMY,
    ClassificationItem,
    ProposedSpan,
    assign_clusters,
    validate_exact_span,
)
from knowledgehub.writing_rag.provenance import ProvenanceDocumentReader

from .helpers import DOCUMENT_ID, build_literature_fixture, write_runtime_contract


def _config(tmp_path, literature) -> WritingMaterialRuntimeConfig:
    taxonomy, classify, abstract = write_runtime_contract(tmp_path)
    return WritingMaterialRuntimeConfig(
        data_root=tmp_path / "materials",
        literature_data_dir=literature,
        taxonomy_path=taxonomy,
        classify_prompt_path=classify,
        abstract_prompt_path=abstract,
        model="fixture-model",
        max_retries=0,
    )


def _fixture_evidence(literature):
    document = ProvenanceDocumentReader(literature).load(DOCUMENT_ID)
    paragraph = document.paragraphs[0]
    item = ClassificationItem(
        paragraph_id=paragraph.paragraph_id,
        category="prior_work_limitation",
        sentence_ids=tuple(sentence.sentence_id for sentence in paragraph.sentences),
        claim_strength="moderate",
        risk_flags=(),
        confidence=0.9,
    )
    return validate_exact_span(
        paragraph,
        item,
        ProposedSpan(0, len(paragraph.text), paragraph.text),
        document=document,
        provider="fixture-provider",
        model="fixture-model",
        prompt_version="fixture-prompt-v1",
        prompt_hash="p" * 64,
        request_hash="q" * 64,
        response_hash="r" * 64,
    )


def _phrase_response(evidence, *, duplicate: bool) -> dict[str, object]:
    phrase = {
        "category_evidence_decisions": {
            evidence.category: {evidence.evidence_id: True}
        },
        "text": "Prior approaches remain limited under [CONDITION].",
        "function": "State a scoped prior-work limitation.",
        "position": "introduction",
        "register": "academic",
        "claim_strength": "moderate",
        "constraints": ["Name the condition explicitly."],
        "language": "en",
        "quality_score": 0.9,
    }
    return {
        "schema_version": "abstraction-v7",
        "strategies": [],
        "templates": [],
        "phrases": [phrase, dict(phrase)] if duplicate else [phrase],
    }


def test_openai_compatible_adapter_uses_strict_schema_and_private_cache(
    tmp_path, monkeypatch
) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    paragraph = ProvenanceDocumentReader(literature).load(DOCUMENT_ID).paragraphs[0]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        assert body["temperature"] == 0
        assert body["max_tokens"] == 8192
        assert body["response_format"]["json_schema"]["strict"] is True
        item_properties = body["response_format"]["json_schema"]["schema"]["properties"]["items"][
            "properties"
        ]
        assert list(item_properties) == [sentence.sentence_id for sentence in paragraph.sentences]
        decision_properties = item_properties[paragraph.sentences[0].sentence_id]["properties"]
        category_decisions = decision_properties["category_decisions"]
        assert set(category_decisions["properties"]) == set(MVP_TAXONOMY)
        assert category_decisions["minProperties"] == 1
        assert "required" not in category_decisions
        assert all(value == {"const": True} for value in category_decisions["properties"].values())
        assert "sentence_id" not in decision_properties
        assert "category" not in decision_properties
        assert "paragraph_id" not in decision_properties
        request_payload = json.loads(body["messages"][1]["content"])
        assert request_payload["paragraphs"][0]["sentences"] == [
            {
                "sentence_id": sentence.sentence_id,
                "text": paragraph.text[sentence.start : sentence.end],
            }
            for sentence in paragraph.sentences
        ]
        assert "text" not in request_payload["paragraphs"][0]
        response = {"schema_version": "classification-v9", "items": {}}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(response)}}]},
        )

    monkeypatch.setenv("KH_WRITING_MATERIAL_LLM_BASE_URL", "https://provider.invalid")
    analyzer = OpenAICompatibleAnalyzer(
        _config(tmp_path, literature), transport=httpx.MockTransport(handler)
    )
    assert analyzer.classify([paragraph])["items"] == {}
    assert analyzer.classify([paragraph])["items"] == {}
    assert analyzer.classify([paragraph], refresh_cache=True)["items"] == {}
    analyzer.close()
    assert len(calls) == 2
    cache_files = list((tmp_path / "materials" / "cache" / "llm").glob("*.json"))
    assert len(cache_files) == 1
    assert cache_files[0].stat().st_mode & 0o777 == 0o600
    first_body = json.loads(calls[0].content)
    expected_pre_correction_cache_key = sha256_json(
        {
            "provider": "openai_compatible",
            "model": "fixture-model",
            "prompt_hash": sha256_text("classify exact evidence"),
            "schema": first_body["response_format"]["json_schema"]["schema"],
            "input": json.loads(first_body["messages"][1]["content"]),
            "temperature": 0,
            "max_tokens": 8192,
        }
    )
    assert cache_files[0].stem == expected_pre_correction_cache_key


@pytest.mark.parametrize(
    "base_url",
    (
        "https://provider.invalid/v1",
        "https://user:secret@provider.invalid",
        "https://provider.invalid?route=v1",
        "https://provider.invalid#v1",
    ),
)
def test_openai_compatible_adapter_rejects_non_origin_url_before_client_use(
    tmp_path, monkeypatch, base_url
) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    monkeypatch.setenv("KH_WRITING_MATERIAL_LLM_BASE_URL", base_url)
    with pytest.raises(ValueError, match="provider origin"):
        OpenAICompatibleAnalyzer(_config(tmp_path, literature))
    assert not (tmp_path / "materials").exists()


def test_provider_schemas_avoid_vllm_unsupported_unique_items() -> None:
    schemas = (
        _classification_json_schema(MVP_TAXONOMY),
        _abstraction_json_schema(),
    )
    assert all("uniqueItems" not in json.dumps(schema) for schema in schemas)

    classification = _classification_json_schema(MVP_TAXONOMY, sentence_ids=("sentence:one",))
    classification_items = classification["properties"]["items"]
    assert classification_items["additionalProperties"] is False
    classification_decision = classification_items["properties"]["sentence:one"]
    assert classification_decision["additionalProperties"] is False
    category_decisions = classification_decision["properties"]["category_decisions"]
    assert category_decisions["additionalProperties"] is False
    assert category_decisions["minProperties"] == 1
    assert all(value == {"const": True} for value in category_decisions["properties"].values())
    classification_decisions = classification_decision["properties"]["risk_flag_decisions"]
    assert classification_decisions["additionalProperties"] is False
    assert set(classification_decisions["required"]) == set(classification_decisions["properties"])
    assert all(
        value == {"type": "boolean"} for value in classification_decisions["properties"].values()
    )

    decisions = _abstraction_json_schema()["properties"]["strategies"]["items"]["properties"][
        "risk_flag_decisions"
    ]
    assert decisions["additionalProperties"] is False
    assert set(decisions["required"]) == set(decisions["properties"])
    assert all(value == {"type": "boolean"} for value in decisions["properties"].values())


def test_classification_only_exposes_sentences_with_complete_source_provenance(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    paragraph = ProvenanceDocumentReader(literature).load(DOCUMENT_ID).paragraphs[0]
    second = paragraph.sentences[1]
    segment = paragraph.segments[0]
    paragraph = replace(
        paragraph,
        segments=(
            replace(
                segment,
                paragraph_start=second.start,
                source_start=segment.source_start + second.start,
            ),
        ),
    )

    assert _classification_sentence_lookup([paragraph]) == {
        second.sentence_id: paragraph.paragraph_id
    }


def test_provider_duplicate_json_object_key_is_rejected(tmp_path, monkeypatch) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    paragraph = ProvenanceDocumentReader(literature).load(DOCUMENT_ID).paragraphs[0]

    def handler(_request: httpx.Request) -> httpx.Response:
        content = '{"schema_version":"classification-v9","items":{},"items":{}}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setenv("KH_WRITING_MATERIAL_LLM_BASE_URL", "https://provider.invalid")
    analyzer = OpenAICompatibleAnalyzer(
        _config(tmp_path, literature), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(RuntimeError, match="duplicate object key"):
        analyzer.classify([paragraph])
    analyzer.close()

    abstraction = _abstraction_json_schema(
        evidence_ids=("evidence:one", "evidence:two"),
        categories=("gap_identification", "motivation"),
        evidence_categories={
            "evidence:one": "gap_identification",
            "evidence:two": "motivation",
        },
    )
    category_schema = abstraction["properties"]["strategies"]["items"]["properties"][
        "category_evidence_decisions"
    ]
    assert category_schema["maxProperties"] == 1
    reference_schema = category_schema["properties"]["gap_identification"]
    assert reference_schema["properties"] == {"evidence:one": {"const": True}}
    assert category_schema["properties"]["motivation"]["properties"] == {
        "evidence:two": {"const": True}
    }
    assert reference_schema["additionalProperties"] is False
    assert reference_schema["minProperties"] == 1
    assert abstraction["properties"]["strategies"]["maxItems"] == 2
    assert abstraction["properties"]["templates"]["maxItems"] == 2
    assert abstraction["properties"]["phrases"]["maxItems"] == 2
    strategy = abstraction["properties"]["strategies"]["items"]["properties"]
    assert "category" not in strategy
    assert strategy["label"]["maxLength"] == 160
    assert strategy["steps"]["maxItems"] == 12
    assert strategy["steps"]["items"]["maxLength"] == 300
    template = abstraction["properties"]["templates"]["items"]["properties"]
    slot = template["slots"]["items"]["properties"]
    assert slot["name"]["maxLength"] == 80
    assert slot["semantic_type"]["maxLength"] == 120
    phrase = abstraction["properties"]["phrases"]["items"]["properties"]
    assert phrase["function"]["maxLength"] == 300
    assert phrase["position"]["maxLength"] == 120


def test_provider_invalid_json_is_rejected(tmp_path, monkeypatch) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    paragraph = ProvenanceDocumentReader(literature).load(DOCUMENT_ID).paragraphs[0]
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})

    monkeypatch.setenv("KH_WRITING_MATERIAL_LLM_BASE_URL", "https://provider.invalid")
    analyzer = OpenAICompatibleAnalyzer(
        replace(_config(tmp_path, literature), max_retries=2),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RuntimeError, match="structured provider request failed"):
        analyzer.classify([paragraph])
    analyzer.close()
    assert calls == 1


def test_provider_corrects_duplicate_material_once_without_caching_invalid_response(
    tmp_path, monkeypatch
) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    evidence = _fixture_evidence(literature)
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        response = _phrase_response(evidence, duplicate=len(calls) == 1)
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(response)}}]},
        )

    monkeypatch.setenv("KH_WRITING_MATERIAL_LLM_BASE_URL", "https://provider.invalid")
    analyzer = OpenAICompatibleAnalyzer(
        _config(tmp_path, literature), transport=httpx.MockTransport(handler)
    )
    assert len(analyzer.abstract([evidence])["phrases"]) == 1
    correction_prompt = calls[1]["messages"][0]["content"]
    assert "duplicate phrase payload" in correction_prompt
    assert "arrays as a set of canonical payloads" in correction_prompt
    assert "exactly one record for that payload" in correction_prompt
    assert len(analyzer.abstract([evidence])["phrases"]) == 1
    analyzer.close()

    assert len(calls) == 2
    cache_files = list((tmp_path / "materials" / "cache" / "llm").glob("*.json"))
    assert len(cache_files) == 1
    cached = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert len(cached["response"]["phrases"]) == 1


def test_provider_rejects_persistently_duplicate_material_after_one_correction(
    tmp_path, monkeypatch
) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    evidence = _fixture_evidence(literature)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        response = _phrase_response(evidence, duplicate=True)
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(response)}}]},
        )

    monkeypatch.setenv("KH_WRITING_MATERIAL_LLM_BASE_URL", "https://provider.invalid")
    analyzer = OpenAICompatibleAnalyzer(
        replace(_config(tmp_path, literature), max_retries=2),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RuntimeError, match="duplicate phrase payload"):
        analyzer.abstract([evidence])
    analyzer.close()

    assert calls == 2
    assert not list((tmp_path / "materials" / "cache" / "llm").glob("*.json"))


def test_provider_read_timeout_is_not_replayed(tmp_path, monkeypatch) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    paragraph = ProvenanceDocumentReader(literature).load(DOCUMENT_ID).paragraphs[0]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("generation exceeded deadline", request=request)

    monkeypatch.setenv("KH_WRITING_MATERIAL_LLM_BASE_URL", "https://provider.invalid")
    analyzer = OpenAICompatibleAnalyzer(
        replace(_config(tmp_path, literature), max_retries=2),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RuntimeError, match="ReadTimeout"):
        analyzer.classify([paragraph])
    analyzer.close()
    assert calls == 1


def test_provider_transient_http_error_is_retried(tmp_path, monkeypatch) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    paragraph = ProvenanceDocumentReader(literature).load(DOCUMENT_ID).paragraphs[0]
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="temporarily unavailable")

    monkeypatch.setenv("KH_WRITING_MATERIAL_LLM_BASE_URL", "https://provider.invalid")
    analyzer = OpenAICompatibleAnalyzer(
        replace(_config(tmp_path, literature), max_retries=2),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RuntimeError, match="HTTPStatusError"):
        analyzer.classify([paragraph])
    analyzer.close()
    assert calls == 3


def test_abstraction_request_has_independent_output_limit(tmp_path, monkeypatch) -> None:
    literature = build_literature_fixture(tmp_path / "literature")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["max_tokens"] == 8192
        response = {
            "schema_version": "abstraction-v7",
            "strategies": [],
            "templates": [],
            "phrases": [],
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(response)}}]},
        )

    monkeypatch.setenv("KH_WRITING_MATERIAL_LLM_BASE_URL", "https://provider.invalid")
    analyzer = OpenAICompatibleAnalyzer(
        _config(tmp_path, literature), transport=httpx.MockTransport(handler)
    )
    assert analyzer.abstract([])["strategies"] == []
    analyzer.close()


def test_provider_reports_token_limit_as_structured_truncation(tmp_path, monkeypatch) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    paragraph = ProvenanceDocumentReader(literature).load(DOCUMENT_ID).paragraphs[0]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": '{"schema_version":"classification-v9"'},
                    }
                ]
            },
        )

    monkeypatch.setenv("KH_WRITING_MATERIAL_LLM_BASE_URL", "https://provider.invalid")
    analyzer = OpenAICompatibleAnalyzer(
        _config(tmp_path, literature), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ProviderOutputTruncatedError, match="exhausted max_tokens"):
        analyzer.classify([paragraph])
    analyzer.close()


def test_dedup_is_stable_and_language_scoped(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    document = ProvenanceDocumentReader(literature).load(DOCUMENT_ID)
    paragraph = document.paragraphs[0]
    from knowledgehub.writing_rag.materials import (
        ClassificationItem,
        ProposedSpan,
        validate_exact_span,
    )

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
        ProposedSpan(0, len(paragraph.text), paragraph.text),
        document=document,
        provider="fake",
        model="fake",
        prompt_version="v1",
        prompt_hash="p" * 64,
        request_hash="q" * 64,
        response_hash="r" * 64,
    )
    duplicate = replace(evidence, evidence_id="evidence:duplicate")
    chinese = replace(
        evidence,
        evidence_id="evidence:zh",
        language="zh",
        original_text="现有方法在该条件下仍然受限。",
    )
    clustered = assign_clusters([evidence, duplicate, chinese])
    assert clustered[0].cluster_id == clustered[1].cluster_id
    assert clustered[0].cluster_id != clustered[2].cluster_id
