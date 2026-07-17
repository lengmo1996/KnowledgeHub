"""End-to-end orchestration for the controlled V3 vision fixture."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from knowledgehub.core.atomic import atomic_write_text
from knowledgehub.core.hashing import sha256_file
from knowledgehub.project.models import (
    ClaimRecord,
    DecisionRecord,
    ExperimentRecord,
    FailureRecord,
    Workspace,
)
from knowledgehub.project.registry import ProjectRegistry, utc_now

WORKSPACE_ID = "fixture-vision-project"

EXPERIMENTS = (
    ("fixture-vision-exp-001", "baseline", "Validate baseline training and recording", None),
    ("fixture-vision-exp-002", "fusion_add", "Evaluate addition fusion", None),
    ("fixture-vision-exp-003", "fusion_concat", "Evaluate concatenation projection", None),
    ("fixture-vision-exp-004", "failure_nan", "Reproduce controlled non-finite failure", None),
    ("fixture-vision-exp-005", "failure_fix", "Verify the controlled failure fix", "fixture-vision-exp-004"),
)


class FixtureOrchestrator:
    def __init__(
        self,
        repository_root: Path | str,
        registry: ProjectRegistry,
    ) -> None:
        self.repository_root = Path(repository_root).resolve(strict=True)
        self.fixture_root = self.repository_root / "fixtures" / "v3" / "fixture_vision_project"
        self.registry = registry

    def initialize(self) -> dict[str, Any]:
        try:
            existing = self.registry.get(WORKSPACE_ID)
            workspace_result = {"status": "unchanged", "workspace": existing}
        except KeyError:
            now = utc_now()
            workspace = Workspace(
                workspace_id=WORKSPACE_ID,
                name="Fixture Vision Project",
                description="Controlled project for validating KnowledgeHub V3 workflows",
                research={
                    "domain": ["computer_vision", "representation_learning"],
                    "questions": [
                        "Do addition and concatenation fusion differ on the controlled synthetic task?"
                    ],
                    "hypotheses": [
                        "Concatenation may improve capacity at higher parameter complexity."
                    ],
                },
                repositories=(
                    {
                        "repository_id": "fixture-main",
                        "role": "primary",
                        "path": "fixtures/v3/fixture_vision_project",
                    },
                ),
                environments={"development": "fixture-cpu", "evaluation": "fixture-cpu"},
                knowledge={
                    "literature": {
                        "enabled": True,
                        "scope": "fixture",
                        "namespace": "fixture-literature-v1",
                        "collection": "local-jsonl",
                    },
                    "code": {
                        "enabled": True,
                        "scope": "fixture",
                        "namespace": "fixture-code-v1",
                        "repository_id": "fixture-main",
                    },
                    "writing": {
                        "enabled": True,
                        "scope": "fixture",
                        "namespace": "fixture-writing-v1",
                        "collection": "local-jsonl",
                    },
                },
                created_at=now,
                updated_at=now,
            )
            workspace_result = self.registry.create(workspace)
        environment = self.registry.capture_fixture_environment(WORKSPACE_ID, "fixture-cpu")
        return {"workspace": workspace_result, "environment": environment}

    def run_all(self) -> dict[str, Any]:
        initialized = self.initialize()
        experiments = [self._run_experiment(*spec) for spec in EXPERIMENTS]
        failure = self._record_failure()
        decision = self._record_decision()
        claims = self._record_claims()
        validation = self.registry.validate(WORKSPACE_ID, repository_root=self.repository_root)
        return {
            "initialized": initialized,
            "experiments": experiments,
            "failure": failure,
            "decision": decision,
            "claims": claims,
            "validation": validation,
        }

    def _run_experiment(
        self,
        experiment_id: str,
        config_name: str,
        objective: str,
        retry_of: str | None,
    ) -> dict[str, Any]:
        try:
            return {"status": "unchanged", "record": self.registry.get_record(
                WORKSPACE_ID, "experiment", experiment_id
            )}
        except KeyError:
            pass
        config = self.fixture_root / "configs" / f"{config_name}.yaml"
        run_dir = self.registry.workspace_dir(WORKSPACE_ID) / "runs" / experiment_id
        metrics_path = run_dir / "metrics.json"
        log_path = run_dir / "run.log"
        started_at = utc_now()
        command = [
            sys.executable,
            "-m",
            "fixture_vision.train",
            "--config",
            str(config),
            "--output",
            str(metrics_path),
        ]
        environment = dict(os.environ)
        fixture_source = str(self.fixture_root / "src")
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            f"{fixture_source}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else fixture_source
        )
        git_commit, git_dirty = self._git_state()
        relative_config = str(config.relative_to(self.repository_root))
        running = ExperimentRecord(
            experiment_id=experiment_id,
            workspace_id=WORKSPACE_ID,
            run_id=f"run-{experiment_id}",
            objective=objective,
            hypothesis="Concatenation may improve capacity at higher parameter complexity.",
            repository_id="fixture-main",
            git_commit=git_commit,
            git_dirty=git_dirty,
            environment_id="fixture-cpu",
            command=(
                f"python -m fixture_vision.train --config {relative_config} "
                f"--output state/fixtures/{WORKSPACE_ID}/runs/{experiment_id}/metrics.json"
            ),
            config_path=relative_config,
            config_hash=sha256_file(config),
            dataset={
                "type": "synthetic",
                "generator_config": {"samples": 240, "input_dim": 4, "split": [0.6, 0.2, 0.2]},
            },
            status="running",
            started_at=started_at,
            ended_at=None,
            seed=42,
            metrics={},
            artifacts=(),
            retry_of=retry_of,
        )
        self.registry.put_record(
            WORKSPACE_ID, "experiment", experiment_id, running.to_dict()
        )
        completed = subprocess.run(
            command,
            cwd=self.fixture_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        log = f"STDOUT\n{completed.stdout}\nSTDERR\n{completed.stderr}"
        atomic_write_text(log_path, log, mode=0o600)
        ended_at = utc_now()
        failed = completed.returncode != 0
        metrics: dict[str, float | int] = {}
        artifacts: list[dict[str, Any]] = [self._artifact(log_path, "log")]
        if metrics_path.is_file():
            loaded = json.loads(metrics_path.read_text(encoding="utf-8"))
            metrics = {
                key: value
                for key, value in loaded.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
            artifacts.append(self._artifact(metrics_path, "metrics"))
        artifacts.append(
            {
                "artifact_type": "config",
                "path": str(config.relative_to(self.repository_root)),
                "content_hash": sha256_file(config),
                "size": config.stat().st_size,
            }
        )
        record = ExperimentRecord(
            experiment_id=experiment_id,
            workspace_id=WORKSPACE_ID,
            run_id=f"run-{experiment_id}",
            objective=objective,
            hypothesis="Concatenation may improve capacity at higher parameter complexity.",
            repository_id="fixture-main",
            git_commit=git_commit,
            git_dirty=git_dirty,
            environment_id="fixture-cpu",
            command=(
                f"python -m fixture_vision.train --config {relative_config} "
                f"--output state/fixtures/{WORKSPACE_ID}/runs/{experiment_id}/metrics.json"
            ),
            config_path=relative_config,
            config_hash=sha256_file(config),
            dataset={
                "type": "synthetic",
                "generator_config": {"samples": 240, "input_dim": 4, "split": [0.6, 0.2, 0.2]},
            },
            status="failed" if failed else "completed",
            started_at=started_at,
            ended_at=ended_at,
            seed=42,
            metrics=metrics,
            artifacts=tuple(artifacts),
            failure_id="fixture-failure-001" if experiment_id == "fixture-vision-exp-004" else None,
            retry_of=retry_of,
            error_summary=(
                "FloatingPointError: controlled fixture failure: injected non-finite loss"
                if failed
                else None
            ),
            log_path=f"runs/{experiment_id}/run.log",
            conclusion=(
                "Controlled failure reproduced without changing other experiment records."
                if failed
                else "Fixture run completed; interpretation is limited to this synthetic setup."
            ),
            next_actions=(
                ("Run the finite retry configuration.",)
                if failed
                else ("Compare only with matched fixture runs.",)
            ),
        )
        if experiment_id == "fixture-vision-exp-004" and not failed:
            raise RuntimeError("controlled failure experiment unexpectedly succeeded")
        final = record.to_dict()
        return self.registry.transition_experiment(
            WORKSPACE_ID,
            experiment_id,
            str(final["status"]),
            {key: value for key, value in final.items() if key != "status"},
        )

    def _record_failure(self) -> dict[str, Any]:
        try:
            current = self.registry.get_record(WORKSPACE_ID, "failure", "fixture-failure-001")
            return {"status": "unchanged", "record": current}
        except KeyError:
            pass
        record = FailureRecord(
            failure_id="fixture-failure-001",
            workspace_id=WORKSPACE_ID,
            experiment_id="fixture-vision-exp-004",
            failure_type="numerical_error",
            symptom="Training stops with a controlled non-finite-loss error after epoch one.",
            error_type="FloatingPointError",
            error_message="controlled fixture failure: injected non-finite loss",
            affected_files=(
                "fixtures/v3/fixture_vision_project/src/fixture_vision/train.py",
                "fixtures/v3/fixture_vision_project/configs/failure_nan.yaml",
            ),
            affected_symbols=("fixture_vision.train.run",),
            root_cause="The failure_nan fixture explicitly enables inject_nan for deterministic testing.",
            root_cause_status="confirmed",
            evidence=(
                {"type": "experiment", "record_id": "fixture-vision-exp-004"},
                {
                    "type": "log",
                    "path": "runs/fixture-vision-exp-004/run.log",
                },
                {"type": "source_code", "evidence_id": "fixture-code-002"},
                {"type": "experiment", "record_id": "fixture-vision-exp-005"},
            ),
            failed_attempts=("fixture-vision-exp-004",),
            working_solution="Disable inject_nan while retaining the same data, seed, and optimizer settings.",
            applicable_conditions=("inject_nan=true",),
            resolved_by_experiment="fixture-vision-exp-005",
            status="resolved",
        )
        return self.registry.put_record(WORKSPACE_ID, "failure", record.failure_id, record.to_dict())

    def _record_decision(self) -> dict[str, Any]:
        try:
            current = self.registry.get_record(WORKSPACE_ID, "decision", "fixture-decision-001")
            return {"status": "unchanged", "record": current}
        except KeyError:
            pass
        addition = self.registry.get_record(
            WORKSPACE_ID, "experiment", "fixture-vision-exp-002"
        )
        concat = self.registry.get_record(
            WORKSPACE_ID, "experiment", "fixture-vision-exp-003"
        )
        add_accuracy = float(addition["metrics"]["validation_accuracy"])
        concat_accuracy = float(concat["metrics"]["validation_accuracy"])
        add_parameters = int(addition["metrics"]["parameter_count"])
        concat_parameters = int(concat["metrics"]["parameter_count"])
        if concat_accuracy > add_accuracy:
            decision = "Retain concatenation_projection as the fixture default."
            rationale = "It had higher validation accuracy in the matched fixture run."
        else:
            decision = "Retain addition as the fixture default."
            rationale = (
                "Concatenation did not improve validation accuracy, while addition used fewer parameters."
            )
        record = DecisionRecord(
            decision_id="fixture-decision-001",
            workspace_id=WORKSPACE_ID,
            title="Select the default fixture fusion strategy",
            status="accepted",
            context="Both fusion variants used the same synthetic data, seed, environment, and commit.",
            question="Which fusion should be the default for this controlled fixture?",
            alternatives=(
                {
                    "name": "addition",
                    "advantages": ["lower parameter count", "simple shape contract"],
                    "disadvantages": ["fixed elementwise interaction"],
                },
                {
                    "name": "concatenation_projection",
                    "advantages": ["learnable cross-branch projection"],
                    "disadvantages": ["higher parameter count"],
                },
            ),
            decision=decision,
            rationale=rationale,
            evidence=(
                {
                    "type": "experiment_comparison",
                    "experiment_ids": ["fixture-vision-exp-002", "fixture-vision-exp-003"],
                },
                {
                    "type": "experiment_metric",
                    "record_id": "fixture-vision-exp-002",
                    "json_pointer": "/metrics/validation_accuracy",
                },
                {
                    "type": "experiment_metric",
                    "record_id": "fixture-vision-exp-003",
                    "json_pointer": "/metrics/validation_accuracy",
                },
                {
                    "type": "configuration",
                    "values": {"addition_parameters": add_parameters, "concat_parameters": concat_parameters},
                },
                {"type": "source_code", "evidence_id": "fixture-code-001"},
            ),
            consequences=("The choice is scoped to the Fixture and is not a production recommendation.",),
            revisit_conditions=("additional seeds change the ranking", "a real project pilot begins"),
            created_at=str(concat["ended_at"]),
        )
        return self.registry.put_record(WORKSPACE_ID, "decision", record.decision_id, record.to_dict())

    def _record_claims(self) -> list[dict[str, Any]]:
        addition = self.registry.get_record(
            WORKSPACE_ID, "experiment", "fixture-vision-exp-002"
        )
        concat = self.registry.get_record(
            WORKSPACE_ID, "experiment", "fixture-vision-exp-003"
        )
        add_accuracy = float(addition["metrics"]["validation_accuracy"])
        concat_accuracy = float(concat["metrics"]["validation_accuracy"])
        performance_status = "supported" if concat_accuracy > add_accuracy else "contradicted"
        claims = (
            ClaimRecord(
                claim_id="fixture-claim-001",
                workspace_id=WORKSPACE_ID,
                claim_type="experimental",
                claim=(
                    "Concatenation fusion achieved higher validation accuracy than addition fusion "
                    f"in the controlled fixture ({concat_accuracy:.6f} vs {add_accuracy:.6f})."
                ),
                status=performance_status,
                evidence=(
                    {
                        "type": "experiment_metric",
                        "record_id": "fixture-vision-exp-002",
                        "json_pointer": "/metrics/validation_accuracy",
                    },
                    {
                        "type": "experiment_metric",
                        "record_id": "fixture-vision-exp-003",
                        "json_pointer": "/metrics/validation_accuracy",
                    },
                ),
                scope="Controlled synthetic fixture, seed 42.",
                limitations=("single seed", "synthetic data", "tiny NumPy model"),
            ),
            ClaimRecord(
                claim_id="fixture-claim-002",
                workspace_id=WORKSPACE_ID,
                claim_type="complexity",
                claim="Concatenation projection used more parameters than addition in the fixture.",
                status=(
                    "supported"
                    if int(concat["metrics"]["parameter_count"])
                    > int(addition["metrics"]["parameter_count"])
                    else "contradicted"
                ),
                evidence=(
                    {
                        "type": "experiment_metric",
                        "record_id": "fixture-vision-exp-002",
                        "json_pointer": "/metrics/parameter_count",
                    },
                    {
                        "type": "experiment_metric",
                        "record_id": "fixture-vision-exp-003",
                        "json_pointer": "/metrics/parameter_count",
                    },
                    {"type": "source_code", "evidence_id": "fixture-code-001"},
                ),
                scope="The exact architectures in fixture version 0.1.0.",
                limitations=("parameter count is not a complete compute-cost measurement",),
            ),
            ClaimRecord(
                claim_id="fixture-claim-003",
                workspace_id=WORKSPACE_ID,
                claim_type="limitation",
                claim="The observed fusion results cannot be generalized to real vision datasets.",
                status="supported",
                evidence=(
                    {
                        "type": "configuration",
                        "record_id": "fixture-vision-exp-002",
                        "json_pointer": "/dataset/type",
                    },
                    {"type": "literature", "evidence_id": "fixture-lit-002"},
                ),
                scope="Interpretation boundary for every fixture experiment.",
                limitations=("This is a scope statement, not empirical evidence about real datasets.",),
            ),
        )
        results: list[dict[str, Any]] = []
        for claim in claims:
            try:
                current = self.registry.get_record(WORKSPACE_ID, "claim", claim.claim_id)
                results.append({"status": "unchanged", "record": current})
            except KeyError:
                results.append(
                    self.registry.put_record(WORKSPACE_ID, "claim", claim.claim_id, claim.to_dict())
                )
        return results

    def _artifact(self, path: Path, artifact_type: str) -> dict[str, Any]:
        return {
            "artifact_type": artifact_type,
            "path": str(path.relative_to(self.registry.workspace_dir(WORKSPACE_ID))),
            "content_hash": sha256_file(path),
            "size": path.stat().st_size,
        }

    def _git_state(self) -> tuple[str, bool]:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repository_root,
            capture_output=True,
            text=True,
            check=False,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.repository_root,
            capture_output=True,
            text=True,
            check=False,
        )
        return (commit.stdout.strip() or "unavailable", bool(status.stdout.strip()))
