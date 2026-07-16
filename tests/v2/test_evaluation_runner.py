from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from knowledgehub.cli.main import build_parser
from knowledgehub.evaluation.metrics import evaluate_code
from knowledgehub.evaluation.runner import (
    EvaluationRunner,
    QueryOutcome,
    _live_query_filters,
    compare_reports,
    load_thresholds,
    write_report,
)

ROOT = Path(__file__).parents[2]


def test_evaluation_cli_is_additive() -> None:
    args = build_parser().parse_args(
        ["evaluate", "run", "--domain", "writing", "--mode", "offline"]
    )
    assert args.source == "evaluate"
    assert args.evaluation_command == "run"
    assert args.domain == "writing"


def test_offline_evaluation_is_grouped_and_deterministic() -> None:
    report = EvaluationRunner(ROOT / "eval").run(domain="all", mode="offline")
    assert report["summary"]["failed_groups"] == []
    assert "code/api_usage" in report["groups"]
    assert report["groups"]["code/api_usage"]["warnings"] == [
        "retrieval_metrics_require_live_mode"
    ]
    assert report["groups"]["writing/function_classification"]["metrics"] == {
        "writing_function_accuracy": 1.0
    }
    assert report["groups"]["writing/section_classification"]["metrics"] == {
        "section_accuracy": 1.0
    }
    assert "overall_score" not in report["summary"]


def test_live_evaluation_uses_observed_hits_not_fixture_oracles() -> None:
    def query(
        domain: str,
        sample: Mapping[str, Any],
        profile: str,
        top_k: int,
    ) -> QueryOutcome:
        assert profile == "v2" and top_k == 3
        if domain == "writing":
            return QueryOutcome(
                (
                    {
                        "document_id": "w1",
                        "writing_function": sample["expected_function"],
                        "source_paper_id": "p1",
                        "section": sample["section"],
                        "research_domain": sample.get("expected_domains") or [],
                    },
                ),
                0.01,
            )
        return QueryOutcome(
            (
                {
                    "source_type": sample["expected_source"],
                    "library": sample["library"],
                    "version": sample["version"],
                    "symbol": sample.get("expected_symbol"),
                    "source_url": "https://example.test/source",
                    "inference": False,
                },
            ),
            0.02,
        )

    report = EvaluationRunner(ROOT / "eval", query=query).run(
        domain="all", mode="live", profile="v2", top_k=3
    )
    assert report["groups"]["code/api_usage"]["metrics"]["recall_at_k"] == 1.0
    writing = report["groups"]["writing/pattern_retrieval"]["metrics"]
    assert writing["function_recall_at_k"] == 1.0
    assert writing["source_traceability_rate"] == 1.0


def test_live_query_filters_never_use_expected_answers_as_inputs() -> None:
    sample = {
        "library": "demo",
        "version": "1.0",
        "symbol": "Requested.symbol",
        "expected_symbol": "Expected.only",
        "expected_function": "research_gap",
    }
    assert _live_query_filters(sample) == {
        "library": "demo",
        "version": "1.0",
        "symbol": "Requested.symbol",
    }


def test_report_comparison_is_fail_closed_and_persisted(tmp_path: Path) -> None:
    baseline = {
        "run_id": "v1",
        "groups": {
            "writing/function_classification": {
                "metrics": {"writing_function_accuracy": 1.0}
            }
        },
    }
    candidate = {
        "run_id": "v2",
        "groups": {
            "writing/function_classification": {
                "metrics": {"writing_function_accuracy": 0.8}
            }
        },
    }
    thresholds = {
        "groups": {
            "writing/function_classification": {
                "writing_function_accuracy": {"min": 0.9, "max_drop": 0.0}
            }
        }
    }
    comparison = compare_reports(baseline, candidate, thresholds)
    assert comparison["passed"] is False
    assert comparison["failed_gates"] == [
        "writing/function_classification.writing_function_accuracy"
    ]
    path = write_report(tmp_path / "comparison.json", comparison)
    assert json.loads(path.read_text(encoding="utf-8"))["passed"] is False
    assert load_thresholds(ROOT / "configs" / "evaluation" / "v2.yaml")[
        "schema_version"
    ] == 1


def test_code_metrics_use_applicable_version_and_symbol_denominators() -> None:
    metrics = evaluate_code(
        [
            {
                "expected_source": "source_code",
                "version": "2",
                "expected_symbol": "A.f",
            },
            {
                "expected_source": "release_note",
                "version": None,
                "expected_symbol": None,
            },
        ],
        [
            [
                {
                    "source_type": "source_code",
                    "version": "2",
                    "symbol": "A.f",
                    "source_url": "https://example.test/a",
                }
            ],
            [{"source_type": "release_note", "version": "2", "commit": "abc"}],
        ],
        latencies=[0.1, 0.2],
    )
    assert metrics["correct_version_recall"] == 1.0
    assert metrics["correct_symbol_recall"] == 1.0
    assert metrics["mean_latency_seconds"] == 0.15
