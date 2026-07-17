"""Read-only project-level Skill-equivalent services."""

from __future__ import annotations

from typing import Any

from knowledgehub.project.knowledge import ProjectQueryService
from knowledgehub.project.registry import ProjectRegistry

SKILLS = {
    "code-debugging",
    "research-result-analysis",
    "research-decision-review",
    "writing-academic",
}


class ProjectSkillService:
    def __init__(self, registry: ProjectRegistry, query_service: ProjectQueryService) -> None:
        self.registry = registry
        self.query_service = query_service

    def run(
        self,
        skill: str,
        workspace_id: str,
        *,
        experiment_ids: tuple[str, ...] = (),
        section: str = "Results",
        writing_function: str = "experimental_comparison",
    ) -> dict[str, Any]:
        if skill not in SKILLS:
            raise ValueError(f"unsupported project skill: {skill}")
        if skill == "code-debugging":
            return self._debug(workspace_id, experiment_ids)
        if skill == "research-result-analysis":
            return self._analysis(workspace_id, experiment_ids)
        if skill == "research-decision-review":
            return self._decision(workspace_id)
        return self._writing(workspace_id, section, writing_function)

    def _debug(self, workspace_id: str, experiment_ids: tuple[str, ...]) -> dict[str, Any]:
        experiments = self._experiments(workspace_id, experiment_ids)
        failed = [item for item in experiments if item["status"] == "failed"]
        failures = self.registry.list_records(workspace_id, "failure")
        evidence = self.query_service.query(
            workspace_id,
            "code_debugging",
            "NaN non-finite loss controlled failure tensor validation",
            experiment_ids=tuple(item["experiment_id"] for item in failed),
        )
        confirmed = [item for item in failures if item["root_cause_status"] == "confirmed"]
        return {
            "skill": "code-debugging",
            "candidate_root_causes": [item["root_cause"] for item in confirmed],
            "evidence": evidence,
            "verification_steps": [
                "Re-run the failing immutable experiment configuration.",
                "Confirm the first non-finite loss occurs after the explicit injection point.",
                "Run the linked retry with finite inputs and compare status and metrics.",
            ],
            "suggested_changes": [item["working_solution"] for item in confirmed],
            "confidence": "high" if confirmed else "low",
            "warnings": ["fixture_only", "no_source_was_modified"],
        }

    def _analysis(self, workspace_id: str, experiment_ids: tuple[str, ...]) -> dict[str, Any]:
        experiments = self._experiments(workspace_id, experiment_ids)
        completed = [item for item in experiments if item["status"] == "completed"]
        comparable = len({item["environment_id"] for item in completed}) <= 1 and len(
            {item["git_commit"] for item in completed}
        ) <= 1
        observations = [
            {
                "experiment_id": item["experiment_id"],
                "config_hash": item["config_hash"],
                "seed": item["seed"],
                "environment_id": item["environment_id"],
                "git_commit": item["git_commit"],
                "metrics": item["metrics"],
            }
            for item in completed
        ]
        return {
            "skill": "research-result-analysis",
            "aligned_experiments": observations,
            "comparable_environment_and_commit": comparable,
            "supported_conclusions": [
                "Only comparisons directly represented by fixture Experiment metrics are supportable."
            ],
            "unsupported_conclusions": [
                "Generalization to real image datasets or other seeds is unsupported."
            ],
            "confounders": ["single synthetic generator", "single seed", "CPU timing noise"],
            "next_experiments": ["repeat both fusion variants with additional fixed seeds"],
            "warnings": ["fixture_results_are_not_academic_findings"],
        }

    def _decision(self, workspace_id: str) -> dict[str, Any]:
        decisions = self.registry.list_records(workspace_id, "decision")
        claims = self.registry.list_records(workspace_id, "claim")
        return {
            "skill": "research-decision-review",
            "decisions": decisions,
            "still_supported": all(item["status"] == "accepted" for item in decisions),
            "missing_evidence": ["multiple seeds", "real dataset validation"],
            "counter_evidence": [
                item for item in claims if item["status"] in {"contradicted", "invalidated"}
            ],
            "current_impact": "The decision applies only to the fixture default configuration.",
            "reassessment_needed": True,
        }

    def _writing(self, workspace_id: str, section: str, writing_function: str) -> dict[str, Any]:
        claims = self.registry.list_records(workspace_id, "claim")
        evidence = self.query_service.query(
            workspace_id,
            "academic_writing",
            f"{writing_function} {section} limitations experimental comparison",
        )
        return {
            "skill": "writing-academic",
            "section": section,
            "writing_function": writing_function,
            "claims": claims,
            "writing_plan": [
                "Label the study as a controlled KnowledgeHub fixture.",
                "State only values resolved from linked Experiment metric pointers.",
                "Compare the two fusion configurations before discussing complexity.",
                "Close with the synthetic-data and single-seed limitations.",
            ],
            "patterns": evidence["knowledge_evidence"].get("writing", {}),
            "sources": evidence["sources"],
            "warnings": [
                "fixture_results_must_not_be_presented_as_real_research",
                "pattern_adaptation_only_no_source_sentence_copying",
            ],
        }

    def _experiments(
        self, workspace_id: str, experiment_ids: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        values = self.registry.list_records(workspace_id, "experiment")
        if experiment_ids:
            selected = set(experiment_ids)
            values = [item for item in values if item["experiment_id"] in selected]
        return values

