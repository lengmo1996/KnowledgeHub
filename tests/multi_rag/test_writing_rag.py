from __future__ import annotations

from knowledgehub.writing_rag.analyzer import RuleWritingAnalyzer
from knowledgehub.writing_rag.derive import WritingDerivationService


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
        ("Introduction", 1, "Although prior work is effective, it remains limited in difficult settings."),
        ("Motivation", 1, "This limitation motivates a robust approach for practical deployment."),
    ]
