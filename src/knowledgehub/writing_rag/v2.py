"""Paragraph rhetoric, internal similarity risk, profiles and feedback."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from statistics import mean, median
from typing import Any, ClassVar, Iterable, Literal, Mapping, Sequence

from knowledgehub.core.atomic import atomic_write_json
from knowledgehub.core.hashing import sha256_file, sha256_json

_FIRST_PERSON = re.compile(r"\b(?:we|our|ours|us)\b", re.I)
_PASSIVE = re.compile(r"\b(?:is|are|was|were|be|been|being)\s+\w+(?:ed|en)\b", re.I)
_CAUTIOUS = re.compile(r"\b(?:may|might|could|appears?|suggests?|likely|potentially)\b", re.I)
_STRONG = re.compile(
    r"\b(?:clearly|significantly|substantially|demonstrates?|proves?|always)\b", re.I
)
_CRITICAL = re.compile(
    r"\b(?:fail(?:s|ed)?|shortcoming|drawback|severe|fundamental limitation)\b", re.I
)
_MATH = re.compile(r"(?:\$[^$]+\$|\\\(|\\\[|\\begin\{|\b[A-Za-z]\s*=\s*[^,.;]+)")
_TRANSITION = re.compile(
    r"\b(?:however|therefore|moreover|furthermore|consequently|nevertheless|"
    r"in contrast|for example|specifically|thus|although|despite)\b",
    re.I,
)
_LEXICAL_TOKEN = re.compile(
    r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*|[\u3400-\u4dbf\u4e00-\u9fff]"
)
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_CJK_FIRST_PERSON = re.compile(r"(?:我们|本人|我)")
_CJK_PASSIVE = re.compile(
    r"(?:被|受到|由[^\uff0c\u3002\uff01\uff1f]{0,20}(?:完成|执行|实现|进行))"
)
_CJK_CAUTIOUS = re.compile(r"(?:可能|或许|似乎|表明|建议|需要进一步|尚需|不确定)")
_CJK_STRONG = re.compile(r"(?:显然|明显|完全|证明|始终|必然|显著)")
_CJK_CRITICAL = re.compile(r"(?:失败|缺点|不足|严重|根本性限制|局限)")
_CJK_TRANSITION = re.compile(r"(?:然而|因此|此外|进一步|相反|例如|具体而言|尽管|虽然|但是)")
_STOP_WORDS = {
    "about",
    "after",
    "also",
    "because",
    "been",
    "before",
    "between",
    "from",
    "have",
    "into",
    "method",
    "paper",
    "results",
    "that",
    "their",
    "these",
    "this",
    "using",
    "were",
    "which",
    "with",
}

WritingTask = Literal[
    "retrieve_patterns",
    "generate_outline",
    "draft_paragraph",
    "rewrite_paragraph",
    "strengthen_argument",
    "improve_transition",
    "compare_expressions",
    "audit_repetition",
    "audit_source_similarity",
    "respond_to_reviewer",
]


def _lexical_tokens(text: str) -> list[str]:
    """Return deterministic Latin words and individual CJK lexical units."""
    return _LEXICAL_TOKEN.findall(text)


def _sentences(text: str) -> list[str]:
    return [
        value.strip()
        for value in re.split(r"(?<=[.!?\u3002\uff01\uff1f])\s*", text)
        if value.strip()
    ]


def _profile_terms(text: str) -> list[str]:
    terms = [
        token.lower()
        for token in re.findall(r"\b[A-Za-z][A-Za-z-]{3,}\b", text)
        if token.lower() not in _STOP_WORDS
    ]
    for run in _CJK_RUN.findall(text):
        terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def paragraph_structure(text: str, section: str) -> dict[str, Any]:
    sentences = _sentences(" ".join(text.split()))
    roles: list[str] = []
    for sentence in sentences:
        lowered = sentence.lower()
        if re.search(r"however|although|despite|remain|limitation|lack", lowered):
            role = "identify_gap"
        elif re.search(r"we (propose|present|introduce)|our method", lowered):
            role = "introduce_solution"
        elif re.search(r"outperform|compared|higher|lower", lowered):
            role = "compare_baseline"
        elif re.search(r"because|suggest|indicate|therefore", lowered):
            role = "derive_implication"
        else:
            role = "establish_context" if not roles else "summarize_progress"
        roles.append(role)
    return {
        "paragraph_pattern": " -> ".join(roles),
        "moves": roles,
        "transition_relations": [f"{left}_to_{right}" for left, right in pairwise(roles)],
        "sentence_roles": [{"index": index, "role": role} for index, role in enumerate(roles)],
        "usage_context": section,
    }


def paragraph_features(text: str) -> dict[str, Any]:
    """Return deterministic style facets suitable for filtering and profiles."""

    normalized = " ".join(text.split())
    words = _lexical_tokens(normalized)
    sentences = _sentences(normalized)
    cautious = len(_CAUTIOUS.findall(normalized)) + len(_CJK_CAUTIOUS.findall(normalized))
    strong = len(_STRONG.findall(normalized)) + len(_CJK_STRONG.findall(normalized))
    critical = len(_CRITICAL.findall(normalized)) + len(_CJK_CRITICAL.findall(normalized))
    if strong > cautious:
        strength = "strong"
    elif cautious > strong:
        strength = "cautious"
    else:
        strength = "moderate"
    if critical:
        tone = "critical"
    elif cautious:
        tone = "cautious"
    elif strong:
        tone = "assertive"
    else:
        tone = "neutral"
    return {
        "paragraph_word_count": len(words),
        "expression_strength": strength,
        "tone": tone,
        "contains_math": bool(_MATH.search(normalized)),
        "first_person": bool(
            _FIRST_PERSON.search(normalized) or _CJK_FIRST_PERSON.search(normalized)
        ),
        "passive_voice": bool(_PASSIVE.search(normalized) or _CJK_PASSIVE.search(normalized)),
        "transition_markers": [
            *[value.lower() for value in _TRANSITION.findall(normalized)],
            *_CJK_TRANSITION.findall(normalized),
        ],
        "mean_sentence_words": round(len(words) / max(1, len(sentences)), 2),
        "bullet_or_numbered": bool(re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)", text.strip())),
        "contribution_expression": bool(
            re.search(
                r"\b(?:we (?:propose|present|introduce)|our contributions?)\b", normalized, re.I
            )
            or re.search(r"(?:我们|本文)(?:提出|介绍|引入)", normalized)
        ),
        "figure_table_reference": bool(
            re.search(r"\b(?:fig(?:ure)?|table)\s*\d+", normalized, re.I)
            or re.search(r"(?:图|表)\s*[一二三四五六七八九十\d]+", normalized)
        ),
        "analysis_expression": bool(
            re.search(
                r"\b(?:results? (?:show|indicate|suggest)|because|we observe)\b", normalized, re.I
            )
            or re.search(r"(?:结果|实验)(?:表明|显示)|由于|我们观察到", normalized)
        ),
        "abbreviations": re.findall(r"\b[A-Z][A-Z0-9-]{1,9}\b", normalized),
    }


def similarity_risk(
    candidate: str,
    sources: Sequence[Mapping[str, str]],
    *,
    n: int = 5,
    semantic_scorer: Any | None = None,
) -> dict[str, Any]:
    """Audit internal source reuse without making a legal plagiarism claim."""

    if not candidate.strip():
        raise ValueError("similarity candidate cannot be empty")
    if n < 2:
        raise ValueError("similarity n-gram size must be at least 2")
    normalized = re.findall(r"[a-z0-9]+", candidate.lower())
    grams = {
        tuple(normalized[index : index + n]) for index in range(max(0, len(normalized) - n + 1))
    }
    matches: list[dict[str, Any]] = []
    for source in sources:
        tokens = re.findall(r"[a-z0-9]+", str(source.get("text") or "").lower())
        source_grams = {
            tuple(tokens[index : index + n]) for index in range(max(0, len(tokens) - n + 1))
        }
        overlap = len(grams & source_grams) / max(1, len(grams))
        exact = candidate.strip().lower() in str(source.get("text") or "").lower()
        longest = _longest_common_run(normalized, tokens)
        semantic = (
            float(semantic_scorer(candidate, str(source.get("text") or "")))
            if semantic_scorer is not None
            else None
        )
        structure = _structure_overlap(candidate, str(source.get("text") or ""))
        if exact or longest >= 8 or overlap >= 0.2 or (semantic is not None and semantic >= 0.8):
            matches.append(
                {
                    "source_id": source.get("source_id"),
                    "exact": exact,
                    "exact_string": exact,
                    "longest_shared_words": longest,
                    "ngram_overlap": round(overlap, 4),
                    "semantic_similarity": round(semantic, 4) if semantic is not None else None,
                    "structural_similarity": round(structure, 4),
                }
            )
    level = (
        "high"
        if any(
            item["exact_string"]
            or item["longest_shared_words"] >= 12
            or item["ngram_overlap"] >= 0.5
            or (item["semantic_similarity"] or 0.0) >= 0.9
            for item in matches
        )
        else "medium"
        if matches
        else "low"
    )
    return {
        "risk_type": "internal_source_similarity",
        "risk_level": level,
        "layers": {
            "exact_string": "evaluated",
            "long_fragment": "evaluated",
            "ngram": "evaluated",
            "semantic": "evaluated" if semantic_scorer is not None else "not_evaluated",
            "continuous_structure": "evaluated",
        },
        "matches": matches,
        "legal_plagiarism_assessment": False,
        "warning": "This is an internal source-similarity signal, not a legal plagiarism assessment.",
    }


def _longest_common_run(left: Sequence[str], right: Sequence[str]) -> int:
    previous = [0] * (len(right) + 1)
    longest = 0
    for left_token in left:
        current = [0]
        for index, right_token in enumerate(right, 1):
            value = previous[index - 1] + 1 if left_token == right_token else 0
            current.append(value)
            longest = max(longest, value)
        previous = current
    return longest


def _structure_overlap(left: str, right: str) -> float:
    left_markers = set(_TRANSITION.findall(left.lower()))
    right_markers = set(_TRANSITION.findall(right.lower()))
    if not left_markers:
        return 0.0
    return len(left_markers & right_markers) / len(left_markers)


def writing_profile(
    entries: Iterable[Mapping[str, Any]], *, profile_type: str, name: str
) -> dict[str, Any]:
    if profile_type not in {"venue", "personal"}:
        raise ValueError("profile_type must be venue or personal")
    rows = list(entries)
    if not rows:
        raise ValueError("a writing profile requires at least one selected sample")
    functions = Counter(str(row.get("writing_function") or "unknown") for row in rows)
    sections = Counter(str(row.get("source_section") or "unknown") for row in rows)
    features = [paragraph_features(str(row.get("original_text") or "")) for row in rows]
    lengths = [int(item["paragraph_word_count"]) for item in features]
    transitions = Counter(value for item in features for value in item["transition_markers"])
    terms = Counter(
        token
        for row in rows
        for token in _profile_terms(str(row.get("original_text") or ""))
    )
    abbreviations = Counter(value for item in features for value in item["abbreviations"])
    source_ids = sorted(
        {
            str(row.get("source_paper_id") or row.get("source_id") or "")
            for row in rows
            if row.get("source_paper_id") or row.get("source_id")
        }
    )
    profile_identity = {
        "profile_type": profile_type,
        "name": name,
        "source_ids": source_ids,
        "content_hashes": sorted(str(row.get("content_hash") or "") for row in rows),
        "processor": "writing-profile-v2.5",
    }
    return {
        "schema_name": "writing_profile",
        "schema_version": "2.4",
        "profile_id": f"profile:{sha256_json(profile_identity)}",
        "profile_type": profile_type,
        "name": name,
        "sample_count": len(rows),
        "source_count": len(source_ids),
        "source_ids": source_ids,
        "mean_paragraph_words": round(sum(lengths) / max(1, len(lengths)), 2),
        "median_paragraph_words": round(median(lengths), 2),
        "writing_functions": dict(functions),
        "sections": dict(sections),
        "expression_strength": dict(Counter(item["expression_strength"] for item in features)),
        "tones": dict(Counter(item["tone"] for item in features)),
        "first_person_rate": round(mean(float(item["first_person"]) for item in features), 4),
        "passive_voice_rate": round(mean(float(item["passive_voice"]) for item in features), 4),
        "math_paragraph_rate": round(mean(float(item["contains_math"]) for item in features), 4),
        "mean_sentence_words": round(mean(item["mean_sentence_words"] for item in features), 2),
        "bullet_or_numbered_rate": round(
            mean(float(item["bullet_or_numbered"]) for item in features), 4
        ),
        "contribution_expression_rate": round(
            mean(float(item["contribution_expression"]) for item in features), 4
        ),
        "figure_table_reference_rate": round(
            mean(float(item["figure_table_reference"]) for item in features), 4
        ),
        "analysis_expression_rate": round(
            mean(float(item["analysis_expression"]) for item in features), 4
        ),
        "common_terms": dict(terms.most_common(30)),
        "abbreviations": dict(abbreviations.most_common(30)),
        "common_transitions": dict(transitions.most_common(20)),
        "evidence_source": "user_selected_literature"
        if profile_type == "venue"
        else "user_supplied_drafts",
        "is_normative_rule": False,
        "processor_version": "writing-profile-v2.5",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _section_family(section: str) -> str:
    lowered = section.lower()
    if "intro" in lowered:
        return "introduction"
    if "method" in lowered or "approach" in lowered:
        return "method"
    if any(value in lowered for value in ("experiment", "result", "analysis")):
        return "experiment"
    if "related" in lowered:
        return "related"
    if "conclu" in lowered:
        return "conclusion"
    return "other"


class WritingProfileStore:
    """Build and persist provenance-separated Venue and Personal profiles."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def build_venue(
        self,
        entries: Iterable[Mapping[str, Any]],
        *,
        name: str,
        paper_ids: Sequence[str],
        sections: Sequence[str] = (),
    ) -> dict[str, Any]:
        if not paper_ids:
            raise ValueError("Venue profiles require explicit user-selected paper IDs")
        selected_ids = set(paper_ids)
        selected = [row for row in entries if str(row.get("source_paper_id")) in selected_ids]
        missing = sorted(selected_ids - {str(row.get("source_paper_id")) for row in selected})
        if missing:
            raise ValueError(f"Venue profile papers not found: {', '.join(missing)}")
        if sections:
            selected_sections = {value.lower() for value in sections}
            selected = [
                row
                for row in selected
                if _section_family(str(row.get("source_section") or ""))
                in selected_sections
            ]
            if not selected:
                raise ValueError("No Venue profile samples matched the selected sections")
        profile = writing_profile(selected, profile_type="venue", name=name)
        profile["selection"] = {
            "paper_ids": sorted(selected_ids),
            "section_families": sorted(value.lower() for value in sections),
        }
        return self.save(profile)

    def build_personal(self, *, name: str, drafts: Sequence[Path]) -> dict[str, Any]:
        if not drafts:
            raise ValueError("Personal profiles require explicit user-supplied draft files")
        rows: list[dict[str, Any]] = []
        for path in drafts:
            selected = path.expanduser().resolve(strict=True)
            if not selected.is_file():
                raise ValueError(f"Personal profile source is not a file: {selected}")
            digest = sha256_file(selected)
            paragraphs = re.split(r"\n\s*\n", selected.read_text(encoding="utf-8"))
            for index, paragraph in enumerate(paragraphs, 1):
                text = " ".join(paragraph.split())
                if len(_lexical_tokens(text)) < 20:
                    continue
                rows.append(
                    {
                        "source_paper_id": f"draft:{digest}",
                        "source_location": {"paragraph": index},
                        "source_section": "user_draft",
                        "writing_function": "unclassified",
                        "original_text": text,
                        "content_hash": sha256_json({"text": text}),
                    }
                )
        if not rows:
            raise ValueError("No paragraph of at least 20 words was found in the supplied drafts")
        profile = writing_profile(rows, profile_type="personal", name=name)
        profile["selection"] = {
            "draft_content_hashes": sorted({
                str(row["source_paper_id"]).removeprefix("draft:") for row in rows
            })
        }
        return self.save(profile)

    def save(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        profile_type = str(profile.get("profile_type") or "")
        if profile_type not in {"venue", "personal"}:
            raise ValueError("profile_type must be venue or personal")
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(profile.get("name") or "")).strip("-")
        if not name:
            name = f"profile-{str(profile['profile_id']).split(':')[-1][:12]}"
        path = self.root / profile_type / f"{name}.json"
        atomic_write_json(path, dict(profile))
        return dict(profile) | {"profile_path": str(path)}

    def list(self, profile_type: str | None = None) -> list[dict[str, Any]]:
        kinds = (profile_type,) if profile_type else ("venue", "personal")
        results: list[dict[str, Any]] = []
        for kind in kinds:
            if kind not in {"venue", "personal"}:
                raise ValueError("profile_type must be venue or personal")
            for path in sorted((self.root / kind).glob("*.json")):
                value = json.loads(path.read_text(encoding="utf-8"))
                results.append(
                    {
                        "profile_id": value["profile_id"],
                        "profile_type": kind,
                        "name": value["name"],
                        "sample_count": value["sample_count"],
                        "profile_path": str(path),
                    }
                )
        return results


class WritingTaskPlanner:
    """Describe evidence retrieval for Writing Skills; it never authors prose."""

    TASKS: ClassVar[set[str]] = {
        "retrieve_patterns",
        "generate_outline",
        "draft_paragraph",
        "rewrite_paragraph",
        "strengthen_argument",
        "improve_transition",
        "compare_expressions",
        "audit_repetition",
        "audit_source_similarity",
        "respond_to_reviewer",
    }
    TEXT_REQUIRED: ClassVar[set[str]] = {
        "rewrite_paragraph",
        "strengthen_argument",
        "improve_transition",
        "audit_repetition",
        "audit_source_similarity",
    }

    def plan(
        self,
        task: WritingTask | str,
        *,
        objective: str,
        text: str | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if task not in self.TASKS:
            raise ValueError(f"unsupported writing task: {task}")
        if not objective.strip():
            raise ValueError("writing task objective cannot be empty")
        if task in self.TEXT_REQUIRED and not (text or "").strip():
            raise ValueError(f"{task} requires input text")
        return {
            "schema_name": "writing_task_plan",
            "schema_version": "2.4",
            "task": task,
            "objective": objective,
            "input_text_supplied": bool((text or "").strip()),
            "retrieval": {
                "knowledge_base": "writing",
                "query": objective,
                "filters": dict(filters or {}),
                "return_mode": "paragraph_structure",
                "evidence_fields": [
                    "writing_id",
                    "abstract_pattern",
                    "paragraph_pattern",
                    "moves",
                    "usage_notes",
                    "source_paper_id",
                    "source_location",
                ],
            },
            "style_layers": ["general_academic_guidance", "venue_profile", "personal_profile"],
            "generation_boundary": "Writing RAG supplies evidence and patterns; the caller authors final prose.",
            "source_similarity_check_required": task
            in {
                "draft_paragraph",
                "rewrite_paragraph",
                "audit_source_similarity",
                "respond_to_reviewer",
            },
        }


class WritingFeedbackStore:
    ALLOWED: ClassVar[set[str]] = {
        "useful",
        "not_useful",
        "too_generic",
        "too_similar",
        "wrong_function",
        "wrong_domain",
        "poor_style",
    }

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        if read_only and not path.is_file():
            raise FileNotFoundError(path)
        if not read_only:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.read_only = read_only
        if not read_only:
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS feedback(feedback_id TEXT PRIMARY KEY,writing_id TEXT,label TEXT,created_at TEXT,context_json TEXT)"
                )

    def submit(
        self, writing_id: str, label: str, context: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        if self.read_only:
            raise RuntimeError("feedback store is read-only")
        if label not in self.ALLOWED:
            raise ValueError("unsupported feedback label")
        value = {
            "feedback_id": str(uuid.uuid4()),
            "writing_id": writing_id,
            "label": label,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "context": dict(context or {}),
        }
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO feedback VALUES(?,?,?,?,?)",
                (
                    value["feedback_id"],
                    writing_id,
                    label,
                    value["created_at"],
                    json.dumps(value["context"], ensure_ascii=False),
                ),
            )
        return value

    def adjustment(self, writing_id: str) -> float:
        return self.adjustments([writing_id]).get(writing_id, 0.0)

    def adjustments(self, writing_ids: Sequence[str]) -> dict[str, float]:
        weights = {
            "useful": 0.1,
            "not_useful": -0.1,
            "too_generic": -0.08,
            "too_similar": -0.15,
            "wrong_function": -0.15,
            "wrong_domain": -0.12,
            "poor_style": -0.1,
        }
        identifiers = sorted(set(writing_ids))
        if not identifiers:
            return {}
        placeholders = ",".join("?" for _ in identifiers)
        connection_path = f"file:{self.path}?mode=ro" if self.read_only else str(self.path)
        with sqlite3.connect(connection_path, uri=self.read_only) as connection:
            rows = connection.execute(
                f"SELECT writing_id,label FROM feedback WHERE writing_id IN ({placeholders})",
                identifiers,
            ).fetchall()
        totals: dict[str, float] = {writing_id: 0.0 for writing_id in identifiers}
        for writing_id, label in rows:
            totals[str(writing_id)] += weights[str(label)]
        return {writing_id: max(-0.5, min(0.3, totals[writing_id])) for writing_id in identifiers}
