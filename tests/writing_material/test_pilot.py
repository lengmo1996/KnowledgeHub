from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import replace
from types import SimpleNamespace

import pytest

from knowledgehub.cli.writing_material import _run_pilot_retrieval_command
from knowledgehub.core.hashing import sha256_json
from knowledgehub.pipeline.config import RagConfig
from knowledgehub.writing_rag.extract import (
    EXTRACTION_DRY_RUN_SCHEMA_VERSION,
    FIXTURE_PROVIDER,
    WritingMaterialExtractionService,
)
from knowledgehub.writing_rag.pilot import (
    CandidateRetrievalEvaluator,
    ControlledPilotEvaluator,
    PilotPolicy,
    PilotRetrievalOutcome,
    create_pilot_approval,
    provider_preflight,
)
from knowledgehub.writing_rag.provenance import ProvenanceDocumentReader
from knowledgehub.writing_rag.review import (
    WritingMaterialCandidateIndexer,
    WritingMaterialReviewService,
    _index_input,
)

from .helpers import based_on, build_literature_fixture
from .test_extract_review import FakeAnalyzer, MixedQualityAnalyzer, _config, _selection
from .test_release import _reviewed_run


def _dry_run_report(selected: int = 30) -> dict:
    version_manifest = {"fixture": "v1"}
    report = {
        "schema_version": EXTRACTION_DRY_RUN_SCHEMA_VERSION,
        "status": "planned",
        "dry_run": True,
        "selected": selected,
        "planned": selected,
        "failed": 0,
        "candidates": selected * 2,
        "selection_sha256": "a" * 64,
        "sections": ["conclusion", "experiment", "introduction"],
        "literature_checkpoint": {"sequence": 1, "sync_id": "fixture"},
        "version_bundle": sha256_json(version_manifest),
        "version_manifest": version_manifest,
        "planning_gates": {
            "provenance_passed": selected,
            "provenance_failed": 0,
            "section_candidate_documents": selected - 2,
            "zero_candidate_documents": 2,
        },
    }
    report["artifact_fingerprint"] = sha256_json(report)
    return report


def _approval(gate: dict, path, config, *, confirmed: bool = True) -> dict:
    return create_pilot_approval(
        gate,
        output=path,
        approver="fixture-approver",
        reviewer="fixture-reviewer",
        rights_basis="fixture-only source use",
        retention_policy="delete with temporary test directory",
        access_policy="test process only",
        provider=config.provider,
        model=config.effective_model,
        confirmed=confirmed,
    )


def _candidate_report(review, run_id: str, tmp_path, *, dry_run: bool = False) -> dict:
    accepted = json.loads(
        (review.run_dir(run_id) / "accepted" / "manifest.json").read_text(encoding="utf-8")
    )
    selected = sum(accepted["counts"][key] for key in ("strategy", "template", "phrase"))

    class Summary:
        def to_dict(self) -> dict:
            return {
                "status": "success",
                "selected": selected,
                "indexed": selected,
                "skipped": 0,
                "chunks": selected,
                "tombstoned": 0,
                "failures": [],
                "dry_run": dry_run,
                "knowledge_base": "writing",
            }

    class FakeIndexer:
        def build(self, values, *, knowledge_base, dry_run: bool, prune):
            assert len(values) == selected
            assert knowledge_base == "writing"
            assert prune is False
            return Summary()

    return WritingMaterialCandidateIndexer(review, RagConfig()).build(
        run_id,
        candidate_collection=f"writing-material-pilot-{run_id[-8:]}",
        active_collection="active-writing",
        candidate_data_dir=tmp_path / "mock-candidate",
        dry_run=dry_run,
        indexer=FakeIndexer(),
    )


def _retrieval_fixture(review, run_id: str, candidate_report: dict, *, query_count: int = 5):
    accepted_dir = review.run_dir(run_id) / "accepted"
    evidences = {
        value["evidence_id"]: value
        for value in (
            json.loads(line)
            for line in (accepted_dir / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
            if line
        )
    }
    strategy = json.loads(
        (accepted_dir / "strategies.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    index_input = _index_input("strategy", strategy, evidences)
    payload = {
        **dict(index_input.chunks[0].metadata),
        "document_id": strategy["strategy_id"],
    }
    cases = [
        {
            "schema_version": "writing-material-retrieval-case-v1",
            "case_id": f"case-{index}",
            "query": f"fixture query {index}",
            "expected_asset_ids": [strategy["strategy_id"]],
            "top_k": 5,
        }
        for index in range(1, query_count + 1)
    ]

    def query(_query: str, _top_k: int) -> PilotRetrievalOutcome:
        return PilotRetrievalOutcome(
            collection=candidate_report["candidate_collection"],
            hits=(payload,),
        )

    return cases, query, payload


def test_pilot_dry_run_assessment_enforces_size_and_gate_counts(tmp_path) -> None:
    review, _run_id = _reviewed_run(tmp_path)
    evaluator = ControlledPilotEvaluator(review)

    ready = evaluator.assess_dry_run(_dry_run_report())
    assert ready["status"] == "ready"
    assert ready["gates"] == {
        "selection_size": True,
        "provenance": True,
        "no_provenance_failures": True,
        "section_candidates": True,
    }
    assert ready["real_llm_called"] is False
    assert ready["writes_performed"] is False
    assert ready["automatic_expansion_performed"] is False
    assert ready["policy"]["maximum_document_failure_rate"] == 0.0
    assert ready["policy"]["maximum_exact_span_rejection_rate"] == 0.0
    assert ready["policy"]["maximum_provider_failure_rate"] == 0.0
    assert ready["policy"]["require_zero_provenance_failures"] is True
    assert ready["artifact_fingerprint"] == sha256_json(
        {key: value for key, value in ready.items() if key != "artifact_fingerprint"}
    )

    too_small = evaluator.assess_dry_run(_dry_run_report(29))
    assert too_small["status"] == "stopped"
    assert too_small["gates"]["selection_size"] is False

    inconsistent = _dry_run_report()
    inconsistent["planning_gates"]["provenance_failed"] = 1
    inconsistent["artifact_fingerprint"] = sha256_json(
        {key: value for key, value in inconsistent.items() if key != "artifact_fingerprint"}
    )
    with pytest.raises(ValueError, match="inconsistent"):
        evaluator.assess_dry_run(inconsistent)

    one_failure = _dry_run_report()
    one_failure["planned"] = 29
    one_failure["failed"] = 1
    one_failure["planning_gates"] = {
        "provenance_passed": 29,
        "provenance_failed": 1,
        "section_candidate_documents": 27,
        "zero_candidate_documents": 2,
    }
    one_failure["artifact_fingerprint"] = sha256_json(
        {key: value for key, value in one_failure.items() if key != "artifact_fingerprint"}
    )
    stopped_for_partial = evaluator.assess_dry_run(one_failure)
    assert stopped_for_partial["status"] == "stopped"
    assert stopped_for_partial["gates"]["provenance"] is True
    assert stopped_for_partial["gates"]["no_provenance_failures"] is False

    tampered = _dry_run_report()
    tampered["selected"] = 31
    with pytest.raises(ValueError, match="fingerprint"):
        evaluator.assess_dry_run(tampered)


def test_provider_preflight_is_network_free_and_reports_environment_presence(
    tmp_path, monkeypatch
) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    selection = _selection(tmp_path)
    base = _config(tmp_path, literature)
    config = replace(base, model="approved-model").validate()
    gate = ControlledPilotEvaluator(
        WritingMaterialReviewService(config.data_root, literature),
        PilotPolicy(min_documents=1, max_documents=1),
    ).assess_dry_run(
        WritingMaterialExtractionService(config).extract(
            selection=selection,
            dry_run=True,
        )
    )
    monkeypatch.delenv(config.base_url_env, raising=False)
    monkeypatch.delenv(config.api_key_env, raising=False)

    stopped = provider_preflight(gate, config)
    assert stopped["status"] == "stopped"
    assert stopped["environment"]["base_url_configured"] is False
    assert stopped["environment"]["api_key_configured"] is False
    assert stopped["network_request_performed"] is False
    assert stopped["provider_client_created"] is False
    assert stopped["secret_values_emitted"] is False
    assert "http" not in json.dumps(stopped).lower()

    monkeypatch.setenv(config.base_url_env, "not-a-url")
    invalid = provider_preflight(gate, config)
    assert invalid["status"] == "stopped"
    assert invalid["environment"]["base_url_configured"] is True
    assert invalid["gates"]["base_url_valid_or_fixture"] is False

    monkeypatch.setenv(config.base_url_env, "http://not-contacted.invalid/v1")
    invalid_path = provider_preflight(gate, config)
    assert invalid_path["status"] == "stopped"
    assert invalid_path["gates"]["base_url_valid_or_fixture"] is False

    monkeypatch.setenv(config.base_url_env, "http://not-contacted.invalid")
    ready = provider_preflight(gate, config)
    assert ready["status"] == "ready"
    assert ready["schema_version"] == "writing-material-provider-preflight-v2"
    assert ready["environment"]["base_url_configured"] is True
    assert "not-contacted" not in json.dumps(ready)

    changed = replace(config, model="changed-model").validate()
    with pytest.raises(ValueError, match="version bundle"):
        provider_preflight(gate, changed)


def test_explicit_pilot_approval_is_bound_before_extraction_writes(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature")
    config = replace(
        _config(tmp_path, literature),
        provider=FIXTURE_PROVIDER,
        model="",
    ).validate(require_provider=True)
    selection = _selection(tmp_path)
    service = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer())
    dry_run = service.extract(selection=selection, dry_run=True)
    gate = ControlledPilotEvaluator(
        WritingMaterialReviewService(config.data_root, literature),
        PilotPolicy(min_documents=1, max_documents=1),
    ).assess_dry_run(dry_run)
    approval = _approval(gate, tmp_path / "approval.json", config)

    drifted = {**approval, "selection_sha256": "b" * 64}
    drifted["artifact_fingerprint"] = sha256_json(
        {key: value for key, value in drifted.items() if key != "artifact_fingerprint"}
    )
    with pytest.raises(ValueError, match="selection differs"):
        service.extract(selection=selection, pilot_approval=drifted)
    assert not config.data_root.exists()

    provider_drift = {**approval, "model": "unapproved-model"}
    provider_drift["artifact_fingerprint"] = sha256_json(
        {
            key: value
            for key, value in provider_drift.items()
            if key != "artifact_fingerprint"
        }
    )
    with pytest.raises(ValueError, match="provider/model differs"):
        service.extract(selection=selection, pilot_approval=provider_drift)
    assert not config.data_root.exists()

    version_drift = {**approval, "version_bundle": "c" * 64}
    version_drift["artifact_fingerprint"] = sha256_json(
        {
            key: value
            for key, value in version_drift.items()
            if key != "artifact_fingerprint"
        }
    )
    with pytest.raises(ValueError, match="version bundle differs"):
        service.extract(selection=selection, pilot_approval=version_drift)
    assert not config.data_root.exists()

    manifest = service.extract(selection=selection, pilot_approval=approval)
    assert manifest["status"] == "success"
    assert manifest["pilot_approval"]["artifact_fingerprint"] == approval[
        "artifact_fingerprint"
    ]
    assert manifest["pilot_approval"]["approver"] == "fixture-approver"


def test_approved_pilot_stops_after_first_partial_document(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature", document_count=2)
    config = replace(
        _config(tmp_path, literature),
        provider=FIXTURE_PROVIDER,
        model="",
    ).validate(require_provider=True)
    service = WritingMaterialExtractionService(config, analyzer=MixedQualityAnalyzer())
    dry_run = service.extract(collections=["COLLKEY"], limit=2, dry_run=True)
    gate = ControlledPilotEvaluator(
        WritingMaterialReviewService(config.data_root, literature),
        PilotPolicy(min_documents=2, max_documents=2),
    ).assess_dry_run(dry_run)
    approval = _approval(gate, tmp_path / "approval.json", config)

    result = service.extract(
        collections=["COLLKEY"],
        limit=2,
        pilot_approval=approval,
    )

    assert result["status"] == "partial"
    assert result["processed"] == 1
    assert result["failed"] == 1
    assert result["evidence"] == 1


def test_pilot_approval_requires_confirmation_ready_gate_and_immutable_output(tmp_path) -> None:
    review, _run_id = _reviewed_run(tmp_path)
    gate = ControlledPilotEvaluator(review).assess_dry_run(_dry_run_report())
    config = replace(
        _config(tmp_path, tmp_path / "literature"),
        provider=FIXTURE_PROVIDER,
        model="",
    ).validate(require_provider=True)
    output = tmp_path / "approval.json"

    with pytest.raises(ValueError, match="--yes"):
        _approval(gate, output, config, confirmed=False)
    assert not output.exists()

    approval = _approval(gate, output, config)
    assert approval["status"] == "approved_for_small_batch_extraction"
    assert approval["secret_included"] is False
    assert approval["production_index_authorized"] is False
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="overwrite"):
        _approval(gate, output, config)

    stopped = ControlledPilotEvaluator(review).assess_dry_run(_dry_run_report(29))
    with pytest.raises(ValueError, match="ready"):
        _approval(stopped, tmp_path / "stopped.json", config)

    unsafe_policy = {
        **gate,
        "policy": {**gate["policy"], "maximum_exact_span_rejection_rate": 0.2},
    }
    unsafe_policy["artifact_fingerprint"] = sha256_json(
        {
            key: value
            for key, value in unsafe_policy.items()
            if key != "artifact_fingerprint"
        }
    )
    with pytest.raises(ValueError, match="partial-run index ban"):
        _approval(unsafe_policy, tmp_path / "unsafe-policy.json", config)


def test_pilot_evaluation_requires_review_candidate_and_retrieval_gates(tmp_path) -> None:
    review, run_id = _reviewed_run(tmp_path)
    evaluator = ControlledPilotEvaluator(
        review,
        PilotPolicy(min_documents=1, max_documents=1),
    )

    incomplete = evaluator.evaluate(run_id)
    assert incomplete["status"] == "incomplete"
    assert incomplete["review"]["complete"] is True
    assert incomplete["recommendation"] == "build_or_fix_isolated_candidate"

    candidate = _candidate_report(review, run_id, tmp_path)
    cases, query, _payload = _retrieval_fixture(review, run_id, candidate)
    retrieval = CandidateRetrievalEvaluator(review).evaluate(
        run_id,
        candidate_report=candidate,
        cases=cases,
        query=query,
    )
    complete = evaluator.evaluate(
        run_id, candidate_report=candidate, retrieval_report=retrieval
    )
    assert complete["status"] == "eligible_for_manual_expansion_decision"
    assert all(complete["gates"].values())
    assert complete["manual_expansion_decision_required"] is True
    assert complete["automatic_expansion_performed"] is False
    assert "original_text" not in json.dumps(complete)


def test_pilot_rejects_dry_run_candidate_as_completed_candidate(tmp_path) -> None:
    review, run_id = _reviewed_run(tmp_path)
    evaluator = ControlledPilotEvaluator(
        review,
        PilotPolicy(min_documents=1, max_documents=1),
    )
    report = evaluator.evaluate(
        run_id, candidate_report=_candidate_report(review, run_id, tmp_path, dry_run=True)
    )
    assert report["candidate"]["passed"] is False
    assert report["recommendation"] == "build_or_fix_isolated_candidate"


def test_default_thirty_document_mock_pilot_closes_all_non_external_gates(tmp_path) -> None:
    literature = build_literature_fixture(tmp_path / "literature", document_count=30)
    config = _config(tmp_path, literature)
    document_ids = tuple(sorted(ProvenanceDocumentReader(literature).documents()))
    result = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer()).extract(
        document_ids=document_ids
    )
    assert result["selected"] == result["processed"] == 30
    assert result["failed"] == 0
    review = WritingMaterialReviewService(config.data_root, literature)
    run_id = str(result["run_id"])
    records = review._records(review.run_dir(run_id))
    id_fields = {
        "evidence": "evidence_id",
        "strategy": "strategy_id",
        "template": "template_id",
        "phrase": "phrase_id",
    }
    decisions = [
        {
            "asset_id": value[id_fields[asset_type]],
            "decision": "accepted",
            "based_on_hash": based_on(value),
            "reviewer": "mock-pilot-reviewer",
            "reason": "fixture source and abstraction verified",
        }
        for asset_type, values in records.items()
        for value in values
    ]
    assert len(decisions) == 120
    decisions_path = tmp_path / "mock-pilot-decisions.jsonl"
    decisions_path.write_text(
        "".join(json.dumps(value) + "\n" for value in decisions), encoding="utf-8"
    )
    review.apply(run_id, decisions_path)

    candidate_report = _candidate_report(review, run_id, tmp_path)
    cases, query, _payload = _retrieval_fixture(
        review, run_id, candidate_report, query_count=6
    )
    retrieval_report = CandidateRetrievalEvaluator(review).evaluate(
        run_id,
        candidate_report=candidate_report,
        cases=cases,
        query=query,
    )
    report = ControlledPilotEvaluator(review).evaluate(
        run_id,
        candidate_report=candidate_report,
        retrieval_report=retrieval_report,
    )
    assert report["status"] == "eligible_for_manual_expansion_decision"
    assert report["counts"]["accepted_assets"] == {
        "evidence": 30,
        "strategy": 30,
        "template": 30,
        "phrase": 30,
    }
    assert all(report["gates"].values())
    candidate_manifest = tmp_path / "mock-candidate" / "writing-material-candidate.json"
    assert candidate_manifest.is_file()
    assert candidate_manifest.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "mock-candidate" / "state").exists()


def test_retrieval_evaluator_rejects_source_join_drift_and_unknown_expectation(tmp_path) -> None:
    review, run_id = _reviewed_run(tmp_path)
    candidate = _candidate_report(review, run_id, tmp_path)
    cases, _query, payload = _retrieval_fixture(review, run_id, candidate)
    drifted = {**payload, "evidence_ids": ["evidence:unknown"]}

    def bad_query(_query: str, _top_k: int) -> PilotRetrievalOutcome:
        return PilotRetrievalOutcome(
            collection=candidate["candidate_collection"],
            hits=(drifted,),
        )

    report = CandidateRetrievalEvaluator(review).evaluate(
        run_id,
        candidate_report=candidate,
        cases=cases,
        query=bad_query,
    )
    assert report["status"] == "failed"
    assert report["source_join_failures"] == 5
    assert report["metrics"]["source_join_rate"] == 0.0
    evaluated = ControlledPilotEvaluator(
        review, PilotPolicy(min_documents=1, max_documents=1)
    ).evaluate(run_id, candidate_report=candidate, retrieval_report=report)
    assert evaluated["gates"]["retrieval_quality"] is False

    invalid_cases = [dict(cases[0], expected_asset_ids=["strategy:unknown"])]
    with pytest.raises(ValueError, match="expected asset IDs"):
        CandidateRetrievalEvaluator(review).evaluate(
            run_id,
            candidate_report=candidate,
            cases=invalid_cases,
            query=bad_query,
        )


def test_pilot_rejects_tampered_candidate_and_retrieval_fingerprints(tmp_path) -> None:
    review, run_id = _reviewed_run(tmp_path)
    candidate = _candidate_report(review, run_id, tmp_path)
    cases, query, _payload = _retrieval_fixture(review, run_id, candidate)
    retrieval = CandidateRetrievalEvaluator(review).evaluate(
        run_id,
        candidate_report=candidate,
        cases=cases,
        query=query,
    )
    tampered_candidate = {**candidate, "indexed": 2}
    with pytest.raises(ValueError, match="verified isolated candidate"):
        CandidateRetrievalEvaluator(review).evaluate(
            run_id,
            candidate_report=tampered_candidate,
            cases=cases,
            query=query,
        )
    tampered_retrieval = {**retrieval, "source_join_failures": 1}
    result = ControlledPilotEvaluator(
        review, PilotPolicy(min_documents=1, max_documents=1)
    ).evaluate(
        run_id,
        candidate_report=candidate,
        retrieval_report=tampered_retrieval,
    )
    assert result["retrieval"]["fingerprint_valid"] is False
    assert result["gates"]["retrieval_quality"] is False


def test_pilot_retrieval_cli_composition_is_read_only_and_defaults_to_sparse(
    tmp_path, monkeypatch
) -> None:
    review, run_id = _reviewed_run(tmp_path)
    candidate = _candidate_report(review, run_id, tmp_path)
    cases, _query, payload = _retrieval_fixture(review, run_id, candidate)
    candidate_path = tmp_path / "candidate.json"
    queries_path = tmp_path / "queries.jsonl"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    queries_path.write_text(
        "".join(json.dumps(value) + "\n" for value in cases), encoding="utf-8"
    )

    class Pool:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Service:
        def __init__(self) -> None:
            self.endpoint_pool = Pool()
            self.reranker = None
            self.requests = []

        def search(self, request):
            self.requests.append(request)
            return SimpleNamespace(
                collection=candidate["candidate_collection"],
                hits=(SimpleNamespace(payload=payload),),
                warnings=(),
            )

    service = Service()
    monkeypatch.setattr(
        "knowledgehub.services.search_api.build_retrieval", lambda _config: service
    )
    result = _run_pilot_retrieval_command(
        Namespace(
            run_id=run_id,
            candidate_report=candidate_path,
            queries=queries_path,
            mode="sparse",
        ),
        SimpleNamespace(rag_config=lambda _knowledge_base: RagConfig()),
        review,
    )
    assert result["status"] == "success"
    assert len(service.requests) == 5
    assert all(request.mode == "sparse" for request in service.requests)
    assert service.endpoint_pool.closed is True
    assert result["writes_performed"] is False
    assert "original_text" not in json.dumps(result)
