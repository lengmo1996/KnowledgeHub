from __future__ import annotations

from pathlib import Path

from knowledgehub.evaluation.metrics import evaluate_rankings
from knowledgehub.workflows.repository import RepositoryIntake
from knowledgehub.writing_rag.v2 import (
    WritingFeedbackStore,
    paragraph_structure,
    similarity_risk,
    writing_profile,
)


def test_repository_intake_and_conservative_matrix(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("torch>=2.0,<2.2\n", encoding="utf-8")
    (repo / "train.py").write_text("import torch\ntorch.compile(model)\n", encoding="utf-8")
    result = RepositoryIntake(repo).analyze(
        {"name": "current", "packages": {"torch": "2.6.0"}}, tmp_path / "reports"
    )
    assert result["compatibility_matrix"][0]["status"] == "conflict"
    assert result["profile"]["api_usage"][0]["library"] == "torch"
    assert Path(result["report"]).is_file()


def test_writing_structure_similarity_profile_and_feedback(tmp_path: Path) -> None:
    structure = paragraph_structure(
        "Prior work is effective. However, it remains limited. We propose a robust method.",
        "Introduction",
    )
    assert structure["moves"] == ["establish_context", "identify_gap", "introduce_solution"]
    risk = similarity_risk(
        "this exact phrase should be detected",
        [{"source_id": "s1", "text": "this exact phrase should be detected in a source"}],
        n=3,
    )
    assert risk["risk_level"] == "high" and risk["legal_plagiarism_assessment"] is False
    profile = writing_profile(
        [{"original_text": "A short paragraph.", "writing_function": "research_gap"}],
        profile_type="venue",
        name="selected papers",
    )
    assert profile["evidence_source"] == "user_selected_literature"
    feedback = WritingFeedbackStore(tmp_path / "feedback.sqlite3")
    feedback.submit("w1", "useful")
    feedback.submit("w1", "too_similar")
    assert feedback.adjustment("w1") < 0


def test_evaluation_metrics_are_groupable() -> None:
    metrics = evaluate_rankings(
        [{"expected_source": "source_code", "version": "1", "expected_symbol": "A.f"}],
        [[{"source_type": "release_note", "version": "2"}, {"source_type": "source_code", "version": "1", "symbol": "A.f"}]],
        k=2,
    )
    assert metrics == {
        "recall_at_k": 1.0,
        "mrr": 0.5,
        "correct_version_recall": 1.0,
        "correct_symbol_recall": 1.0,
    }
