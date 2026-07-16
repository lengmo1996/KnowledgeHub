"""Deterministic default and protocol for pluggable writing analysis."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, Sequence

_CITATION = re.compile(r"\[(?:\d+[ ,;-]*)+\]|\([A-Z][A-Za-z-]+(?: et al\.)?,? \d{4}[a-z]?\)")
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?%?\b")
_FORMULA = re.compile(r"[$\\][^\n]{2,}|[=<>]\s*[A-Za-z0-9_{}^]+")
_GAP = re.compile(
    r"\b(however|nevertheless|despite|although|remain(?:s)?|lack(?:s|ing)?|limited|"
    r"challenge|few studies|has not|have not|still (?:fails?|requires?|suffers?))\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class WritingAnalysis:
    writing_function: str
    normalized_text: str
    abstract_pattern: str
    rhetorical_structure: tuple[str, ...]
    usage_notes: str
    quality_score: float
    confidence: float


class WritingAnalyzer(Protocol):
    name: str
    version: str

    def analyze(self, text: str, *, section: str, domains: Sequence[str]) -> WritingAnalysis | None: ...


class RuleWritingAnalyzer:
    name = "rules"
    version = "rules-v1"

    def analyze(
        self, text: str, *, section: str, domains: Sequence[str]
    ) -> WritingAnalysis | None:
        value = " ".join(text.split())
        if len(value) < 60 or len(value) > 1800 or _FORMULA.search(value):
            return None
        function = self._function(value, section)
        if function is None:
            return None
        normalized = _NUMBER.sub("[VALUE]", _CITATION.sub("[CITATION]", value))
        pattern, structure, notes = self._pattern(function)
        quality = 0.55
        if 90 <= len(value) <= 900:
            quality += 0.15
        if not re.match(r"^(This|It|They|These|Those)\b", value):
            quality += 0.1
        if _CITATION.search(value):
            quality -= 0.05
        if value[-1:] in ".!?":
            quality += 0.05
        quality = max(0.0, min(1.0, quality))
        return WritingAnalysis(
            writing_function=function,
            normalized_text=normalized,
            abstract_pattern=pattern,
            rhetorical_structure=structure,
            usage_notes=notes,
            quality_score=quality,
            confidence=0.78 if function == "research_gap" else 0.65,
        )

    @staticmethod
    def _function(text: str, section: str) -> str | None:
        lowered = section.lower()
        if "intro" in lowered:
            if _GAP.search(text):
                return "research_gap"
            if re.search(r"we (?:propose|present|introduce)|our contribution", text, re.I):
                return "contribution_statement"
            if re.search(r"motiv|therefore|to address", text, re.I):
                return "motivation"
            return "research_context"
        if "related" in lowered:
            return "method_comparison" if re.search(r"whereas|unlike|compared", text, re.I) else "method_summary"
        if "method" in lowered or "approach" in lowered:
            return "design_rationale" if re.search(r"because|in order to|allows us", text, re.I) else "method_overview"
        if "experiment" in lowered or "result" in lowered:
            return "result_interpretation" if re.search(r"indicat|suggest|because", text, re.I) else "experimental_setup"
        if "conclu" in lowered:
            return "future_work" if re.search(r"future|remain|limitation", text, re.I) else "work_summary"
        return None

    @staticmethod
    def _pattern(function: str) -> tuple[str, tuple[str, ...], str]:
        values = {
            "research_gap": (
                "Although [prior progress or prevailing approach], [unresolved limitation or missing capability] remains.",
                ("acknowledge_prior_progress", "identify_limitation"),
                "Use to connect established progress to a precise, evidence-backed research gap.",
            ),
            "contribution_statement": (
                "To address [problem], we introduce [method or contribution], which [main capability].",
                ("state_problem", "present_contribution", "preview_benefit"),
                "Use after the gap and motivation have been established.",
            ),
            "motivation": (
                "This limitation motivates [research direction] to achieve [desired capability].",
                ("identify_limitation", "motivate_direction"),
                "Use as a transition from the gap to the proposed approach.",
            ),
            "research_context": (
                "[Research topic] has become important because [context or impact].",
                ("establish_topic", "explain_significance"),
                "Use to establish context before narrowing to the specific problem.",
            ),
            "method_comparison": (
                "Whereas [method A] emphasizes [property], [method B] addresses [contrasting property].",
                ("describe_first_method", "contrast_second_method"),
                "Use only when both sides of the comparison are supported by citations.",
            ),
            "method_summary": (
                "Prior work approaches [problem] through [method family or principle].",
                ("identify_work", "summarize_method"),
                "Use for concise, source-backed related-work summaries.",
            ),
            "design_rationale": (
                "We adopt [design choice] because it enables [desired property].",
                ("state_design", "justify_design"),
                "Use when the causal link between design and benefit is demonstrable.",
            ),
            "method_overview": (
                "The proposed method consists of [components] that jointly perform [objective].",
                ("introduce_method", "summarize_components"),
                "Use at the beginning of a method section.",
            ),
            "result_interpretation": (
                "The results indicate [finding], suggesting that [supported interpretation].",
                ("report_finding", "interpret_finding"),
                "Keep interpretation within the evidence supplied by the experiment.",
            ),
            "experimental_setup": (
                "We evaluate [method] on [data or setting] using [metrics and protocol].",
                ("state_subject", "describe_protocol"),
                "Use to make experimental conditions reproducible.",
            ),
            "future_work": (
                "Future work will investigate [open limitation] in [broader setting].",
                ("acknowledge_limitation", "propose_future_direction"),
                "Use for concrete limitations rather than generic aspirations.",
            ),
            "work_summary": (
                "This work addressed [problem] by introducing [approach] and demonstrated [finding].",
                ("restate_problem", "summarize_approach", "state_finding"),
                "Use for a concise conclusion without introducing new evidence.",
            ),
        }
        return values[function]
