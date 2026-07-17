from __future__ import annotations

from pathlib import Path

from knowledgehub.project.fixture import FixtureOrchestrator
from knowledgehub.project.registry import ProjectRegistry


def _pointer(record: dict, pointer: str):
    value = record
    for part in pointer.strip("/").split("/"):
        value = value[part]
    return value


def test_complete_fixture_is_traceable_idempotent_and_cleanable(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / "fixtures")
    orchestrator = FixtureOrchestrator(Path.cwd(), registry)
    first = orchestrator.run_all()
    second = orchestrator.run_all()
    experiments = registry.list_records("fixture-vision-project", "experiment")
    assert len(experiments) == 5
    assert [item["status"] for item in experiments].count("failed") == 1
    assert all(item["status"] == "unchanged" for item in second["experiments"])
    failure = registry.get_record("fixture-vision-project", "failure", "fixture-failure-001")
    assert failure["root_cause_status"] == "confirmed"
    assert failure["resolved_by_experiment"] == "fixture-vision-exp-005"
    decision = registry.get_record("fixture-vision-project", "decision", "fixture-decision-001")
    assert len(decision["alternatives"]) == 2
    for claim in registry.list_records("fixture-vision-project", "claim"):
        for evidence in claim["evidence"]:
            if evidence.get("json_pointer") and evidence.get("record_id", "").startswith(
                "fixture-vision-exp"
            ):
                record = registry.get_record(
                    "fixture-vision-project", "experiment", evidence["record_id"]
                )
                assert _pointer(record, evidence["json_pointer"]) is not None
    plan = registry.cleanup("fixture-vision-project")
    assert plan["dry_run"] is True
    assert plan["shared_knowledge_bases_deleted"] is False
    assert first["validation"]["valid"]
    registry.cleanup("fixture-vision-project", execute=True)
    assert not registry.workspace_dir("fixture-vision-project").exists()
    rebuilt = orchestrator.run_all()
    assert rebuilt["validation"]["valid"]
