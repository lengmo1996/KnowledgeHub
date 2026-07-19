"""Conservative normalization for academic section headings."""

from __future__ import annotations

import re

_NUMBERING = re.compile(
    r"^\s*(?:(?:section|chapter|appendix)\s+)?"
    r"(?:[a-z]\.\d+(?:\.\d+)*|\d+(?:\.\d+)*|[ivxlcdm]+)"
    r"(?:\s*[.):-]\s*|\s+)",
    re.I,
)
_SEPARATORS = re.compile(r"[^a-z0-9\u3400-\u4dbf\u4e00-\u9fff]+")

_FAMILIES: dict[str, set[str]] = {
    "introduction": {
        "introduction",
        "introduction and motivation",
        "overview",
        "引",
        "引言",
        "绪论",
    },
    "related_work": {
        "background",
        "related work",
        "related works",
        "literature review",
        "背景",
        "相关工作",
        "文献综述",
    },
    "method": {
        "approach",
        "materials and methods",
        "method",
        "methodology",
        "methods",
        "our approach",
        "proposed approach",
        "proposed method",
        "方法",
        "方法论",
        "研究方法",
    },
    "experiment": {
        "analysis",
        "evaluation",
        "experiment",
        "experimental results",
        "experiments",
        "result",
        "results",
        "实验",
        "实验结果",
        "结果",
        "结果与分析",
    },
    "conclusion": {
        "conclusion",
        "conclusions",
        "conclusion and discussion",
        "discussion",
        "总结",
        "结论",
        "讨论",
        "展望",
        "未来展望",
        "总结与展望",
        "结论与展望",
    },
}
_PREFIX_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("introduction", re.compile(r"^(?:introduction|引言|绪论)\b")),
    (
        "related_work",
        re.compile(
            r"^(?:(?:additional\s+)?related works?|background|literature review|相关工作|背景|文献综述)\b"
        ),
    ),
    (
        "method",
        re.compile(
            r"^(?:materials and methods|methods?|methodology|approach|our approach|"
            r"proposed (?:method|methodology|approach)|方法论?|研究方法)\b"
        ),
    ),
    (
        "experiment",
        re.compile(
            r"^(?:experiments?|experimental|evaluation|results?|analysis|"
            r"quantitative results?|quantitive results?|generalization results?|"
            r"comparing\b|实验|结果|分析)\b"
        ),
    ),
    ("conclusion", re.compile(r"^(?:conclusions?|discussion|结论|讨论|总结)\b")),
)


def normalize_section_heading(value: str) -> str:
    """Normalize a heading without interpreting arbitrary title substrings."""

    unnumbered = _NUMBERING.sub("", value.strip().lower(), count=1)
    return _SEPARATORS.sub(" ", unnumbered).strip()


def section_family(value: str) -> str:
    """Classify only known complete headings and return unknown text unchanged."""

    normalized = normalize_section_heading(value)
    for family, headings in _FAMILIES.items():
        if normalized in headings:
            return family
    for family, pattern in _PREFIX_FAMILIES:
        if pattern.search(normalized):
            return family
    if re.search(r"\bexperiments?$", normalized):
        return "experiment"
    return normalized or "other"
