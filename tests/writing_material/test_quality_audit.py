from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledgehub.core.hashing import sha256_json
from knowledgehub.writing_rag.pilot import (
    AcceptedCorpusQualityAuditor,
    QualityAuditPolicy,
    _normalized_quality_text,
    _quality_findings,
)

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
