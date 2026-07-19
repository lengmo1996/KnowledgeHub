from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from knowledgehub.cli.writing_material import add_writing_material_parser
from knowledgehub.core.atomic import atomic_write_json
from knowledgehub.core.hashing import sha256_json
from knowledgehub.writing_rag.release_retention import (
    WritingMaterialReleaseRetirementService,
)
from knowledgehub.writing_rag.retention import RetentionDispositionError

RUN_ID = "run-release-retirement"
ACTIVE = "writing_release_current_fixture"
HISTORICAL = "writing_release_old_fixture"
FALLBACK = "writing_fallback_fixture"


class FakeBackend:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.alias = ACTIVE
        healthy = {
            "exists": True,
            "status": "green",
            "points": 10,
            "schema": {"vectors": {"size": 4}},
        }
        self.collections = {
            ACTIVE: dict(healthy),
            HISTORICAL: dict(healthy),
            FALLBACK: dict(healthy),
        }
        self.inspect_calls = 0
        self.fail_once: str | None = None

    def inspect(self, collection: str) -> dict[str, Any]:
        self.inspect_calls += 1
        return dict(self.collections.get(collection, {"exists": False}))

    def alias_target(self, alias: str) -> str | None:
        assert alias == "knowledgehub_writing_current"
        return self.alias

    def delete_collection(self, collection: str) -> None:
        self.events.append(f"delete:{collection}")
        if self.fail_once == collection:
            self.fail_once = None
            raise OSError("fixture deletion interruption")
        self.collections.pop(collection, None)


class FakePromotion:
    def __init__(self, backend: FakeBackend, events: list[str]) -> None:
        self.backend = backend
        self.events = events
        self.current: dict[str, Any] = {
            "status": "active",
            "active_collection": ACTIVE,
            "previous_collection": FALLBACK,
        }

    def status(self, knowledge_base: str, fallback: str) -> dict[str, Any]:
        assert knowledge_base == "writing"
        assert fallback == FALLBACK
        return {
            "alias": "knowledgehub_writing_current",
            "current": dict(self.current),
        }

    def rollback(self, knowledge_base: str, *, confirmed: bool = False) -> dict[str, Any]:
        assert knowledge_base == "writing" and confirmed
        self.events.append("rollback")
        self.backend.alias = FALLBACK
        self.current = {
            "status": "active",
            "active_collection": FALLBACK,
            "previous_collection": ACTIVE,
        }
        return dict(self.current)

    def finalize_retired_previous(
        self,
        knowledge_base: str,
        retired_collection: str,
        *,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        assert knowledge_base == "writing" and confirmed
        assert retired_collection == ACTIVE
        self.events.append("finalize")
        self.current["previous_collection"] = None
        return dict(self.current)


def _service(
    tmp_path: Path,
    *,
    approved_at: str = "2026-07-19T06:47:32+00:00",
) -> tuple[
    WritingMaterialReleaseRetirementService,
    FakeBackend,
    FakePromotion,
    list[str],
]:
    data_root = tmp_path / "writing-materials"
    run_dir = data_root / "runs" / RUN_ID
    run_dir.mkdir(parents=True, mode=0o700)
    run_dir.chmod(0o700)
    atomic_write_json(
        run_dir / "manifest.json",
        {
            "run_id": RUN_ID,
            "status": "success",
            "versions": {"provider": "openai_compatible"},
            "pilot_approval": {
                "approved_at": approved_at,
                "rights_basis": "private research use",
                "retention_policy": "five years",
                "access_policy": "local reviewer only",
            },
        },
        mode=0o600,
    )
    events: list[str] = []
    backend = FakeBackend(events)
    promotion = FakePromotion(backend, events)
    service = WritingMaterialReleaseRetirementService(
        data_root,
        backend,
        promotion,
        fallback_collection=FALLBACK,
    )
    return service, backend, promotion, events


def _reference(service: WritingMaterialReleaseRetirementService, collection: str) -> Path:
    group = "releases" if collection == ACTIVE else "index-candidates"
    filename = "manifest.json" if group == "releases" else "writing-material-candidate.json"
    path = service.reference_roots[group] / collection / filename
    payload = {
        "schema_version": "fixture-v1",
        "run_id": RUN_ID,
        "candidate_collection": collection,
        "status": "validated",
    }
    atomic_write_json(
        path,
        {**payload, "artifact_fingerprint": sha256_json(payload)},
        mode=0o600,
    )
    return path


def test_release_retirement_plan_is_zero_io_and_zero_write_before_expiry(
    tmp_path: Path,
) -> None:
    service, backend, _, _ = _service(tmp_path)
    result = service.plan(
        RUN_ID,
        now=datetime(2031, 7, 19, 6, 47, 31, tzinfo=timezone.utc),
    )
    assert result["status"] == "not_due"
    assert result["references"] == []
    assert result["writes_performed"] is False
    assert backend.inspect_calls == 0
    assert not service.retention_root.exists()


def test_expired_release_plan_requires_healthy_independent_fallback(tmp_path: Path) -> None:
    service, backend, _, _ = _service(tmp_path)
    _reference(service, ACTIVE)
    _reference(service, HISTORICAL)
    ready = service.plan(RUN_ID, now=datetime(2032, 1, 1, tzinfo=timezone.utc))
    assert ready["status"] == "ready"
    assert ready["alias_action"] == {
        "operation": "rollback",
        "alias": "knowledgehub_writing_current",
        "retired_collection": ACTIVE,
        "fallback_collection": FALLBACK,
    }
    assert {item["collection"] for item in ready["collections"]} == {
        ACTIVE,
        HISTORICAL,
    }
    backend.collections[FALLBACK]["status"] = "red"
    blocked = service.plan(RUN_ID, now=datetime(2032, 1, 1, tzinfo=timezone.utc))
    assert blocked["status"] == "blocked"
    assert "fallback collection is missing or unhealthy" in blocked["blockers"]


def test_release_retirement_rolls_back_before_delete_and_quarantines_references(
    tmp_path: Path,
) -> None:
    service, backend, promotion, events = _service(tmp_path)
    active_reference = _reference(service, ACTIVE)
    historical_reference = _reference(service, HISTORICAL)
    expired = datetime(2032, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(RetentionDispositionError, match="explicit confirmation"):
        service.decommission(RUN_ID, confirmed=False, now=expired)
    receipt = service.decommission(RUN_ID, confirmed=True, now=expired)
    assert receipt["status"] == "completed"
    assert receipt["rollback_performed"] is True
    assert receipt["rollback_performed_this_attempt"] is True
    assert receipt["alias_target"] == FALLBACK
    assert events[0] == "rollback"
    assert events[-1] == "finalize"
    assert all(
        events.index(f"delete:{name}") > events.index("rollback") for name in (ACTIVE, HISTORICAL)
    )
    assert promotion.current["previous_collection"] is None
    assert not active_reference.exists()
    assert not historical_reference.exists()
    assert len(receipt["quarantined_directories"]) == 2
    assert service.decommission(RUN_ID, confirmed=True, now=expired) == receipt
    assert backend.inspect(FALLBACK)["exists"] is True


def test_release_retirement_recovers_after_partial_collection_deletion(tmp_path: Path) -> None:
    service, backend, promotion, events = _service(tmp_path)
    _reference(service, ACTIVE)
    _reference(service, HISTORICAL)
    backend.fail_once = HISTORICAL
    expired = datetime(2032, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(OSError, match="deletion interruption"):
        service.decommission(RUN_ID, confirmed=True, now=expired)
    assert backend.alias == FALLBACK
    assert promotion.current["previous_collection"] == ACTIVE
    receipt = service.decommission(RUN_ID, confirmed=True, now=expired)
    assert receipt["status"] == "completed"
    assert receipt["rollback_performed"] is True
    assert receipt["rollback_performed_this_attempt"] is False
    assert events.count("rollback") == 1
    assert promotion.current["previous_collection"] is None


def test_release_retirement_forgets_inactive_previous_without_another_rollback(
    tmp_path: Path,
) -> None:
    service, backend, promotion, events = _service(tmp_path)
    _reference(service, ACTIVE)
    backend.alias = FALLBACK
    promotion.current = {
        "status": "active",
        "active_collection": FALLBACK,
        "previous_collection": ACTIVE,
    }
    expired = datetime(2032, 1, 1, tzinfo=timezone.utc)
    plan = service.plan(RUN_ID, now=expired)
    assert plan["status"] == "ready"
    assert plan["alias_action"]["operation"] == "retire_previous"
    receipt = service.decommission(RUN_ID, confirmed=True, now=expired)
    assert receipt["rollback_performed"] is False
    assert "rollback" not in events
    assert backend.alias == FALLBACK
    assert promotion.current["previous_collection"] is None


def test_release_retirement_blocks_live_alias_drift(tmp_path: Path) -> None:
    service, backend, _, _ = _service(tmp_path)
    _reference(service, ACTIVE)
    backend.alias = HISTORICAL
    result = service.plan(RUN_ID, now=datetime(2032, 1, 1, tzinfo=timezone.utc))
    assert result["status"] == "blocked"
    assert "live alias target differs from promotion state" in result["blockers"]


def test_release_retirement_resume_rejects_new_collection_owner(tmp_path: Path) -> None:
    service, backend, _, _ = _service(tmp_path)
    _reference(service, ACTIVE)
    backend.fail_once = ACTIVE
    expired = datetime(2032, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(OSError, match="deletion interruption"):
        service.decommission(RUN_ID, confirmed=True, now=expired)
    other = service.reference_roots["release-candidates"] / "other" / "manifest.json"
    payload = {"run_id": "other-run", "candidate_collection": ACTIVE}
    atomic_write_json(
        other,
        {**payload, "artifact_fingerprint": sha256_json(payload)},
        mode=0o600,
    )
    with pytest.raises(RetentionDispositionError, match="another run owner"):
        service.decommission(RUN_ID, confirmed=True, now=expired)
    assert backend.inspect(ACTIVE)["exists"] is True


def test_release_retirement_blocks_shared_collection_and_invalid_fingerprint(
    tmp_path: Path,
) -> None:
    service, _, _, _ = _service(tmp_path)
    reference = _reference(service, ACTIVE)
    other = service.reference_roots["release-candidates"] / "other" / "manifest.json"
    payload = {
        "run_id": "other-run",
        "candidate_collection": ACTIVE,
    }
    atomic_write_json(
        other,
        {**payload, "artifact_fingerprint": sha256_json(payload)},
        mode=0o600,
    )
    shared = service.plan(RUN_ID, now=datetime(2032, 1, 1, tzinfo=timezone.utc))
    assert shared["status"] == "blocked"
    assert f"collection ownership is ambiguous: {ACTIVE}" in shared["blockers"]
    value = {
        "run_id": RUN_ID,
        "candidate_collection": ACTIVE,
        "artifact_fingerprint": "tampered",
    }
    atomic_write_json(reference, value, mode=0o600)
    invalid = service.plan(RUN_ID, now=datetime(2032, 1, 1, tzinfo=timezone.utc))
    assert invalid["status"] == "blocked"
    assert "artifact fingerprint is invalid" in " ".join(invalid["blockers"])


def test_release_retirement_cli_is_confirmation_gated() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_writing_material_parser(commands)
    plan = parser.parse_args(
        ["writing-material", "retention", "plan-release-retirement", "--run-id", RUN_ID]
    )
    execute = parser.parse_args(
        [
            "writing-material",
            "retention",
            "decommission-release",
            "--run-id",
            RUN_ID,
            "--yes",
        ]
    )
    assert plan.writing_material_retention_command == "plan-release-retirement"
    assert execute.writing_material_retention_command == "decommission-release"
    assert execute.yes is True
