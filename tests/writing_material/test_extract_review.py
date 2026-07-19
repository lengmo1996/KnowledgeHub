from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import pytest

from knowledgehub.cli.writing_material import (
    add_writing_material_parser,
    run_writing_material_command,
)
from knowledgehub.core.hashing import sha256_json
from knowledgehub.pipeline.artifacts import safe_document_name
from knowledgehub.pipeline.config import RagConfig
from knowledgehub.writing_rag.extract import (
    FIXTURE_MODEL,
    FIXTURE_PROVIDER,
    ExtractionState,
    ProviderOutputTruncatedError,
    WritingMaterialExtractionService,
    WritingMaterialRuntimeConfig,
)
from knowledgehub.writing_rag.materials import RISK_FLAGS, Evidence
from knowledgehub.writing_rag.provenance import Paragraph, ProvenanceDocumentReader
from knowledgehub.writing_rag.review import (
    ReviewValidationError,
    WritingMaterialCandidateIndexer,
    WritingMaterialReviewService,
)

from .helpers import (
    DOCUMENT_ID,
    PARAGRAPH_TEXT,
    based_on,
    build_literature_fixture,
    write_runtime_contract,
)


def test_review_cli_requires_explicit_partial_snapshot_flag() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_writing_material_parser(commands)
    args = parser.parse_args(
        [
            "writing-material",
            "review",
            "apply",
            "--run-id",
            "run-1",
            "--decisions",
            "decisions.jsonl",
            "--allow-partial-snapshot",
        ]
    )
    assert args.allow_partial_snapshot is True


@pytest.mark.parametrize(
    "classification_schema",
    (
        "classification-v1",
        "classification-v2",
        "classification-v3",
        "classification-v4",
        "classification-v5",
        "classification-v6",
        "classification-v7",
        "classification-v8",
        "classification-v9",
    ),
)
@pytest.mark.parametrize(
    "abstraction_schema",
    (
        "abstraction-v1",
        "abstraction-v2",
        "abstraction-v3",
        "abstraction-v4",
        "abstraction-v5",
        "abstraction-v6",
        "abstraction-v7",
    ),
)
def test_review_manifest_keeps_supported_classification_runs_readable(
    tmp_path, classification_schema, abstraction_schema
) -> None:
    manifest = {
        "schema_name": "writing_material_extraction_run",
        "schema_version": "1.0",
        "status": "partial",
        "finished_at": "2026-07-18T07:07:07+00:00",
        "versions": {
            "taxonomy": "writing-taxonomy-v1",
            "classification_schema": classification_schema,
            "abstraction_schema": abstraction_schema,
            "prompt": "historical-prompt",
            "provider": "historical-provider",
            "model": "historical-model",
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    loaded = WritingMaterialReviewService._completed_manifest(tmp_path)
    assert loaded["versions"]["classification_schema"] == classification_schema


def test_release_and_pilot_cli_commands_keep_explicit_safety_inputs() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_writing_material_parser(commands)

    release = parser.parse_args(
        [
            "writing-material",
            "release",
            "stage",
            "--manifest",
            "release.json",
            "--yes",
        ]
    )
    assert release.writing_material_release_command == "stage"
    assert release.yes is True
    pilot = parser.parse_args(
        [
            "writing-material",
            "pilot",
            "evaluate",
            "--run-id",
            "run-1",
            "--candidate-report",
            "candidate.json",
            "--retrieval-report",
            "retrieval.json",
        ]
    )
    assert pilot.writing_material_pilot_command == "evaluate"
    assert pilot.candidate_report == Path("candidate.json")
    retrieval = parser.parse_args(
        [
            "writing-material",
            "pilot",
            "evaluate-retrieval",
            "--run-id",
            "run-1",
            "--candidate-report",
            "candidate.json",
            "--queries",
            "queries.jsonl",
        ]
    )
    assert retrieval.writing_material_pilot_command == "evaluate-retrieval"
    assert retrieval.mode == "sparse"
    quality = parser.parse_args(
        [
            "writing-material",
            "pilot",
            "audit-quality",
            "--run-id",
            "run-1",
            "--output",
            "quality.json",
        ]
    )
    assert quality.writing_material_pilot_command == "audit-quality"
    assert quality.output == Path("quality.json")
    extraction = parser.parse_args(
        [
            "writing-material",
            "extract",
            "--selection",
            "selection.jsonl",
            "--pilot-approval",
            "pilot-approval.json",
        ]
    )
    assert extraction.pilot_approval == Path("pilot-approval.json")


def test_extract_cli_dispatch_supports_document_and_collection_dry_run(
    tmp_path, monkeypatch, capsys
) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    runtime = _config(tmp_path, literature)
    materials = SimpleNamespace(
        data_root=runtime.data_root,
        literature_data_dir=runtime.literature_data_dir,
        runtime_config=lambda: runtime,
    )
    monkeypatch.setattr(
        "knowledgehub.cli.writing_material.HubConfig.load",
        lambda _path: SimpleNamespace(writing_materials=materials),
    )
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_writing_material_parser(commands)
    for selector in (["--document-id", DOCUMENT_ID], ["--collection", "COLLKEY"]):
        args = parser.parse_args(["writing-material", "extract", *selector, "--dry-run"])
        args.hub_config = None
        assert run_writing_material_command(args) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "planned"
        assert payload["planned"] == 1
        assert payload["planning_gates"] == {
            "provenance_passed": 1,
            "provenance_failed": 0,
            "section_candidate_documents": 1,
            "zero_candidate_documents": 0,
        }
        assert payload["request_partition_plan"]["observed_max_sentences_per_request"] <= 8
        assert payload["request_partition_plan"]["abstraction_max_evidence_per_request"] == 8
    assert not runtime.data_root.exists()


class FakeAnalyzer:
    provider = "fake"
    model = "fake-model"

    def classify(
        self, paragraphs: Sequence[Paragraph], *, refresh_cache: bool = False
    ) -> Mapping[str, Any]:
        del refresh_cache
        paragraph = paragraphs[0]
        sentence_id = paragraph.sentences[0].sentence_id
        return {
            "schema_version": "classification-v9",
            "items": {
                sentence_id: {
                    "category_decisions": {"prior_work_limitation": True},
                    "claim_strength": "moderate",
                    "risk_flag_decisions": {flag: False for flag in RISK_FLAGS},
                    "confidence": 0.95,
                }
            },
        }

    def abstract(self, evidences: Sequence[Evidence]) -> Mapping[str, Any]:
        evidence = evidences[0]
        common = {
            "category_evidence_decisions": {
                evidence.category: {evidence.evidence_id: True}
            },
            "language": "en",
            "quality_score": 0.9,
        }
        return {
            "schema_version": "abstraction-v7",
            "strategies": [
                common
                | {
                    "label": "Scoped limitation",
                    "description": "Name the limitation and its operating condition.",
                    "steps": ["State prior scope", "Name the remaining limitation"],
                    "applicability": "Research-gap positioning",
                    "claim_strength_guidance": "Use remain limited only with a named scope.",
                    "explanation_zh": "先限定范围\uff0c再指出不足。",
                    "explanation_en": "Bound the scope before stating the limitation.",
                    "risk_flag_decisions": {
                        "unsupported_superlative": False,
                        "exaggerated_novelty": False,
                        "vague_claim": False,
                        "missing_comparison": False,
                        "causal_overclaim": False,
                    },
                }
            ],
            "templates": [
                common
                | {
                    "template_text": "However, prior [APPROACHES] remain limited under [CONDITION].",
                    "slots": [
                        {"name": "APPROACHES", "semantic_type": "method", "required": True},
                        {"name": "CONDITION", "semantic_type": "scope", "required": True},
                    ],
                    "constraints": ["Name the comparison scope"],
                    "claim_strength_guidance": "Avoid universal claims.",
                }
            ],
            "phrases": [
                common
                | {
                    "text": "remain limited under",
                    "function": "scope a limitation",
                    "position": "predicate",
                    "register": "academic",
                    "claim_strength": "moderate",
                    "constraints": ["Follow with a concrete condition"],
                }
            ],
        }

    def close(self) -> None:
        return None


class InvalidSelectionAnalyzer(FakeAnalyzer):
    def classify(
        self, paragraphs: Sequence[Paragraph], *, refresh_cache: bool = False
    ) -> Mapping[str, Any]:
        result = dict(super().classify(paragraphs, refresh_cache=refresh_cache))
        items = dict(result["items"])
        categories = items.pop(next(iter(items)))
        items["sentence:not-in-source"] = categories
        result["items"] = items
        return result


class MixedQualityAnalyzer(FakeAnalyzer):
    def classify(
        self, paragraphs: Sequence[Paragraph], *, refresh_cache: bool = False
    ) -> Mapping[str, Any]:
        result = dict(super().classify(paragraphs, refresh_cache=refresh_cache))
        items = dict(result["items"])
        paragraph = paragraphs[0]
        sentence_id = paragraph.sentences[1].sentence_id
        items[sentence_id] = {
            "category_decisions": {"gap_identification": True},
            "claim_strength": "moderate",
            "confidence": 0.0,
            "risk_flag_decisions": {flag: True for flag in RISK_FLAGS},
        }
        result["items"] = items
        return result


class RefreshTrackingAnalyzer(FakeAnalyzer):
    def __init__(self) -> None:
        self.refresh_cache_values: list[bool] = []

    def classify(
        self, paragraphs: Sequence[Paragraph], *, refresh_cache: bool = False
    ) -> Mapping[str, Any]:
        self.refresh_cache_values.append(refresh_cache)
        return super().classify(paragraphs, refresh_cache=refresh_cache)


class FailingAbstractionAnalyzer(FakeAnalyzer):
    def abstract(self, evidences: Sequence[Evidence]) -> Mapping[str, Any]:
        assert evidences
        raise RuntimeError("fixture abstraction failure")


class FailingSecondClassificationAnalyzer(FakeAnalyzer):
    def __init__(self) -> None:
        self.classification_calls = 0

    def classify(
        self, paragraphs: Sequence[Paragraph], *, refresh_cache: bool = False
    ) -> Mapping[str, Any]:
        self.classification_calls += 1
        if self.classification_calls == 2:
            raise RuntimeError("fixture second classification failure")
        return super().classify(paragraphs, refresh_cache=refresh_cache)


class PartitionTrackingAnalyzer(FakeAnalyzer):
    def __init__(self) -> None:
        self.classification_sentence_counts: list[int] = []
        self.abstraction_batch_sizes: list[int] = []

    def classify(
        self, paragraphs: Sequence[Paragraph], *, refresh_cache: bool = False
    ) -> Mapping[str, Any]:
        self.classification_sentence_counts.append(
            sum(len(paragraph.sentences) for paragraph in paragraphs)
        )
        return super().classify(paragraphs, refresh_cache=refresh_cache)

    def abstract(self, evidences: Sequence[Evidence]) -> Mapping[str, Any]:
        self.abstraction_batch_sizes.append(len(evidences))
        return super().abstract(evidences)


class AdaptiveSplitAnalyzer(PartitionTrackingAnalyzer):
    def abstract(self, evidences: Sequence[Evidence]) -> Mapping[str, Any]:
        self.abstraction_batch_sizes.append(len(evidences))
        if len(evidences) > 1:
            raise ProviderOutputTruncatedError("fixture output reached max_tokens")
        return FakeAnalyzer.abstract(self, evidences)


def _config(tmp_path: Path, literature: Path) -> WritingMaterialRuntimeConfig:
    taxonomy, classify, abstract = write_runtime_contract(tmp_path)
    return WritingMaterialRuntimeConfig(
        data_root=tmp_path / "materials",
        literature_data_dir=literature,
        taxonomy_path=taxonomy,
        classify_prompt_path=classify,
        abstract_prompt_path=abstract,
    ).validate()


def _selection(tmp_path: Path) -> Path:
    path = tmp_path / "selection.jsonl"
    path.write_text(json.dumps({"document_id": DOCUMENT_ID}) + "\n", encoding="utf-8")
    return path


def test_dry_run_has_no_state_cache_task_or_run_writes(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    result = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer()).extract(
        selection=_selection(tmp_path), dry_run=True
    )
    assert result["status"] == "planned"
    assert result["planned"] == 1
    assert not config.data_root.exists()


def test_real_provider_extraction_requires_explicit_pilot_approval_before_writes(
    tmp_path,
) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    service = WritingMaterialExtractionService(config)
    with pytest.raises(ValueError, match="explicit pilot approval"):
        service.extract(selection=_selection(tmp_path))
    assert not config.data_root.exists()


def test_cli_configured_fixture_provider_runs_without_network_or_llm_cache(
    tmp_path, monkeypatch, capsys
) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    runtime = replace(
        _config(tmp_path, literature),
        provider=FIXTURE_PROVIDER,
        model="",
    ).validate(require_provider=True)
    materials = SimpleNamespace(
        data_root=runtime.data_root,
        literature_data_dir=runtime.literature_data_dir,
        runtime_config=lambda: runtime,
    )
    monkeypatch.setattr(
        "knowledgehub.cli.writing_material.HubConfig.load",
        lambda _path: SimpleNamespace(writing_materials=materials),
    )
    monkeypatch.setenv("KH_STATE_ROOT", str(tmp_path / "task-state"))
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_writing_material_parser(commands)
    args = parser.parse_args(
        [
            "writing-material",
            "extract",
            "--document-id",
            DOCUMENT_ID,
        ]
    )
    args.hub_config = None

    assert run_writing_material_command(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "success"
    assert result["evidence"] == 1
    run_dir = Path(result["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    evidence = json.loads((run_dir / "evidence.jsonl").read_text(encoding="utf-8"))
    assert manifest["versions"]["provider"] == FIXTURE_PROVIDER
    assert manifest["versions"]["model"] == FIXTURE_MODEL
    assert manifest["version_manifest"]["model"] == FIXTURE_MODEL
    assert manifest["version_manifest"]["classification_batch_size"] == runtime.batch_size
    assert manifest["generation_limits"]["classification_batch_size"] == runtime.batch_size
    assert manifest["version_manifest"]["provider_timeout_seconds"] == 600.0
    assert manifest["generation_limits"]["provider_timeout_seconds"] == 600.0
    assert evidence["analyzer_provider"] == FIXTURE_PROVIDER
    assert PARAGRAPH_TEXT.startswith(evidence["original_text"])
    assert not (runtime.data_root / "cache" / "llm").exists()
    assert (tmp_path / "task-state" / "tasks.sqlite3").is_file()


def test_cli_controlled_pilot_binds_explicit_approval(tmp_path, monkeypatch, capsys) -> None:
    literature = build_literature_fixture(tmp_path / "literature", document_count=30)
    runtime = replace(
        _config(tmp_path, literature),
        provider=FIXTURE_PROVIDER,
        model="",
    ).validate(require_provider=True)
    materials = SimpleNamespace(
        data_root=runtime.data_root,
        literature_data_dir=runtime.literature_data_dir,
        runtime_config=lambda: runtime,
    )
    monkeypatch.setattr(
        "knowledgehub.cli.writing_material.HubConfig.load",
        lambda _path: SimpleNamespace(writing_materials=materials),
    )
    monkeypatch.setenv("KH_STATE_ROOT", str(tmp_path / "task-state"))
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_writing_material_parser(commands)

    dry_report_path = tmp_path / "dry-run.json"
    dry_args = parser.parse_args(
        [
            "writing-material",
            "extract",
            "--collection",
            "COLLKEY",
            "--limit",
            "30",
            "--dry-run",
            "--output",
            str(dry_report_path),
        ]
    )
    dry_args.hub_config = None
    assert run_writing_material_command(dry_args) == 0
    dry_report = json.loads(capsys.readouterr().out)
    assert dry_report["selected"] == 30
    assert not runtime.data_root.exists()
    assert dry_report_path.stat().st_mode & 0o777 == 0o600

    gate_path = tmp_path / "ready-gate.json"
    assess_args = parser.parse_args(
        [
            "writing-material",
            "pilot",
            "assess-dry-run",
            "--report",
            str(dry_report_path),
            "--output",
            str(gate_path),
        ]
    )
    assess_args.hub_config = None
    assert run_writing_material_command(assess_args) == 0
    gate = json.loads(capsys.readouterr().out)
    assert gate["status"] == "ready"
    assert gate_path.stat().st_mode & 0o777 == 0o600

    preflight_path = tmp_path / "provider-preflight.json"
    preflight_args = parser.parse_args(
        [
            "writing-material",
            "pilot",
            "preflight-provider",
            "--gate-report",
            str(gate_path),
            "--output",
            str(preflight_path),
        ]
    )
    preflight_args.hub_config = None
    assert run_writing_material_command(preflight_args) == 0
    preflight = json.loads(capsys.readouterr().out)
    assert preflight["status"] == "ready"
    assert preflight["provider_client_created"] is False
    assert preflight_path.stat().st_mode & 0o777 == 0o600

    approval_path = tmp_path / "pilot-approval.json"
    approve_args = parser.parse_args(
        [
            "writing-material",
            "pilot",
            "approve-extraction",
            "--gate-report",
            str(gate_path),
            "--output",
            str(approval_path),
            "--approver",
            "fixture-approver",
            "--reviewer",
            "fixture-reviewer",
            "--rights-basis",
            "fixture-only use",
            "--retention-policy",
            "temporary test directory",
            "--access-policy",
            "test process only",
            "--yes",
        ]
    )
    approve_args.hub_config = None
    assert run_writing_material_command(approve_args) == 0
    approval = json.loads(capsys.readouterr().out)
    assert approval["status"] == "approved_for_small_batch_extraction"
    assert approval_path.stat().st_mode & 0o777 == 0o600

    approval_manifest = json.loads(approval_path.read_text(encoding="utf-8"))
    drifted_approval = {**approval_manifest, "version_bundle": "f" * 64}
    drifted_approval["artifact_fingerprint"] = sha256_json(
        {key: value for key, value in drifted_approval.items() if key != "artifact_fingerprint"}
    )
    drifted_path = tmp_path / "drifted-approval.json"
    drifted_path.write_text(json.dumps(drifted_approval), encoding="utf-8")
    rejected_args = parser.parse_args(
        [
            "writing-material",
            "extract",
            "--collection",
            "COLLKEY",
            "--limit",
            "30",
            "--pilot-approval",
            str(drifted_path),
        ]
    )
    rejected_args.hub_config = None
    assert run_writing_material_command(rejected_args) == 2
    rejected = json.loads(capsys.readouterr().out)
    assert "version bundle differs" in rejected["error"]
    assert not (tmp_path / "task-state").exists()
    assert not runtime.data_root.exists()

    extract_args = parser.parse_args(
        [
            "writing-material",
            "extract",
            "--collection",
            "COLLKEY",
            "--limit",
            "30",
            "--pilot-approval",
            str(approval_path),
        ]
    )
    extract_args.hub_config = None
    assert run_writing_material_command(extract_args) == 0
    result = json.loads(capsys.readouterr().out)
    manifest = json.loads((Path(result["run_dir"]) / "manifest.json").read_text(encoding="utf-8"))
    assert result["status"] == "success"
    assert result["processed"] == 30
    assert manifest["pilot_approval"]["artifact_fingerprint"] == approval["artifact_fingerprint"]
    assert not (runtime.data_root / "cache" / "llm").exists()


def test_dry_run_uses_only_selected_section_coverage(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    parsed = next((literature / "parsed" / "json").glob("*.json"))
    value = json.loads(parsed.read_text(encoding="utf-8"))
    value["structured"]["texts"].extend(
        [
            {
                "self_ref": "#/texts/2",
                "label": "section_header",
                "orig": "Method",
                "text": "Method",
                "prov": [],
            },
            {
                "self_ref": "#/texts/3",
                "label": "text",
                "orig": "Unmapped method text that must not lower introduction coverage.",
                "text": "Unmapped method text that must not lower introduction coverage.",
                "prov": [],
            },
        ]
    )
    value["structured"]["body"]["children"].extend([{"cref": "#/texts/2"}, {"cref": "#/texts/3"}])
    parsed.write_text(json.dumps(value), encoding="utf-8")
    markdown = next((literature / "parsed" / "markdown").glob("*.md"))
    markdown.write_text(markdown.read_text(encoding="utf-8") + "\n# Method\n", encoding="utf-8")
    config = _config(tmp_path, literature)
    result = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer()).extract(
        selection=_selection(tmp_path), sections=("introduction",), dry_run=True
    )
    assert result["status"] == "planned"
    assert result["planned"] == 1
    assert result["failed"] == 0


def test_extract_review_and_evidence_immutability(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    service = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer())
    result = service.extract(selection=_selection(tmp_path))
    run_id = str(result["run_id"])
    assert result["evidence"] == 1
    assert Path(str(result["run_dir"]), "evidence.jsonl").stat().st_mode & 0o777 == 0o600

    unchanged = service.extract(selection=_selection(tmp_path), dry_run=True)
    assert unchanged["skipped"] == 1
    review = WritingMaterialReviewService(config.data_root, literature)
    assert review.validate(run_id)["status"] == "success"
    records = review._records(review.run_dir(run_id))
    decisions = []
    for asset_type, values in records.items():
        identifier = {
            "evidence": "evidence_id",
            "strategy": "strategy_id",
            "template": "template_id",
            "phrase": "phrase_id",
        }[asset_type]
        for value in values:
            decisions.append(
                {
                    "asset_id": value[identifier],
                    "decision": "accepted",
                    "based_on_hash": based_on(value),
                    "reviewer": "fixture-reviewer",
                    "reason": "verified against fixture",
                }
            )
    path = tmp_path / "decisions.jsonl"
    path.write_text("".join(json.dumps(value) + "\n" for value in decisions), encoding="utf-8")
    applied = review.apply(run_id, path)
    assert applied["accepted_snapshot"]["counts"] == {
        "evidence": 1,
        "strategy": 1,
        "template": 1,
        "phrase": 1,
    }

    evidence = records["evidence"][0]
    bad = tmp_path / "bad-decision.jsonl"
    bad.write_text(
        json.dumps(
            {
                "asset_id": evidence["evidence_id"],
                "decision": "edited",
                "based_on_hash": based_on(evidence),
                "reviewer": "fixture-reviewer",
                "reason": "attempted rewrite",
                "edits": {"original_text": PARAGRAPH_TEXT.upper()},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ReviewValidationError, match="immutable"):
        review.apply(run_id, bad)

    class Summary:
        def to_dict(self) -> dict[str, object]:
            return {"status": "success", "selected": 3, "failures": []}

    class FakeIndexer:
        def build(self, values, *, knowledge_base, dry_run, prune):
            assert knowledge_base == "writing"
            assert dry_run is True
            assert prune is False
            assert len(values) == 3
            assert all(uuid.UUID(value.chunks[0].chunk_id) for value in values)
            assert len({value.chunks[0].chunk_id for value in values}) == 3
            assert all("original_text" not in value.document.metadata for value in values)
            assert all(value.document.metadata["accepted_snapshot_only"] for value in values)
            return Summary()

    candidate = WritingMaterialCandidateIndexer(review, RagConfig())
    with pytest.raises(ReviewValidationError, match="distinct physical"):
        candidate.build(
            run_id,
            candidate_collection="active-writing",
            active_collection="active-writing",
            candidate_data_dir=tmp_path / "candidate",
            dry_run=True,
            indexer=FakeIndexer(),
        )
    indexed = candidate.build(
        run_id,
        candidate_collection="writing-material-pilot-v1",
        active_collection="active-writing",
        candidate_data_dir=tmp_path / "candidate",
        dry_run=True,
        indexer=FakeIndexer(),
    )
    assert indexed["promotion_performed"] is False

    dry_candidate = tmp_path / "dry-candidate"
    dry_indexed = candidate.build(
        run_id,
        candidate_collection="writing-material-pilot-v2",
        active_collection="active-writing",
        candidate_data_dir=dry_candidate,
        dry_run=True,
    )
    assert dry_indexed["indexed"] == 3
    assert not dry_candidate.exists()

    accepted_phrase = review.run_dir(run_id) / "accepted" / "phrases.jsonl"
    original_snapshot = accepted_phrase.read_text(encoding="utf-8")
    accepted_phrase.write_text(
        original_snapshot.replace("remain limited", "rewritten"), encoding="utf-8"
    )
    invalid = review.validate(run_id)
    assert invalid["status"] == "failed"
    assert "accepted snapshot differs" in " ".join(invalid["errors"])


def test_document_assets_are_checkpointed_before_state_commit(tmp_path, monkeypatch) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)

    def interrupt_record(_self, _document, **_kwargs) -> None:
        raise KeyboardInterrupt("simulated process interruption")

    monkeypatch.setattr(ExtractionState, "record", interrupt_record)
    service = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer())
    with pytest.raises(KeyboardInterrupt, match="simulated process interruption"):
        service.extract(selection=_selection(tmp_path))

    run_dir = next((config.data_root / "runs").iterdir())
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "running"
    assert manifest["processed"] == 1
    assert manifest["evidence"] == 1
    assert "finished_at" not in manifest
    assert (run_dir / "evidence.jsonl").read_text(encoding="utf-8").strip()

    review = WritingMaterialReviewService(config.data_root, literature)
    with pytest.raises(ReviewValidationError, match="not complete"):
        review.render(str(manifest["run_id"]))
    with pytest.raises(ReviewValidationError, match="complete successful"):
        WritingMaterialCandidateIndexer(review, RagConfig()).build(
            str(manifest["run_id"]),
            candidate_collection="interrupted-candidate",
            active_collection="active-writing",
            candidate_data_dir=tmp_path / "interrupted-candidate",
            dry_run=True,
        )


def test_interrupted_run_resumes_from_verified_checkpoint_idempotently(
    tmp_path, monkeypatch
) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    service = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer())
    original_record = ExtractionState.record

    def interrupt_record(self, *args, **kwargs):
        raise RuntimeError("simulated commit interruption")

    monkeypatch.setattr(ExtractionState, "record", interrupt_record)
    with pytest.raises(RuntimeError, match="commit interruption"):
        service.extract(selection=_selection(tmp_path), run_id="resume-fixture")
    run_dir = config.data_root / "runs" / "resume-fixture"
    interrupted_manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert interrupted_manifest["status"] == "running"
    assert interrupted_manifest["checkpoint"]["schema_version"] == "writing-material-checkpoint-v1"
    assert Path(interrupted_manifest["selection"]) == run_dir / "selection.jsonl"

    monkeypatch.setattr(ExtractionState, "record", original_record)
    resumed = service.extract(resume_run_id="resume-fixture")
    assert resumed["status"] == "success"
    assert resumed["evidence"] == 1
    assert resumed["strategies"] == 1
    with sqlite3.connect(config.data_root / "state" / "extraction.sqlite3") as connection:
        attempts = connection.execute(
            "SELECT run_id,stage,status FROM attempts WHERE document_id=?",
            (DOCUMENT_ID,),
        ).fetchall()
    assert attempts == [("resume-fixture", "complete", "success")]


def test_resume_rejects_tampered_checkpoint_and_changed_source(tmp_path, monkeypatch) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    service = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer())
    original_record = ExtractionState.record

    def interrupt_record(self, *args, **kwargs):
        raise RuntimeError("simulated commit interruption")

    monkeypatch.setattr(ExtractionState, "record", interrupt_record)
    with pytest.raises(RuntimeError):
        service.extract(selection=_selection(tmp_path), run_id="tampered-resume")
    monkeypatch.setattr(ExtractionState, "record", original_record)
    run_dir = config.data_root / "runs" / "tampered-resume"
    evidence_path = run_dir / "evidence.jsonl"
    original_evidence = evidence_path.read_text(encoding="utf-8")
    evidence_path.write_text(original_evidence + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="checkpoint asset changed"):
        service.extract(resume_run_id="tampered-resume")

    evidence_path.write_text(original_evidence, encoding="utf-8")
    with sqlite3.connect(literature / "state" / "pipeline.sqlite3") as connection:
        connection.execute(
            "UPDATE pipeline_documents SET source_content_fingerprint='source-changed'"
        )
    with pytest.raises(ValueError, match="resume source changed"):
        service.extract(resume_run_id="tampered-resume")


def test_selection_failure_is_retryable_and_retry_refreshes_classification_cache(
    tmp_path,
) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    first = WritingMaterialExtractionService(config, analyzer=InvalidSelectionAnalyzer()).extract(
        selection=_selection(tmp_path)
    )
    assert first["status"] == "partial"
    assert first["failed"] == 1
    assert first["evidence"] == 0
    assert ExtractionState(config.data_root).document(DOCUMENT_ID)["status"] == "failed"

    analyzer = RefreshTrackingAnalyzer()
    retried = WritingMaterialExtractionService(config, analyzer=analyzer).extract(
        selection=_selection(tmp_path), retry_failed=True
    )
    assert retried["status"] == "success"
    assert retried["dispositions"]["failed"] == 1
    assert analyzer.refresh_cache_values == [True]
    assert ExtractionState(config.data_root).document(DOCUMENT_ID)["status"] == "success"


def test_mixed_valid_and_low_quality_items_mark_document_partial(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    result = WritingMaterialExtractionService(config, analyzer=MixedQualityAnalyzer()).extract(
        selection=_selection(tmp_path)
    )
    assert result["status"] == "partial"
    assert result["evidence"] == 1
    state = ExtractionState(config.data_root).document(DOCUMENT_ID)
    assert state is not None
    assert state["status"] == "partial"
    assert (
        ExtractionState(config.data_root).disposition(
            ProvenanceDocumentReader(literature).load(DOCUMENT_ID), config.version_bundle
        )
        == "failed"
    )


def test_changed_source_document_is_reprocessed(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    selection = _selection(tmp_path)
    service = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer())
    assert service.extract(selection=selection)["status"] == "success"
    with sqlite3.connect(literature / "state" / "pipeline.sqlite3") as connection:
        connection.execute(
            "UPDATE pipeline_documents SET source_content_fingerprint='source-2' "
            "WHERE document_id=?",
            (DOCUMENT_ID,),
        )
    planned = service.extract(selection=selection, dry_run=True)
    assert planned["dispositions"]["changed"] == 1
    assert planned["stale_reasons"] == {"source_content_changed": 1}
    assert planned["planned"] == 1
    reprocessed = service.extract(selection=selection)
    assert reprocessed["status"] == "success"
    state = ExtractionState(config.data_root).document(DOCUMENT_ID)
    assert state is not None
    assert state["source_content_fingerprint"] == "source-2"


def test_supported_parser_version_and_fingerprint_change_requires_reprocessing(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    selection = _selection(tmp_path)
    service = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer())
    assert service.extract(selection=selection)["status"] == "success"

    with sqlite3.connect(literature / "state" / "pipeline.sqlite3") as connection:
        connection.execute(
            "UPDATE pipeline_documents SET parser_version='2.112.1',parse_fingerprint='parse-2' "
            "WHERE document_id=?",
            (DOCUMENT_ID,),
        )
    artifact = literature / "parsed" / "json" / f"{safe_document_name(DOCUMENT_ID)}.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["parser_version"] = "2.112.1"
    payload["parse_fingerprint"] = "parse-2"
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    planned = service.extract(selection=selection, dry_run=True)
    assert planned["status"] == "planned"
    assert planned["dispositions"]["changed"] == 1
    assert planned["planned"] == 1


def test_source_revalidation_detects_provenance_location_drift(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    result = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer()).extract(
        selection=_selection(tmp_path)
    )
    artifact = literature / "parsed" / "json" / f"{safe_document_name(DOCUMENT_ID)}.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["structured"]["texts"][1]["prov"][0]["charspan"] = [1, len(PARAGRAPH_TEXT)]
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    validation = WritingMaterialReviewService(config.data_root, literature).validate(
        str(result["run_id"])
    )
    assert validation["status"] == "failed"
    assert "source segment mapping changed" in " ".join(validation["errors"])


def test_prompt_model_and_taxonomy_changes_invalidate_or_reject(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    selection = _selection(tmp_path)
    assert (
        WritingMaterialExtractionService(config, analyzer=FakeAnalyzer()).extract(
            selection=selection
        )["status"]
        == "success"
    )

    config.classify_prompt_path.write_text("changed classification prompt", encoding="utf-8")
    prompt_changed = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer()).extract(
        selection=selection, dry_run=True
    )
    assert prompt_changed["dispositions"]["changed"] == 1
    assert any(
        reason.startswith("version_changed:") and "classify_prompt_hash" in reason
        for reason in prompt_changed["stale_reasons"]
    )

    model_changed = WritingMaterialExtractionService(
        replace(config, model="fixture-model-v2"), analyzer=FakeAnalyzer()
    ).extract(selection=selection, dry_run=True)
    assert model_changed["dispositions"]["changed"] == 1

    taxonomy = config.taxonomy_path.read_text(encoding="utf-8")
    config.taxonomy_path.write_text(
        taxonomy.replace("writing-taxonomy-v1", "writing-taxonomy-v2", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="taxonomy file does not match"):
        WritingMaterialExtractionService(config, analyzer=FakeAnalyzer())


def test_abstraction_failure_preserves_evidence_and_blocks_partial_index(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    result = WritingMaterialExtractionService(
        config, analyzer=FailingAbstractionAnalyzer()
    ).extract(selection=_selection(tmp_path))
    run_id = str(result["run_id"])
    assert result["status"] == "partial"
    assert result["evidence"] == 1
    assert (Path(str(result["run_dir"])) / "evidence.jsonl").read_text(encoding="utf-8").strip()
    assert ExtractionState(config.data_root).document(DOCUMENT_ID)["status"] == "failed"

    review = WritingMaterialReviewService(config.data_root, literature)
    validation = review.validate(run_id)
    assert validation["status"] == "partial"
    assert validation["index_eligible"] is False
    with pytest.raises(ReviewValidationError, match="complete successful"):
        WritingMaterialCandidateIndexer(review, RagConfig()).build(
            run_id,
            candidate_collection="partial-run-candidate",
            active_collection="active-writing",
            candidate_data_dir=tmp_path / "partial-run-candidate",
            dry_run=True,
        )


def test_request_partition_bounds_classification_and_abstraction_batches(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = replace(
        _config(tmp_path, literature),
        classification_max_sentences_per_request=1,
        abstraction_batch_size=1,
    )
    analyzer = PartitionTrackingAnalyzer()
    result = WritingMaterialExtractionService(config, analyzer=analyzer).extract(
        selection=_selection(tmp_path)
    )
    assert result["status"] == "success"
    assert result["evidence"] == 2
    assert analyzer.classification_sentence_counts == [1, 1]
    assert analyzer.abstraction_batch_sizes == [1, 1]
    manifest = json.loads(
        (Path(str(result["run_dir"])) / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["version_manifest"]["classification_max_sentences_per_request"] == 1
    assert manifest["version_manifest"]["abstraction_batch_size"] == 1
    assert manifest["generation_limits"]["classification_max_sentences_per_request"] == 1
    assert manifest["generation_limits"]["abstraction_batch_size"] == 1


def test_abstraction_token_truncation_splits_batch_until_structured_output_succeeds(
    tmp_path,
) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = replace(
        _config(tmp_path, literature),
        classification_max_sentences_per_request=1,
        abstraction_batch_size=2,
    )
    analyzer = AdaptiveSplitAnalyzer()
    result = WritingMaterialExtractionService(config, analyzer=analyzer).extract(
        selection=_selection(tmp_path)
    )
    assert result["status"] == "success"
    assert result["processed"] == 1
    assert result["failed"] == 0
    assert analyzer.abstraction_batch_sizes == [2, 1, 1]
    manifest = json.loads(
        (Path(str(result["run_dir"])) / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["generation_limits"]["abstraction_adaptive_split_on_truncation"] is True
    assert manifest["generation_limits"]["abstraction_min_evidence_per_retry"] == 1


def test_mid_classification_failure_does_not_checkpoint_partial_document_evidence(
    tmp_path,
) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = replace(_config(tmp_path, literature), classification_max_sentences_per_request=1)
    analyzer = FailingSecondClassificationAnalyzer()
    result = WritingMaterialExtractionService(config, analyzer=analyzer).extract(
        selection=_selection(tmp_path)
    )
    assert result["status"] == "partial"
    assert result["processed"] == 0
    assert result["failed"] == 1
    assert result["evidence"] == 0
    assert analyzer.classification_calls == 2
    run_dir = Path(str(result["run_dir"]))
    assert (run_dir / "evidence.jsonl").read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("field", "replacement", "remove"),
    [
        ("schema_version", "evidence-v999", False),
        ("taxonomy_version", "writing-taxonomy-v999", False),
        ("prompt_version", "different-prompt", False),
        ("attachment_key", None, True),
        ("source_spans", [], False),
    ],
)
def test_stored_evidence_schema_provenance_and_trace_drift_are_rejected(
    tmp_path, field, replacement, remove
) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    result = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer()).extract(
        selection=_selection(tmp_path)
    )
    run_id = str(result["run_id"])
    path = Path(str(result["run_dir"])) / "evidence.jsonl"
    record = json.loads(path.read_text(encoding="utf-8"))
    if remove:
        record.pop(field)
    else:
        record[field] = replacement
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    validation = WritingMaterialReviewService(config.data_root, literature).validate(run_id)
    assert validation["status"] == "failed"
    assert validation["errors"]


def test_stored_material_unknown_field_is_rejected(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    result = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer()).extract(
        selection=_selection(tmp_path)
    )
    path = Path(str(result["run_dir"])) / "strategies.jsonl"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["surprise"] = True
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    validation = WritingMaterialReviewService(config.data_root, literature).validate(
        str(result["run_id"])
    )
    assert validation["status"] == "failed"
    assert "closed-world" in " ".join(validation["errors"])


def test_stored_material_category_must_be_supported_by_referenced_evidence(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    result = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer()).extract(
        selection=_selection(tmp_path)
    )
    path = Path(str(result["run_dir"])) / "strategies.jsonl"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["category"] = "gap_identification"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    validation = WritingMaterialReviewService(config.data_root, literature).validate(
        str(result["run_id"])
    )
    assert validation["status"] == "failed"
    assert "category is unsupported" in " ".join(validation["errors"])


def test_review_projection_requires_explicit_partial_snapshot(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    result = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer()).extract(
        selection=_selection(tmp_path)
    )
    run_id = str(result["run_id"])
    review = WritingMaterialReviewService(config.data_root, literature)
    rendered = review.render(run_id)
    assert rendered["review_counts"] == {
        "pending": 4,
        "accepted": 0,
        "edited": 0,
        "rejected": 0,
    }
    assert "Pending: 4" in Path(str(rendered["review_report"])).read_text(encoding="utf-8")

    evidence = review._records(review.run_dir(run_id))["evidence"][0]
    decision = {
        "asset_id": evidence["evidence_id"],
        "decision": "accepted",
        "based_on_hash": based_on(evidence),
        "reviewer": "fixture-reviewer",
        "reason": "source checked",
    }
    decisions = tmp_path / "partial-decisions.jsonl"
    decisions.write_text(json.dumps(decision) + "\n", encoding="utf-8")
    with pytest.raises(ReviewValidationError, match="3 pending"):
        review.apply(run_id, decisions)
    assert not (review.run_dir(run_id) / "review-events.jsonl").exists()

    applied = review.apply(run_id, decisions, allow_partial_snapshot=True)
    snapshot = applied["accepted_snapshot"]
    assert snapshot["review_completeness"] == "partial"
    assert snapshot["pending_count"] == 3
    assert snapshot["index_eligible"] is False
    assert Path(str(snapshot["path"])).name == "accepted-partial"
    assert review.validate(run_id)["index_eligible"] is False
    with pytest.raises(ReviewValidationError, match="complete successful"):
        WritingMaterialCandidateIndexer(review, RagConfig()).build(
            run_id,
            candidate_collection="partial-review-candidate",
            active_collection="active-writing",
            candidate_data_dir=tmp_path / "partial-review-candidate",
            dry_run=True,
        )
    (review.run_dir(run_id) / "review-status.jsonl").write_text("", encoding="utf-8")
    invalid = review.validate(run_id)
    assert invalid["status"] == "failed"
    assert "review status projection differs" in " ".join(invalid["errors"])


def test_review_edited_rejected_complete_snapshot_and_idempotent_import(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    result = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer()).extract(
        selection=_selection(tmp_path)
    )
    run_id = str(result["run_id"])
    review = WritingMaterialReviewService(config.data_root, literature)
    records = review._records(review.run_dir(run_id))
    evidence = records["evidence"][0]
    strategy = records["strategy"][0]
    template = records["template"][0]
    phrase = records["phrase"][0]
    decisions = [
        {
            "asset_id": evidence["evidence_id"],
            "decision": "accepted",
            "based_on_hash": based_on(evidence),
            "reviewer": "fixture-reviewer",
            "reason": "source checked",
        },
        {
            "asset_id": strategy["strategy_id"],
            "decision": "edited",
            "based_on_hash": based_on(strategy),
            "reviewer": "fixture-reviewer",
            "reason": "clarify scope",
            "edits": {"description": "Use the contrast only for scoped limitations."},
        },
        {
            "asset_id": template["template_id"],
            "decision": "rejected",
            "based_on_hash": based_on(template),
            "reviewer": "fixture-reviewer",
            "reason": "too specific",
        },
        {
            "asset_id": phrase["phrase_id"],
            "decision": "accepted",
            "based_on_hash": based_on(phrase),
            "reviewer": "fixture-reviewer",
            "reason": "reusable",
        },
    ]
    path = tmp_path / "complete-decisions.jsonl"
    path.write_text("".join(json.dumps(value) + "\n" for value in decisions), encoding="utf-8")
    applied = review.apply(run_id, path)
    snapshot = applied["accepted_snapshot"]
    assert snapshot["review_completeness"] == "complete"
    assert snapshot["pending_count"] == 0
    assert snapshot["counts"] == {"evidence": 1, "strategy": 1, "template": 0, "phrase": 1}
    accepted_strategy = json.loads(
        (Path(str(snapshot["path"])) / "strategies.jsonl").read_text(encoding="utf-8")
    )
    assert accepted_strategy["description"] == "Use the contrast only for scoped limitations."
    assert accepted_strategy["review_status"] == "edited"
    assert accepted_strategy["reviewed_from_hash"] == based_on(strategy)
    assert accepted_strategy["review_reviewer"] == "fixture-reviewer"
    audit_fields = {
        "review_status",
        "reviewed_from_hash",
        "review_decision_id",
        "review_reviewer",
        "review_timestamp",
        "review_reason",
        "materialized_hash",
    }
    materialized = {
        key: value for key, value in accepted_strategy.items() if key not in audit_fields
    }
    assert accepted_strategy["materialized_hash"] == based_on(materialized)
    validation = review.validate(run_id)
    assert validation["review_counts"] == {
        "pending": 0,
        "accepted": 2,
        "edited": 1,
        "rejected": 1,
    }
    assert validation["index_eligible"] is True

    imported_again = review.apply(run_id, path)
    assert imported_again["events_appended"] == 0
    assert imported_again["duplicate_events_ignored"] == 4


def test_review_dependency_rejection_latest_event_and_conflict_detection(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    result = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer()).extract(
        selection=_selection(tmp_path)
    )
    run_id = str(result["run_id"])
    review = WritingMaterialReviewService(config.data_root, literature)
    records = review._records(review.run_dir(run_id))
    decisions = []
    for asset_type, values in records.items():
        field = {
            "evidence": "evidence_id",
            "strategy": "strategy_id",
            "template": "template_id",
            "phrase": "phrase_id",
        }[asset_type]
        for value in values:
            decisions.append(
                {
                    "asset_id": value[field],
                    "decision": "rejected" if asset_type == "evidence" else "accepted",
                    "based_on_hash": based_on(value),
                    "reviewer": "fixture-reviewer",
                    "reason": "dependency test",
                }
            )
    path = tmp_path / "dependency-decisions.jsonl"
    path.write_text("".join(json.dumps(value) + "\n" for value in decisions), encoding="utf-8")
    snapshot = review.apply(run_id, path)["accepted_snapshot"]
    assert snapshot["review_completeness"] == "complete"
    assert snapshot["dependency_exclusion_count"] == 3
    assert snapshot["counts"] == {"evidence": 0, "strategy": 0, "template": 0, "phrase": 0}

    evidence = records["evidence"][0]
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(
        json.dumps(
            {
                "asset_id": evidence["evidence_id"],
                "decision": "accepted",
                "based_on_hash": based_on(evidence),
                "reviewer": "fixture-reviewer",
                "reason": "second source check passed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    replaced = review.apply(run_id, replacement)["accepted_snapshot"]
    assert replaced["counts"] == {"evidence": 1, "strategy": 1, "template": 1, "phrase": 1}

    conflict = tmp_path / "conflict.jsonl"
    conflict.write_text(
        json.dumps(decisions[0]) + "\n" + json.dumps(decisions[0]) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ReviewValidationError, match="duplicate asset"):
        review.apply(run_id, conflict)

    stale = dict(decisions[1])
    stale["based_on_hash"] = "0" * 64
    stale_path = tmp_path / "stale.jsonl"
    stale_path.write_text(json.dumps(stale) + "\n", encoding="utf-8")
    with pytest.raises(ReviewValidationError, match="stale"):
        review.apply(run_id, stale_path)
