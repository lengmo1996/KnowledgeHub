from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledgehub.core.hashing import sha256_json
from knowledgehub.writing_rag.pilot import (
    AcceptedCorpusQualityAuditor,
    AcceptedCorpusQualityReviewRenderer,
    QualityAuditPolicy,
    _deduplicate_text_segments,
    _normalized_quality_text,
    _quality_findings,
)
from knowledgehub.writing_rag.review import ReviewValidationError

from .test_release import _reviewed_run


def test_quality_audit_clean_fixture_is_fingerprinted_private_and_read_only(
    tmp_path: Path,
) -> None:
    review, run_id = _reviewed_run(tmp_path)
    accepted_dir = review.run_dir(run_id) / "accepted"
    before = {
        path.name: path.read_bytes()
        for path in accepted_dir.iterdir()
        if path.is_file()
    }

    report = AcceptedCorpusQualityAuditor(review).audit(run_id)

    assert report["status"] == "success"
    assert report["passed"] is True
    assert report["counts"]["assets"]["total"] == 3
    assert report["source_text_included"] is False
    assert report["review_decisions_modified"] is False
    assert report["accepted_snapshot_modified"] is False
    assert report["index_modified"] is False
    assert report["llm_called"] is False
    assert report["writes_performed"] is False
    assert report["artifact_fingerprint"] == sha256_json(
        {key: value for key, value in report.items() if key != "artifact_fingerprint"}
    )
    after = {
        path.name: path.read_bytes()
        for path in accepted_dir.iterdir()
        if path.is_file()
    }
    assert after == before
    serialized = json.dumps(report)
    assert "original_text" not in serialized
    assert "template_text" not in serialized


def test_quality_findings_detect_repetition_duplicates_length_and_low_score() -> None:
    repeated = "A sufficiently long repeated segment for validation. " * 3
    assets = {
        "strategy": [
            {
                "strategy_id": "strategy:one",
                "category": "result_reporting",
                "language": "en",
                "quality_score": 0.70,
                "cluster_id": "cluster:shared",
                "label": "Fixture",
                "description": repeated,
                "steps": ["same list item", "same list item"],
                "applicability": "A" * 801,
                "claim_strength_guidance": "Moderate",
                "explanation_zh": "测试说明",
                "explanation_en": "Fixture explanation",
            },
            {
                "strategy_id": "strategy:two",
                "category": "result_reporting",
                "language": "en",
                "quality_score": 0.90,
                "cluster_id": "cluster:shared",
                "label": "Fixture two",
                "description": repeated,
                "steps": ["one step"],
                "applicability": "Fixture",
                "claim_strength_guidance": "Moderate",
                "explanation_zh": "测试说明二",
                "explanation_en": "Fixture explanation two",
            },
        ],
        "template": [],
        "phrase": [],
    }

    findings, clusters = _quality_findings(assets, QualityAuditPolicy())
    codes = {value["code"] for value in findings}

    assert codes == {
        "exact_duplicate_primary_text",
        "low_quality_score",
        "near_duplicate_cluster",
        "oversized_text_field",
        "repeated_list_item",
        "repeated_text_segment",
    }
    assert clusters == [
        {
            "asset_type": "strategy",
            "cluster_id": "cluster:shared",
            "size": 2,
            "asset_ids": ["strategy:one", "strategy:two"],
        }
    ]
    assert repeated.strip() not in json.dumps(findings)


def test_quality_audit_rejects_incomplete_review_and_invalid_policy(tmp_path: Path) -> None:
    class IncompleteReview:
        def validate(self, _run_id: str, *, verify_source: bool) -> dict[str, object]:
            assert verify_source is True
            return {"status": "partial", "index_eligible": False}

    with pytest.raises(ValueError, match="source-verified complete review"):
        AcceptedCorpusQualityAuditor(IncompleteReview()).audit("run-1")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="minimum score"):
        QualityAuditPolicy(minimum_quality_score=1.1).validate()
    with pytest.raises(ValueError, match="must be positive"):
        QualityAuditPolicy(maximum_text_field_characters=0).validate()


def test_quality_normalization_is_unicode_and_whitespace_stable() -> None:
    assert (
        _normalized_quality_text("\uff21\uff22\uff23\u3000Template\nText")
        == "abc template text"
    )


def test_quality_cleanup_removes_repeated_and_truncated_tail_segments() -> None:
    complete = "Use calibrated language rather than unsupported superlatives."
    value = f"{complete} Keep the evidence scope explicit. {complete} Use calibrated language"

    cleaned = _deduplicate_text_segments(value, QualityAuditPolicy())

    assert cleaned == f"{complete} Keep the evidence scope explicit."


def test_quality_review_packet_proposes_edits_without_changing_review_state(
    tmp_path: Path,
) -> None:
    review, run_id = _reviewed_run(tmp_path)
    run_dir = review.run_dir(run_id)
    raw_template = review._records(run_dir)["template"][0]
    repeated_segment = "This guidance sentence is deliberately repeated for review."
    decision = {
        "asset_id": raw_template["template_id"],
        "decision": "edited",
        "based_on_hash": sha256_json(raw_template),
        "reviewer": "fixture-reviewer",
        "reason": "create a bounded quality fixture",
        "edits": {
            "claim_strength_guidance": " ".join([repeated_segment] * 3),
        },
    }
    decision_path = tmp_path / "quality-fixture-decision.jsonl"
    decision_path.write_text(json.dumps(decision) + "\n", encoding="utf-8")
    review.apply(run_id, decision_path)
    audit = AcceptedCorpusQualityAuditor(review).audit(run_id)
    assert audit["counts"]["findings"] == {"repeated_text_segment": 1}
    accepted_dir = review.accepted_dir(run_id)
    evidence = json.loads(
        (accepted_dir / "evidence.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    original_text = evidence["original_text"]
    protected_paths = [
        run_dir / "review-events.jsonl",
        *(path for path in accepted_dir.iterdir() if path.is_file()),
    ]
    before = {str(path): path.read_bytes() for path in protected_paths}

    output_dir = tmp_path / "quality-review"
    packet = AcceptedCorpusQualityReviewRenderer(review).render(
        run_id,
        quality_report=audit,
        reviewer="lengmo",
        output_dir=output_dir,
    )

    assert packet["status"] == "success"
    assert packet["counts"] == {
        "flagged_assets": 1,
        "recommendations": {"edit_repeated_content": 1},
    }
    assert packet["decision_import_ready"] is False
    assert packet["requires_explicit_reviewer_decision"] is True
    assert packet["evidence_text_included"] is False
    assert packet["provenance_excerpt_included"] is False
    assert packet["derived_material_text_included"] is True
    assert packet["review_decisions_modified"] is False
    assert packet["accepted_snapshot_modified"] is False
    assert packet["index_modified"] is False
    assert packet["llm_called"] is False
    item = packet["items"][0]
    assert item["recommended_action"] == "edit_repeated_content"
    assert item["decision_draft"]["decision"] is None
    assert item["decision_draft"]["reason"] is None
    proposed = item["proposed_edits"]["claim_strength_guidance"]
    assert proposed.count(repeated_segment) == 1
    assert packet["artifact_fingerprint"] == sha256_json(
        {key: value for key, value in packet.items() if key != "artifact_fingerprint"}
    )
    packet_path = output_dir / "quality-review-packet.json"
    markdown_path = output_dir / "quality-review.md"
    assert oct(output_dir.stat().st_mode & 0o777) == "0o700"
    assert oct(packet_path.stat().st_mode & 0o777) == "0o600"
    assert oct(markdown_path.stat().st_mode & 0o777) == "0o600"
    assert original_text not in packet_path.read_text(encoding="utf-8")
    assert original_text not in markdown_path.read_text(encoding="utf-8")
    after = {str(path): path.read_bytes() for path in protected_paths}
    assert after == before
    with pytest.raises(ValueError, match="refusing to overwrite"):
        AcceptedCorpusQualityReviewRenderer(review).render(
            run_id,
            quality_report=audit,
            reviewer="lengmo",
            output_dir=output_dir,
        )


def test_quality_review_packet_rejects_tampered_or_unknown_findings(tmp_path: Path) -> None:
    review, run_id = _reviewed_run(tmp_path)
    audit = AcceptedCorpusQualityAuditor(review).audit(run_id)
    assert audit["passed"] is True
    with pytest.raises(ValueError, match="valid failed quality audit"):
        AcceptedCorpusQualityReviewRenderer(review).render(
            run_id,
            quality_report=audit,
            reviewer="lengmo",
            output_dir=tmp_path / "clean-report",
        )

    tampered = dict(audit)
    tampered["passed"] = False
    with pytest.raises(ValueError, match="fingerprint"):
        AcceptedCorpusQualityReviewRenderer(review).render(
            run_id,
            quality_report=tampered,
            reviewer="lengmo",
            output_dir=tmp_path / "tampered-report",
        )


def test_quality_review_import_is_dry_run_first_versioned_and_stale_safe(
    tmp_path: Path,
) -> None:
    review, run_id = _reviewed_run(tmp_path)
    run_dir = review.run_dir(run_id)
    legacy_dir = review.accepted_dir(run_id)
    legacy_before = {
        path.name: path.read_bytes() for path in legacy_dir.iterdir() if path.is_file()
    }
    raw_template = review._records(run_dir)["template"][0]
    repeated_segment = "This guidance sentence is deliberately repeated for review."
    fixture_decision = {
        "asset_id": raw_template["template_id"],
        "decision": "edited",
        "based_on_hash": sha256_json(raw_template),
        "reviewer": "fixture-reviewer",
        "reason": "create a bounded quality fixture",
        "edits": {
            "claim_strength_guidance": " ".join([repeated_segment] * 3),
            "constraints": ["Keep this prior reviewer constraint."],
        },
    }
    fixture_path = tmp_path / "quality-fixture-decision.jsonl"
    fixture_path.write_text(json.dumps(fixture_decision) + "\n", encoding="utf-8")
    review.apply(run_id, fixture_path)
    first_revision = review.accepted_dir(run_id)
    assert first_revision != legacy_dir
    assert first_revision.parent.name == "accepted-revisions"
    first_revision_before = {
        path.name: path.read_bytes() for path in first_revision.iterdir() if path.is_file()
    }

    audit = AcceptedCorpusQualityAuditor(review).audit(run_id)
    packet = AcceptedCorpusQualityReviewRenderer(review).render(
        run_id,
        quality_report=audit,
        reviewer="lengmo",
        output_dir=tmp_path / "quality-import",
    )
    packet_path = Path(packet["packet_path"])
    decision = dict(packet["items"][0]["decision_draft"])
    decision["decision"] = "edited"
    decision["reason"] = "remove repeated generated guidance"
    decisions_path = tmp_path / "quality-decisions.jsonl"
    decisions_path.write_text(json.dumps(decision) + "\n", encoding="utf-8")
    events_before = (run_dir / "review-events.jsonl").read_bytes()
    index_root = review.data_root / "index-candidates"
    index_existed = index_root.exists()

    planned = review.apply_quality_review(
        run_id,
        packet_path=packet_path,
        decisions_path=decisions_path,
        dry_run=True,
    )

    assert planned["status"] == "planned"
    assert planned["decision_count"] == 1
    assert planned["writes_performed"] is False
    assert planned["review_events_modified"] is False
    assert planned["accepted_snapshot_modified"] is False
    assert planned["index_modified"] is False
    assert (run_dir / "review-events.jsonl").read_bytes() == events_before
    assert review.accepted_dir(run_id) == first_revision
    with pytest.raises(ReviewValidationError, match="explicit confirmation"):
        review.apply_quality_review(
            run_id,
            packet_path=packet_path,
            decisions_path=decisions_path,
        )

    applied = review.apply_quality_review(
        run_id,
        packet_path=packet_path,
        decisions_path=decisions_path,
        confirmed=True,
    )

    second_revision = review.accepted_dir(run_id)
    assert applied["status"] == "success"
    assert applied["imported"] == 1
    assert applied["review_events_modified"] is True
    assert applied["accepted_snapshot_modified"] is True
    assert applied["index_modified"] is False
    assert second_revision != first_revision
    assert {
        path.name: path.read_bytes() for path in legacy_dir.iterdir() if path.is_file()
    } == legacy_before
    assert {
        path.name: path.read_bytes() for path in first_revision.iterdir() if path.is_file()
    } == first_revision_before
    accepted_template = json.loads(
        (second_revision / "templates.jsonl").read_text(encoding="utf-8")
    )
    assert accepted_template["claim_strength_guidance"].count(repeated_segment) == 1
    assert accepted_template["constraints"] == ["Keep this prior reviewer constraint."]
    pointer = run_dir / "accepted-current.json"
    assert oct(pointer.stat().st_mode & 0o777) == "0o600"
    assert review.validate(run_id)["index_eligible"] is True
    assert index_root.exists() is index_existed
    with pytest.raises(ReviewValidationError, match="invalid or stale"):
        review.apply_quality_review(
            run_id,
            packet_path=packet_path,
            decisions_path=decisions_path,
            dry_run=True,
        )


def test_quality_review_import_rejects_null_or_incomplete_decisions(tmp_path: Path) -> None:
    review, run_id = _reviewed_run(tmp_path)
    run_dir = review.run_dir(run_id)
    raw_template = review._records(run_dir)["template"][0]
    repeated = "This guidance sentence is deliberately repeated for review."
    fixture_path = tmp_path / "fixture.jsonl"
    fixture_path.write_text(
        json.dumps(
            {
                "asset_id": raw_template["template_id"],
                "decision": "edited",
                "based_on_hash": sha256_json(raw_template),
                "reviewer": "fixture-reviewer",
                "reason": "create quality finding",
                "edits": {"claim_strength_guidance": " ".join([repeated] * 3)},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    review.apply(run_id, fixture_path)
    audit = AcceptedCorpusQualityAuditor(review).audit(run_id)
    packet = AcceptedCorpusQualityReviewRenderer(review).render(
        run_id,
        quality_report=audit,
        reviewer="lengmo",
        output_dir=tmp_path / "packet",
    )
    null_path = tmp_path / "null.jsonl"
    null_path.write_text(
        json.dumps(packet["items"][0]["decision_draft"]) + "\n", encoding="utf-8"
    )
    with pytest.raises(ReviewValidationError, match=r"empty identity|invalid decision"):
        review.apply_quality_review(
            run_id,
            packet_path=Path(packet["packet_path"]),
            decisions_path=null_path,
            dry_run=True,
        )
    empty_path = tmp_path / "empty.jsonl"
    empty_path.write_text("", encoding="utf-8")
    with pytest.raises(ReviewValidationError, match="cover every packet item"):
        review.apply_quality_review(
            run_id,
            packet_path=Path(packet["packet_path"]),
            decisions_path=empty_path,
            dry_run=True,
        )
