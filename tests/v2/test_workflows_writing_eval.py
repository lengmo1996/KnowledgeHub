from __future__ import annotations

from pathlib import Path

import pytest

from knowledgehub.evaluation.metrics import evaluate_rankings, evaluate_writing
from knowledgehub.workflows.repository import RepositoryIntake
from knowledgehub.writing_rag.v2 import (
    WritingFeedbackStore,
    WritingProfileStore,
    WritingTaskPlanner,
    paragraph_features,
    paragraph_structure,
    similarity_risk,
    writing_profile,
)


def test_repository_intake_and_conservative_matrix(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("torch>=2.0,<2.2\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\ndependencies = ["transformers>=5"]\n[tool.ruff]\nselect = ["E4"]\n',
        encoding="utf-8",
    )
    (repo / "setup.py").write_text(
        '_deps = ["accelerate>=1", "ignored; python_version < \'3.0\'"]\n',
        encoding="utf-8",
    )
    (repo / "train.py").write_text("import torch\ntorch.compile(model)\n", encoding="utf-8")
    result = RepositoryIntake(repo).analyze(
        {"name": "current", "packages": {"torch": "2.6.0"}}, tmp_path / "reports"
    )
    matrix = {item["package"]: item for item in result["compatibility_matrix"]}
    assert matrix["torch"]["status"] == "conflict"
    assert "accelerate" in matrix
    assert "E4" not in matrix
    assert "ignored" not in matrix
    assert result["profile"]["api_usage"][0]["library"] == "torch"
    assert result["profile"]["api_inventory"]["truncated"] is False
    assert Path(result["report"]).is_file()


def test_repository_inventory_resolves_aliases_and_structural_usage(tmp_path: Path) -> None:
    repo = tmp_path / "aliased"
    repo.mkdir()
    (repo / "main.py").write_text(
        """import pytorch_lightning as pl
from diffusers import DiffusionPipeline as Pipeline

class Model(pl.LightningModule):
    pass

trainer = pl.Trainer(gpus=1)
pipe = Pipeline.from_pretrained('demo')
pl.Trainer.legacy_flag = True
if pl.__version__ < '2':
    pass
""",
        encoding="utf-8",
    )
    result = RepositoryIntake(repo).inspect({"name": "test", "packages": {}})
    usage = {item["library"]: item for item in result["profile"]["api_usage"]}
    lightning = usage["pytorch_lightning"]
    assert "pytorch_lightning.Trainer" in lightning["symbols"]
    assert lightning["call_sites"][0]["parameters"] == ["gpus"]
    assert lightning["inherited_symbols"][0]["base"] == "pytorch_lightning.LightningModule"
    assert lightning["monkey_patches"][0]["target"] == "pytorch_lightning.Trainer.legacy_flag"
    assert lightning["detected_version_assumptions"]
    assert "diffusers.DiffusionPipeline.from_pretrained" in usage["diffusers"]["symbols"]


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
    assert risk["layers"]["long_fragment"] == "evaluated"
    assert risk["layers"]["semantic"] == "not_evaluated"
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


def test_writing_profiles_keep_venue_and_personal_sources_separate(tmp_path: Path) -> None:
    store = WritingProfileStore(tmp_path / "profiles")
    entries = [
        {
            "source_paper_id": "paper-1",
            "source_section": "Introduction",
            "writing_function": "research_gap",
            "original_text": "However, prior methods remain limited for this challenging setting.",
            "content_hash": "h1",
        }
    ]
    venue = store.build_venue(
        entries,
        name="Selected Venue",
        paper_ids=["paper-1"],
        sections=["Introduction"],
    )
    assert venue["profile_type"] == "venue"
    assert venue["evidence_source"] == "user_selected_literature"
    assert venue["is_normative_rule"] is False
    assert venue["selection"]["section_families"] == ["introduction"]
    with pytest.raises(ValueError, match="explicit user-selected"):
        store.build_venue(entries, name="invalid", paper_ids=[])

    draft = tmp_path / "draft.md"
    draft.write_text(
        "We present a deliberately long paragraph from a user supplied draft so that "
        "the personal profile records stylistic statistics without treating literature "
        "or venue conventions as the writer's own historical preference.",
        encoding="utf-8",
    )
    personal = store.build_personal(name="My Drafts", drafts=[draft])
    assert personal["profile_type"] == "personal"
    assert personal["evidence_source"] == "user_supplied_drafts"
    assert {item["profile_type"] for item in store.list()} == {"venue", "personal"}


def test_personal_profile_supports_chinese_paragraphs(tmp_path: Path) -> None:
    draft = tmp_path / "draft-zh.md"
    draft.write_text(
        "本文关注红外视觉与跨模态图像生成, 并强调准确性、逻辑结构和可执行性。"
        "对于没有实验支持的结论, 需要进一步验证并明确标记不确定内容。\n\n"
        "实验结果表明该方法可能改善目标检测性能, 但是仍需在更多数据集上验证。"
        "因此, 技术说明应给出具体参数、执行步骤、风险和验收条件。",
        encoding="utf-8",
    )
    profile = WritingProfileStore(tmp_path / "profiles").build_personal(
        name="Chinese Draft", drafts=[draft]
    )
    assert profile["sample_count"] == 2
    assert profile["processor_version"] == "writing-profile-v2.5"
    assert profile["mean_sentence_words"] > 0
    assert profile["analysis_expression_rate"] == 0.5
    assert profile["common_terms"]


def test_writing_task_plan_and_style_facets() -> None:
    facets = paragraph_features(
        "However, our results clearly demonstrate a substantial improvement."
    )
    assert facets["tone"] == "assertive"
    assert facets["expression_strength"] == "strong"
    assert facets["first_person"] is True
    plan = WritingTaskPlanner().plan(
        "strengthen_argument",
        objective="make the evidence chain explicit",
        text="The result is useful.",
        filters={"section": "Experiment"},
    )
    assert plan["retrieval"]["return_mode"] == "paragraph_structure"
    assert plan["generation_boundary"].startswith("Writing RAG supplies")
    with pytest.raises(ValueError, match="requires input text"):
        WritingTaskPlanner().plan("rewrite_paragraph", objective="rewrite")


def test_evaluation_metrics_are_groupable() -> None:
    metrics = evaluate_rankings(
        [{"expected_source": "source_code", "version": "1", "expected_symbol": "A.f"}],
        [
            [
                {"source_type": "release_note", "version": "2"},
                {"source_type": "source_code", "version": "1", "symbol": "A.f"},
            ]
        ],
        k=2,
    )
    assert metrics == {
        "recall_at_k": 1.0,
        "mrr": 0.5,
        "correct_version_recall": 1.0,
        "correct_symbol_recall": 1.0,
    }

    writing = evaluate_writing(
        [
            {
                "expected_function": "research_gap",
                "expected_section": "Introduction",
                "expected_domains": ["vision"],
                "expected_similarity_risk": "high",
            }
        ],
        [
            {
                "writing_function": "research_gap",
                "section": "Introduction",
                "pattern_transferability_score": 0.8,
                "source_paper_id": "p1",
                "source_location": {"paragraph": 1},
                "accepted": True,
                "retrieved_domains": ["vision", "language"],
                "similarity_risk": "medium",
                "material_ids": ["w1", "w1"],
            }
        ],
    )
    assert writing["writing_function_accuracy"] == 1.0
    assert writing["source_traceability_rate"] == 1.0
    assert writing["duplicate_material_ratio"] == 0.5
    assert writing["wrong_domain_recall_rate"] == 0.5
