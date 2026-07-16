"""Executable grouped evaluation reports and explicit regression gates."""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Sequence

import yaml

from knowledgehub.core.atomic import atomic_write_json
from knowledgehub.core.hashing import sha256_file
from knowledgehub.evaluation.metrics import evaluate_code
from knowledgehub.writing_rag.analyzer import RuleWritingAnalyzer
from knowledgehub.writing_rag.v2 import paragraph_structure, similarity_risk

QueryCallback = Callable[[str, Mapping[str, Any], str, int], "QueryOutcome"]


@dataclass(frozen=True, slots=True)
class QueryOutcome:
    hits: tuple[Mapping[str, Any], ...]
    latency_seconds: float
    warnings: tuple[str, ...] = ()


class EvaluationRunner:
    """Run reviewable fixture groups without averaging unrelated task types."""

    CODE_REQUIRED: ClassVar[set[str]] = {
        "query",
        "library",
        "version",
        "environment",
        "expected_source",
        "expected_conclusion",
        "difficulty",
    }

    def __init__(
        self,
        eval_root: Path,
        *,
        query: QueryCallback | None = None,
    ) -> None:
        self.eval_root = eval_root
        self.query = query

    def run(
        self,
        *,
        domain: str = "all",
        mode: str = "offline",
        profile: str = "v2",
        top_k: int = 10,
    ) -> dict[str, Any]:
        if domain not in {"all", "code", "writing"}:
            raise ValueError("evaluation domain must be all, code, or writing")
        if mode not in {"offline", "live"}:
            raise ValueError("evaluation mode must be offline or live")
        if profile not in {"v1", "v2"}:
            raise ValueError("evaluation profile must be v1 or v2")
        if not 1 <= top_k <= 100:
            raise ValueError("evaluation top_k must be between 1 and 100")
        if mode == "live" and self.query is None:
            raise ValueError("live evaluation requires a query callback")
        groups: dict[str, Any] = {}
        if domain in {"all", "code"}:
            groups.update(self._code_groups(mode=mode, profile=profile, top_k=top_k))
        if domain in {"all", "writing"}:
            groups.update(self._writing_groups(mode=mode, profile=profile, top_k=top_k))
        return {
            "schema_name": "evaluation_report",
            "schema_version": "2.0",
            "run_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "git_commit": _git_commit(self.eval_root.parent),
            "git_dirty": _git_dirty(self.eval_root.parent),
            "fixture_manifest": {
                str(path.relative_to(self.eval_root)): sha256_file(path)
                for path in sorted(self.eval_root.glob("*/*.jsonl"))
            },
            "mode": mode,
            "profile": profile,
            "top_k": top_k,
            "groups": groups,
            "summary": {
                "group_count": len(groups),
                "sample_count": sum(int(value["sample_count"]) for value in groups.values()),
                "failed_groups": sorted(
                    name for name, value in groups.items() if value["status"] == "failed"
                ),
                "note": "Metrics are intentionally reported by task group; no overall average is computed.",
            },
        }

    def _code_groups(self, *, mode: str, profile: str, top_k: int) -> dict[str, Any]:
        groups: dict[str, Any] = {}
        for path in sorted((self.eval_root / "code").glob("*.jsonl")):
            name = f"code/{path.stem}"
            samples = self._load(path)
            fixture_failures = self._required_failures(samples, self.CODE_REQUIRED)
            if fixture_failures:
                groups[name] = self._group(samples, {}, fixture_failures)
                continue
            if mode == "offline":
                groups[name] = self._group(
                    samples,
                    {"fixture_schema_accuracy": 1.0},
                    warnings=["retrieval_metrics_require_live_mode"],
                )
                continue
            outcomes = [self._query("code", sample, profile, top_k) for sample in samples]
            metrics = evaluate_code(
                samples,
                [[dict(hit) for hit in outcome.hits] for outcome in outcomes],
                latencies=[outcome.latency_seconds for outcome in outcomes],
                k=top_k,
            )
            warnings = sorted({warning for outcome in outcomes for warning in outcome.warnings})
            groups[name] = self._group(samples, metrics, warnings=warnings)
        return groups

    def _writing_groups(
        self, *, mode: str, profile: str, top_k: int
    ) -> dict[str, Any]:
        groups: dict[str, Any] = {}
        for path in sorted((self.eval_root / "writing").glob("*.jsonl")):
            samples = self._load(path)
            name = f"writing/{path.stem}"
            if path.stem == "function_classification":
                groups[name] = self._writing_functions(samples)
            elif path.stem == "section_classification":
                groups[name] = self._writing_sections(samples)
            elif path.stem == "paragraph_structure":
                groups[name] = self._writing_paragraphs(samples)
            elif path.stem == "similarity_risk":
                groups[name] = self._writing_similarity(samples)
            elif path.stem == "venue_style":
                groups[name] = self._writing_profiles(samples)
            elif path.stem == "pattern_retrieval" and mode == "live":
                groups[name] = self._writing_retrieval(samples, profile, top_k)
            else:
                groups[name] = self._group(
                    samples,
                    {"fixture_schema_accuracy": 1.0},
                    warnings=["retrieval_metrics_require_live_mode"],
                )
        return groups

    def _writing_functions(self, samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        analyzer = RuleWritingAnalyzer()
        failures: list[str] = []
        hits = 0
        for index, sample in enumerate(samples, 1):
            required = {"text", "section", "expected_function"}
            missing = required - set(sample)
            if missing:
                failures.append(f"sample {index} missing {', '.join(sorted(missing))}")
                continue
            analysis = analyzer.analyze(
                str(sample["text"]), section=str(sample["section"]), domains=()
            )
            predicted = analysis.writing_function if analysis else None
            hits += predicted == sample["expected_function"]
            if predicted != sample["expected_function"]:
                failures.append(
                    f"sample {index} expected {sample['expected_function']}, got {predicted}"
                )
        return self._group(
            samples,
            {"writing_function_accuracy": _rate(hits, len(samples))},
            failures,
        )

    def _writing_sections(self, samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        hits = 0
        failures: list[str] = []
        for index, sample in enumerate(samples, 1):
            heading = str(sample.get("heading") or "")
            expected = str(sample.get("expected_section") or "")
            predicted = classify_section_heading(heading)
            hits += bool(expected) and predicted == expected
            if not expected or predicted != expected:
                failures.append(f"sample {index} expected {expected or 'missing'}, got {predicted}")
        return self._group(samples, {"section_accuracy": _rate(hits, len(samples))}, failures)

    def _writing_paragraphs(self, samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        hits = 0
        failures: list[str] = []
        for index, sample in enumerate(samples, 1):
            expected = list(sample.get("expected_moves") or [])
            actual = paragraph_structure(
                str(sample.get("text") or ""), str(sample.get("section") or "Introduction")
            )["moves"]
            hits += actual == expected
            if actual != expected:
                failures.append(f"sample {index} expected {expected}, got {actual}")
        return self._group(
            samples,
            {"paragraph_move_exact_match": _rate(hits, len(samples))},
            failures,
        )

    def _writing_similarity(self, samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        hits = 0
        failures: list[str] = []
        for index, sample in enumerate(samples, 1):
            expected = str(sample.get("expected_risk") or "")
            actual = similarity_risk(
                str(sample.get("candidate") or ""),
                [{"source_id": f"fixture-{index}", "text": str(sample.get("source") or "")}],
            )
            predicted = str(actual["risk_level"])
            detected = (expected == "low" and predicted == "low") or (
                expected != "low" and predicted != "low"
            )
            hits += detected
            if not detected:
                failures.append(f"sample {index} expected {expected}, got {predicted}")
        return self._group(
            samples,
            {"similarity_risk_detection_rate": _rate(hits, len(samples))},
            failures,
        )

    def _writing_profiles(self, samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        hits = 0
        failures: list[str] = []
        expected_sources = {
            "venue": "user_selected_literature",
            "personal": "user_supplied_drafts",
        }
        for index, sample in enumerate(samples, 1):
            profile_type = str(sample.get("profile_type") or "")
            valid = (
                sample.get("source") == expected_sources.get(profile_type)
                and sample.get("expected_normative") is False
                and not (
                    profile_type == "personal"
                    and sample.get("must_not_fallback_to") != "literature"
                )
            )
            hits += valid
            if not valid:
                failures.append(f"sample {index} violates profile source separation")
        return self._group(
            samples,
            {"profile_source_separation_accuracy": _rate(hits, len(samples))},
            failures,
        )

    def _writing_retrieval(
        self, samples: Sequence[Mapping[str, Any]], profile: str, top_k: int
    ) -> dict[str, Any]:
        outcomes = [self._query("writing", sample, profile, top_k) for sample in samples]
        function_hits = traceable = wrong_domains = domain_results = 0
        reciprocal = 0.0
        material_ids: list[str] = []
        for sample, outcome in zip(samples, outcomes, strict=True):
            expected = sample.get("expected_function")
            rank = None
            allowed_domains = set(sample.get("expected_domains") or [])
            for index, hit in enumerate(outcome.hits[:top_k], 1):
                if expected and hit.get("writing_function") == expected and rank is None:
                    rank = index
                if hit.get("source_paper_id") and (
                    hit.get("source_location") or hit.get("section")
                ):
                    traceable += 1
                domains = (
                    hit.get("inferred_research_domain")
                    or hit.get("research_domain")
                    or []
                )
                for domain in domains:
                    domain_results += 1
                    wrong_domains += bool(allowed_domains and domain not in allowed_domains)
                material_ids.append(str(hit.get("writing_id") or hit.get("document_id") or ""))
            if rank is not None:
                function_hits += 1
                reciprocal += 1 / rank
        duplicates = len(material_ids) - len(set(material_ids))
        result_count = sum(len(outcome.hits[:top_k]) for outcome in outcomes)
        metrics = {
            "function_recall_at_k": _rate(function_hits, len(samples)),
            "mrr": _rate(reciprocal, len(samples)),
            "source_traceability_rate": _rate(traceable, result_count),
            "duplicate_material_ratio": _rate(duplicates, len(material_ids)),
            "wrong_domain_recall_rate": _rate(wrong_domains, domain_results),
            "mean_latency_seconds": round(
                sum(outcome.latency_seconds for outcome in outcomes) / max(1, len(outcomes)), 6
            ),
        }
        warnings = sorted({warning for outcome in outcomes for warning in outcome.warnings})
        return self._group(samples, metrics, warnings=warnings)

    def _query(
        self, domain: str, sample: Mapping[str, Any], profile: str, top_k: int
    ) -> QueryOutcome:
        assert self.query is not None
        started = time.monotonic()
        outcome = self.query(domain, sample, profile, top_k)
        if outcome.latency_seconds < 0:
            return QueryOutcome(
                outcome.hits,
                time.monotonic() - started,
                outcome.warnings,
            )
        return outcome

    @staticmethod
    def _group(
        samples: Sequence[Mapping[str, Any]],
        metrics: Mapping[str, float],
        failures: Sequence[str] = (),
        *,
        warnings: Sequence[str] = (),
    ) -> dict[str, Any]:
        return {
            "sample_count": len(samples),
            "status": "failed" if failures else "passed",
            "metrics": dict(metrics),
            "failures": list(failures),
            "warnings": list(warnings),
        }

    @staticmethod
    def _required_failures(
        samples: Sequence[Mapping[str, Any]], required: set[str]
    ) -> list[str]:
        return [
            f"sample {index} missing {', '.join(sorted(required - set(sample)))}"
            for index, sample in enumerate(samples, 1)
            if required - set(sample)
        ]

    @staticmethod
    def _load(path: Path) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} must contain an object")
            values.append(value)
        if not values:
            raise ValueError(f"evaluation group is empty: {path}")
        return values


def classify_section_heading(heading: str) -> str:
    lowered = heading.lower()
    if "intro" in lowered:
        return "Introduction"
    if "related" in lowered or "background" in lowered:
        return "Related Work"
    if "method" in lowered or "approach" in lowered:
        return "Method"
    if any(value in lowered for value in ("experiment", "result", "analysis")):
        return "Experiment"
    if "conclu" in lowered or "discussion" in lowered:
        return "Conclusion"
    return "Unknown"


def compare_reports(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    thresholds: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare only configured group metrics and fail closed on missing candidates."""

    decisions: dict[str, Any] = {}
    failed: list[str] = []
    baseline_groups = baseline.get("groups") or {}
    candidate_groups = candidate.get("groups") or {}
    configured = thresholds.get("groups") or {}
    if not isinstance(configured, Mapping):
        raise ValueError("evaluation thresholds groups must be a mapping")
    for group, metrics in configured.items():
        if not isinstance(metrics, Mapping):
            raise ValueError(f"threshold group must be a mapping: {group}")
        baseline_metrics = (baseline_groups.get(group) or {}).get("metrics") or {}
        candidate_metrics = (candidate_groups.get(group) or {}).get("metrics") or {}
        metric_decisions: dict[str, Any] = {}
        for metric, rule in metrics.items():
            if not isinstance(rule, Mapping):
                raise ValueError(f"threshold rule must be a mapping: {group}.{metric}")
            baseline_value = baseline_metrics.get(metric)
            candidate_value = candidate_metrics.get(metric)
            reasons: list[str] = []
            if candidate_value is None:
                reasons.append("candidate_metric_missing")
            else:
                value = float(candidate_value)
                if rule.get("min") is not None and value < float(rule["min"]):
                    reasons.append("below_minimum")
                if rule.get("max") is not None and value > float(rule["max"]):
                    reasons.append("above_maximum")
                if baseline_value is not None and rule.get("max_drop") is not None:
                    if float(baseline_value) - value > float(rule["max_drop"]):
                        reasons.append("regression_exceeds_max_drop")
            key = f"{group}.{metric}"
            metric_decisions[metric] = {
                "passed": not reasons,
                "baseline": baseline_value,
                "candidate": candidate_value,
                "delta": (
                    round(float(candidate_value) - float(baseline_value), 6)
                    if baseline_value is not None and candidate_value is not None
                    else None
                ),
                "reasons": reasons,
            }
            if reasons:
                failed.append(key)
        decisions[str(group)] = metric_decisions
    return {
        "schema_name": "evaluation_comparison",
        "schema_version": "2.0",
        "baseline_run_id": baseline.get("run_id"),
        "candidate_run_id": candidate.get("run_id"),
        "baseline_profile": baseline.get("profile"),
        "candidate_profile": candidate.get("profile"),
        "passed": not failed,
        "failed_gates": sorted(failed),
        "groups": decisions,
        "note": "Gates are evaluated per task group; no overall average is used.",
    }


def load_thresholds(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict) or int(value.get("schema_version", 0)) != 1:
        raise ValueError("unsupported evaluation threshold configuration")
    return value


def write_report(path: Path, report: Mapping[str, Any]) -> Path:
    return atomic_write_json(path, dict(report))


def report_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return {"path": str(path), "sha256": sha256_file(path), "run_id": value.get("run_id")}


def live_query_callback(config: Any) -> QueryCallback:
    """Build a V1-direct/V2-routed query callback against existing collections."""

    def query(
        domain: str,
        sample: Mapping[str, Any],
        profile: str,
        top_k: int,
    ) -> QueryOutcome:
        from knowledgehub.hub.query import HubQueryRequest, HubQueryService
        from knowledgehub.retrieval.models import SearchRequest
        from knowledgehub.services.search_api import build_retrieval

        filters = {
            key: value
            for key, value in {
                "library": sample.get("library"),
                "version": sample.get("version"),
                "section": sample.get("section"),
                "writing_function": sample.get("expected_function"),
                "research_domain": sample.get("research_domain"),
            }.items()
            if value is not None
        }
        started = time.monotonic()
        if profile == "v2":
            response = HubQueryService(config).search(
                HubQueryRequest(
                    knowledge_base=domain,
                    query=str(sample["query"]),
                    filters=filters,
                    top_k=top_k,
                    return_mode="pattern_first",
                )
            )
        else:
            rag_config = config.rag_config(domain)
            service = build_retrieval(rag_config)
            request = SearchRequest(
                query=str(sample["query"]),
                mode="hybrid",
                limit=top_k,
                prefetch_limit=max(50, top_k),
                source=None,
                **filters,
            )
            try:
                response = service.search(request)
            finally:
                if hasattr(service, "endpoint_pool"):
                    service.endpoint_pool.close()
                reranker = getattr(service, "reranker", None)
                if reranker is not None:
                    reranker.close()
        hits = [dict(hit.payload) for hit in response.hits]
        if profile == "v2" and domain == "code" and sample.get("expected_symbol"):
            symbol_hit = _exact_symbol_evidence(config, sample)
            if symbol_hit is not None:
                hits.insert(0, symbol_hit)
        return QueryOutcome(
            tuple(hits[:top_k]),
            time.monotonic() - started,
            tuple(response.warnings),
        )

    return query


def _exact_symbol_evidence(
    config: Any, sample: Mapping[str, Any]
) -> dict[str, Any] | None:
    from knowledgehub.code_rag.symbols import SymbolIndex

    library = str(sample.get("library") or "")
    version = str(sample.get("version") or "")
    symbol = str(sample.get("expected_symbol") or "")
    if not library or not version or not symbol:
        return None
    path = config.code.data_root / "state" / "symbols.sqlite3"
    if not path.is_file():
        return None
    value = SymbolIndex(path, read_only=True).inspect(library, version, symbol)
    if value is None:
        return None
    marker_path = (
        config.code.data_root
        / "sources"
        / "repositories"
        / library
        / version
        / "current.json"
    )
    marker = (
        json.loads(marker_path.read_text(encoding="utf-8"))
        if marker_path.is_file()
        else {}
    )
    repository = str(marker.get("repository") or "")
    commit = str(marker.get("commit") or "")
    source_path = str(value.get("path") or "")
    source_url = (
        f"https://github.com/{repository}/blob/{commit}/{source_path}"
        if repository and commit and source_path
        else ""
    )
    return dict(value) | {
        "source_type": "source_code",
        "symbol": symbol,
        "repository": repository,
        "commit": commit,
        "source_url": source_url,
        "evidence_role": "exact_symbol_evidence",
        "inference": False,
    }


def _rate(numerator: int | float, denominator: int) -> float:
    return round(float(numerator) / denominator, 6) if denominator else 0.0


def _git_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _git_dirty(root: Path) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(result.stdout.strip())
