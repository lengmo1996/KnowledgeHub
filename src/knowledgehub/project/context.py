"""Task-aware, budgeted project context construction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from knowledgehub.core.hashing import canonical_json_dumps
from knowledgehub.project.models import TASK_TYPES, ContextBudget
from knowledgehub.project.registry import ProjectRegistry, utc_now


class ProjectContextBuilder:
    def __init__(self, registry: ProjectRegistry) -> None:
        self.registry = registry

    def build(
        self,
        workspace_id: str,
        task: str,
        *,
        budget: ContextBudget | None = None,
        target_experiment_id: str | None = None,
    ) -> dict[str, Any]:
        if task not in TASK_TYPES:
            raise ValueError(f"unsupported context task: {task}")
        selected_budget = budget or ContextBudget()
        workspace = self.registry.get(workspace_id)
        experiments = self._experiments(workspace_id, selected_budget)
        if target_experiment_id:
            experiments = [
                item for item in experiments if item["experiment_id"] == target_experiment_id
            ]
        failures = self.registry.list_records(workspace_id, "failure")
        decisions = self.registry.list_records(workspace_id, "decision")
        claims = self.registry.list_records(workspace_id, "claim")
        environments = self.registry.list_environments(workspace_id)
        context: dict[str, Any] = {
            "task": task,
            "workspace": workspace,
            "repositories": workspace["repositories"],
            "environments": environments,
            "knowledge_scopes": workspace["knowledge"],
            "recent_experiments": [],
            "active_decisions": [],
            "known_failures": [],
            "claims": [],
            "warnings": [],
            "generated_at": utc_now(),
            "budget": {
                "max_records": selected_budget.max_records,
                "max_characters": selected_budget.max_characters,
                "include_raw_logs": selected_budget.include_raw_logs,
                "include_paper_fragments": selected_budget.include_paper_fragments,
            },
        }
        if task == "project_overview":
            context["recent_experiments"] = experiments
            context["active_decisions"] = [item for item in decisions if item["status"] == "accepted"]
            context["known_failures"] = [item for item in failures if item["status"] != "resolved"]
        elif task == "code_debugging":
            context["recent_experiments"] = experiments
            experiment_ids = {item["experiment_id"] for item in experiments}
            context["known_failures"] = [
                item for item in failures if item["experiment_id"] in experiment_ids
            ]
            context["knowledge_scopes"] = {"code": workspace["knowledge"]["code"]}
            context["claims"] = []
        elif task == "experiment_analysis":
            context["recent_experiments"] = experiments
            experiment_ids = {item["experiment_id"] for item in experiments}
            context["known_failures"] = [
                item for item in failures if item["experiment_id"] in experiment_ids
            ]
            context["active_decisions"] = decisions
        elif task == "decision_review":
            context["recent_experiments"] = experiments
            context["active_decisions"] = decisions
            context["claims"] = claims
        else:
            context["repositories"] = [
                {"repository_id": item["repository_id"], "role": item.get("role")}
                for item in workspace["repositories"]
            ]
            context["recent_experiments"] = [self._writing_experiment(item) for item in experiments]
            context["active_decisions"] = decisions
            context["claims"] = claims
            context["known_failures"] = [self._failure_summary(item) for item in failures]
        self._apply_record_budget(context, selected_budget.max_records)
        self._apply_character_budget(context, selected_budget.max_characters)
        return context

    def _experiments(self, workspace_id: str, budget: ContextBudget) -> list[dict[str, Any]]:
        values = self.registry.list_records(workspace_id, "experiment")
        if budget.experiment_ids:
            allowed = set(budget.experiment_ids)
            values = [item for item in values if item["experiment_id"] in allowed]
        if budget.days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=budget.days)
            values = [
                item
                for item in values
                if datetime.fromisoformat(item["started_at"]) >= cutoff
            ]
        return sorted(values, key=lambda item: item["started_at"], reverse=True)

    @staticmethod
    def _writing_experiment(value: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value.get(key)
            for key in (
                "experiment_id",
                "objective",
                "hypothesis",
                "config_hash",
                "seed",
                "status",
                "metrics",
                "conclusion",
                "failure_id",
            )
        }

    @staticmethod
    def _failure_summary(value: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value.get(key)
            for key in ("failure_id", "experiment_id", "symptom", "status", "working_solution")
        }

    @staticmethod
    def _apply_record_budget(context: dict[str, Any], maximum: int) -> None:
        remaining = maximum
        for key in ("recent_experiments", "known_failures", "active_decisions", "claims"):
            values = context[key]
            context[key] = values[:remaining]
            remaining -= len(context[key])
            if len(values) > len(context[key]):
                context["warnings"].append(f"{key}_truncated_by_record_budget")

    @staticmethod
    def _apply_character_budget(context: dict[str, Any], maximum: int) -> None:
        removable = ("claims", "known_failures", "active_decisions", "recent_experiments")
        while len(canonical_json_dumps(context)) > maximum:
            for key in removable:
                if context[key]:
                    context[key].pop()
                    warning = f"{key}_truncated_by_character_budget"
                    if warning not in context["warnings"]:
                        context["warnings"].append(warning)
                    break
            else:
                context["warnings"].append("base_context_exceeds_character_budget")
                break
