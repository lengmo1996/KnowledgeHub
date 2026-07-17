"""Versioned V3 project records with strict fixture safety validation."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, ClassVar, Mapping

SCHEMA_VERSION = "3.0"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")
WORKSPACE_STATUSES = {"active", "archived"}
EXPERIMENT_STATUSES = {"planned", "running", "completed", "failed", "cancelled", "invalid"}
ROOT_CAUSE_STATUSES = {"unknown", "suspected", "probable", "confirmed"}
CLAIM_STATUSES = {
    "draft",
    "unsupported",
    "partially_supported",
    "supported",
    "contradicted",
    "invalidated",
}
TASK_TYPES = {
    "project_overview",
    "code_debugging",
    "experiment_analysis",
    "decision_review",
    "academic_writing",
}


def _require_id(value: str, field_name: str) -> None:
    if not ID_PATTERN.fullmatch(value):
        raise ValueError(f"invalid {field_name}: {value!r}")


def _require_schema(value: str) -> None:
    if value != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: {value}")


def _relative_path(value: str, field_name: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be a contained relative path")


@dataclass(frozen=True, slots=True, kw_only=True)
class Workspace:
    workspace_id: str
    name: str
    description: str
    research: Mapping[str, Any]
    repositories: tuple[Mapping[str, Any], ...]
    environments: Mapping[str, str]
    knowledge: Mapping[str, Any]
    created_at: str
    updated_at: str
    workspace_type: str = "fixture"
    data_scope: str = "test"
    status: str = "active"
    schema_version: str = SCHEMA_VERSION
    record_type: ClassVar[str] = "workspace"

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        _require_id(self.workspace_id, "workspace_id")
        if self.workspace_type not in {"fixture", "project"}:
            raise ValueError("workspace_type must be fixture or project")
        if self.workspace_type == "fixture" and self.data_scope != "test":
            raise ValueError("fixture workspace must use data_scope=test")
        if self.status not in WORKSPACE_STATUSES:
            raise ValueError(f"unsupported workspace status: {self.status}")
        repository_ids: set[str] = set()
        for item in self.repositories:
            repository_id = str(item.get("repository_id") or "")
            _require_id(repository_id, "repository_id")
            if repository_id in repository_ids:
                raise ValueError(f"duplicate repository_id: {repository_id}")
            repository_ids.add(repository_id)
            _relative_path(str(item.get("path") or ""), "repository path")
        for base in ("literature", "code", "writing"):
            scope = self.knowledge.get(base)
            if not isinstance(scope, Mapping):
                raise ValueError(f"missing knowledge scope: {base}")
            namespace = str(scope.get("namespace") or "")
            if self.workspace_type == "fixture" and not namespace.startswith("fixture-"):
                raise ValueError(f"fixture {base} namespace must start with fixture-")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Workspace":
        data = dict(value)
        data["repositories"] = tuple(data.get("repositories") or ())
        return cls(**data)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExperimentRecord:
    experiment_id: str
    workspace_id: str
    run_id: str
    objective: str
    hypothesis: str
    repository_id: str
    git_commit: str
    git_dirty: bool
    environment_id: str
    command: str
    config_path: str
    config_hash: str
    dataset: Mapping[str, Any]
    status: str
    started_at: str
    ended_at: str | None
    seed: int
    metrics: Mapping[str, float | int]
    artifacts: tuple[Mapping[str, Any], ...]
    experiment_type: str = "fixture"
    data_scope: str = "test"
    failure_id: str | None = None
    retry_of: str | None = None
    error_summary: str | None = None
    log_path: str | None = None
    conclusion: str = ""
    next_actions: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION
    record_type: ClassVar[str] = "experiment"

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        for name, value in (
            ("experiment_id", self.experiment_id),
            ("workspace_id", self.workspace_id),
            ("run_id", self.run_id),
            ("repository_id", self.repository_id),
            ("environment_id", self.environment_id),
        ):
            _require_id(value, name)
        if self.experiment_type != "fixture" or self.data_scope != "test":
            raise ValueError("V3 fixture experiment must remain isolated")
        if self.status not in EXPERIMENT_STATUSES:
            raise ValueError(f"unsupported experiment status: {self.status}")
        if self.status in {"completed", "failed", "cancelled", "invalid"} and not self.ended_at:
            raise ValueError("terminal experiment must have ended_at")
        if self.status == "failed" and not self.error_summary:
            raise ValueError("failed experiment must have error_summary")
        if self.status == "completed" and not self.metrics:
            raise ValueError("completed experiment must have metrics")
        _relative_path(self.config_path, "config_path")
        if self.log_path:
            _relative_path(self.log_path, "log_path")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExperimentRecord":
        data = dict(value)
        data["artifacts"] = tuple(data.get("artifacts") or ())
        data["next_actions"] = tuple(data.get("next_actions") or ())
        return cls(**data)


@dataclass(frozen=True, slots=True, kw_only=True)
class FailureRecord:
    failure_id: str
    workspace_id: str
    experiment_id: str
    failure_type: str
    symptom: str
    error_type: str
    error_message: str
    root_cause: str
    root_cause_status: str
    evidence: tuple[Mapping[str, Any], ...]
    status: str
    resolved_by_experiment: str | None = None
    affected_files: tuple[str, ...] = ()
    affected_symbols: tuple[str, ...] = ()
    failed_attempts: tuple[str, ...] = ()
    working_solution: str = ""
    applicable_conditions: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION
    record_type: ClassVar[str] = "failure"

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        for name, value in (
            ("failure_id", self.failure_id),
            ("workspace_id", self.workspace_id),
            ("experiment_id", self.experiment_id),
        ):
            _require_id(value, name)
        if self.root_cause_status not in ROOT_CAUSE_STATUSES:
            raise ValueError("invalid root_cause_status")
        if self.status not in {"open", "resolved", "invalid"}:
            raise ValueError("invalid failure status")
        if self.status == "resolved" and not self.resolved_by_experiment:
            raise ValueError("resolved failure must reference a fixing experiment")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionRecord:
    decision_id: str
    workspace_id: str
    title: str
    status: str
    context: str
    question: str
    alternatives: tuple[Mapping[str, Any], ...]
    decision: str
    rationale: str
    evidence: tuple[Mapping[str, Any], ...]
    consequences: tuple[str, ...]
    revisit_conditions: tuple[str, ...]
    created_at: str
    schema_version: str = SCHEMA_VERSION
    record_type: ClassVar[str] = "decision"

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        _require_id(self.decision_id, "decision_id")
        _require_id(self.workspace_id, "workspace_id")
        if self.status not in {"proposed", "accepted", "rejected", "superseded"}:
            raise ValueError("invalid decision status")
        if len(self.alternatives) < 2:
            raise ValueError("decision requires at least two alternatives")
        if not self.evidence:
            raise ValueError("decision requires evidence")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaimRecord:
    claim_id: str
    workspace_id: str
    claim_type: str
    claim: str
    status: str
    evidence: tuple[Mapping[str, Any], ...]
    scope: str
    limitations: tuple[str, ...]
    counter_evidence: tuple[Mapping[str, Any], ...] = ()
    manuscript_locations: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION
    record_type: ClassVar[str] = "claim"

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        _require_id(self.claim_id, "claim_id")
        _require_id(self.workspace_id, "workspace_id")
        if self.status not in CLAIM_STATUSES:
            raise ValueError("invalid claim status")
        if self.status == "supported" and not self.evidence:
            raise ValueError("supported claim requires evidence")
        if not self.scope or not self.limitations:
            raise ValueError("claim requires scope and limitations")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextBudget:
    max_records: int = 20
    max_characters: int = 12_000
    days: int | None = None
    experiment_ids: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    include_raw_logs: bool = False
    include_paper_fragments: bool = False

    def __post_init__(self) -> None:
        if self.max_records < 1 or self.max_characters < 256:
            raise ValueError("context budget must be positive")
