from __future__ import annotations

from knowledgehub.writing_rag.analyzer import RuleWritingAnalyzer
from knowledgehub.writing_rag.derive import WritingDerivationService
from knowledgehub.writing_rag.sections import section_family


def test_rule_analyzer_extracts_research_gap_pattern() -> None:
    text = (
        "Although diffusion models have achieved strong generation quality, "
        "they remain limited when only small thermal infrared datasets are available."
    )
    result = RuleWritingAnalyzer().analyze(text, section="Introduction", domains=("vision",))
    assert result is not None
    assert result.writing_function == "research_gap"
    assert "[unresolved limitation" in result.abstract_pattern
    assert result.rhetorical_structure == (
        "acknowledge_prior_progress",
        "identify_limitation",
    )


def test_rule_analyzer_covers_background_comparison_and_limitation() -> None:
    analyzer = RuleWritingAnalyzer()
    background = analyzer.analyze(
        "Graph representation learning supports scientific discovery across many important application domains and increasingly complex relational datasets.",
        section="Introduction",
        domains=(),
    )
    comparison = analyzer.analyze(
        "Compared with the strongest baseline, our method achieves higher accuracy across every evaluated dataset and experimental configuration.",
        section="4.3 Quantitative results",
        domains=(),
    )
    limitation = analyzer.analyze(
        "A remaining limitation is the restricted evaluation scale, which cannot establish robustness for substantially larger deployment settings.",
        section="Conclusion",
        domains=(),
    )
    assert background and background.writing_function == "background"
    assert comparison and comparison.writing_function == "quantitative_comparison"
    assert limitation and limitation.writing_function == "limitation"


def test_paragraph_extraction_keeps_section_context_and_skips_references() -> None:
    markdown = """# Introduction

Although prior work is effective, it remains limited in difficult settings.

## Motivation

This limitation motivates a robust approach for practical deployment.

# References

[1] A reference entry.
"""
    values = list(WritingDerivationService._paragraphs(markdown))
    assert values == [
        (
            "Introduction",
            1,
            "Although prior work is effective, it remains limited in difficult settings.",
        ),
        ("Motivation", 1, "This limitation motivates a robust approach for practical deployment."),
    ]


def test_derived_entry_contains_v24_paragraph_and_filter_metadata(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service = WritingDerivationService(
        literature_data_dir=tmp_path,
        data_root=tmp_path,
        rag_config=None,
        processor_version="rules-v2",
    )
    markdown = """# Introduction

Prior work performs well in standard settings. However, it remains limited in difficult domains. We propose a robust method to address this gap.
"""
    entries = list(
        service._paper_entries(
            "paper-1",
            {
                "title": "Paper",
                "tags": ["NeurIPS", "vision"],
                "collections": [{"key": "C1", "path": "NeurIPS/2025"}],
            },
            markdown,
        )
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry.processor_version == "rules-v2"
    assert entry.venue == "NeurIPS"
    assert entry.moves == ("establish_context", "identify_gap", "introduce_solution")
    assert entry.paragraph_pattern
    indexed = service._index_input(entry)
    metadata = indexed.chunks[0].metadata
    assert metadata["paragraph_word_count"] > 0
    assert metadata["source_location"] == {"page": None, "paragraph": 1}


def test_section_family_accepts_subsections_without_title_substring_false_positive() -> None:
    assert section_family("4.3 Quantitative results") == "experiment"
    assert section_family("5.1 Time-series Latent ODE Experiments") == "experiment"
    assert section_family("G.4 Comparing GDA and vanilla methods") == "experiment"
    assert section_family("A Practical Approach to Small Data Learning") != "method"


def test_derivation_excludes_captions_short_lines_and_title_front_matter(tmp_path) -> None:
    service = WritingDerivationService(
        literature_data_dir=tmp_path,
        data_root=tmp_path,
        rag_config=None,
        processor_version="rules-v2",
    )
    markdown = """# A Practical Approach to Small Data Learning

Department of Computer Science, Example University

# 4.3 Quantitative results

Figure 1: Accuracy for every model and benchmark under all evaluation conditions.

The results indicate that combining visual and textual evidence consistently improves model robustness across all evaluated domains and perturbation settings.
"""
    entries = list(
        service._paper_entries(
            "paper-1",
            {"title": "A Practical Approach to Small Data Learning"},
            markdown,
        )
    )
    assert len(entries) == 1
    assert entries[0].source_section == "4.3 Quantitative results"
    assert entries[0].writing_function == "result_interpretation"
