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
from typing import Any, ClassVar, Iterable, Mapping, Sequence


def paragraph_structure(text: str, section: str) -> dict[str, Any]:
    sentences = [value.strip() for value in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if value.strip()]
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


def similarity_risk(candidate: str, sources: Sequence[Mapping[str, str]], *, n: int = 5) -> dict[str, Any]:
    normalized = re.findall(r"[a-z0-9]+", candidate.lower())
    grams = {tuple(normalized[index:index+n]) for index in range(max(0, len(normalized)-n+1))}
    matches: list[dict[str, Any]] = []
    for source in sources:
        tokens = re.findall(r"[a-z0-9]+", str(source.get("text") or "").lower())
        source_grams = {tuple(tokens[index:index+n]) for index in range(max(0, len(tokens)-n+1))}
        overlap = len(grams & source_grams) / max(1, len(grams))
        exact = candidate.strip().lower() in str(source.get("text") or "").lower()
        if exact or overlap >= 0.2:
            matches.append({"source_id": source.get("source_id"), "exact": exact, "ngram_overlap": round(overlap, 4)})
    level = "high" if any(item["exact"] or item["ngram_overlap"] >= 0.5 for item in matches) else "medium" if matches else "low"
    return {"risk_type": "internal_source_similarity", "risk_level": level, "matches": matches, "legal_plagiarism_assessment": False}


def writing_profile(entries: Iterable[Mapping[str, Any]], *, profile_type: str, name: str) -> dict[str, Any]:
    rows = list(entries)
    lengths = [len(str(row.get("original_text") or "").split()) for row in rows]
    functions = Counter(str(row.get("writing_function") or "unknown") for row in rows)
    return {
        "schema_name": "writing_profile",
        "schema_version": "2.0",
        "profile_type": profile_type,
        "name": name,
        "sample_count": len(rows),
        "mean_paragraph_words": round(sum(lengths) / max(1, len(lengths)), 2),
        "writing_functions": dict(functions),
        "evidence_source": "user_selected_literature" if profile_type == "venue" else "user_supplied_drafts",
        "is_normative_rule": False,
    }


class WritingFeedbackStore:
    ALLOWED: ClassVar[set[str]] = {"useful", "not_useful", "too_generic", "too_similar", "wrong_function", "wrong_domain", "poor_style"}

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS feedback(feedback_id TEXT PRIMARY KEY,writing_id TEXT,label TEXT,created_at TEXT,context_json TEXT)")

    def submit(self, writing_id: str, label: str, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if label not in self.ALLOWED:
            raise ValueError("unsupported feedback label")
        value = {"feedback_id": str(uuid.uuid4()), "writing_id": writing_id, "label": label, "created_at": datetime.now(timezone.utc).isoformat(), "context": dict(context or {})}
        with sqlite3.connect(self.path) as connection:
            connection.execute("INSERT INTO feedback VALUES(?,?,?,?,?)", (value["feedback_id"], writing_id, label, value["created_at"], json.dumps(value["context"], ensure_ascii=False)))
        return value

    def adjustment(self, writing_id: str) -> float:
        weights = {"useful": 0.1, "not_useful": -0.1, "too_generic": -0.08, "too_similar": -0.15, "wrong_function": -0.15, "wrong_domain": -0.12, "poor_style": -0.1}
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute("SELECT label FROM feedback WHERE writing_id=?", (writing_id,)).fetchall()
        return max(-0.5, min(0.3, sum(weights[row[0]] for row in rows)))
