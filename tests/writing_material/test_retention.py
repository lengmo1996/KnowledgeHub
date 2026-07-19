from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from knowledgehub.cli.writing_material import (
    _required_permission,
    add_writing_material_parser,
)
from knowledgehub.core.atomic import atomic_write_json
from knowledgehub.writing_rag.retention import (
    RetentionDispositionError,
    WritingMaterialRetentionService,
)

RUN_ID = "run-retention-fixture"


def _run(
    tmp_path: Path,
    *,
    approved_at: str = "2026-07-19T06:47:32+00:00",
    provider: str = "deterministic_fixture",
) -> tuple[Path, WritingMaterialRetentionService]:
    data_root = tmp_path / "writing-materials"
    run_dir = data_root / "runs" / RUN_ID
    run_dir.mkdir(parents=True, mode=0o700)
    run_dir.chmod(0o700)
    manifest = {
        "run_id": RUN_ID,
        "status": "success",
        "versions": {"provider": provider},
        "pilot_approval": {
            "approved_at": approved_at,
            "rights_basis": "private research use",
            "retention_policy": "five years",
            "access_policy": "local reviewer only",
        },
    }
    atomic_write_json(run_dir / "manifest.json", manifest, mode=0o600)
    (run_dir / "evidence.jsonl").write_text('{"fixture":true}\n', encoding="utf-8")
    (run_dir / "evidence.jsonl").chmod(0o600)
    return run_dir, WritingMaterialRetentionService(data_root, quarantine_days=30)


def test_retention_plan_is_zero_write_before_expiration(tmp_path) -> None:
    run_dir, service = _run(tmp_path)
    result = service.plan(
        RUN_ID,
        now=datetime(2031, 7, 19, 6, 47, 31, tzinfo=timezone.utc),
    )
    assert result["status"] == "not_due"
    assert result["counts"] == {
        "scanned": 1,
        "expired": 0,
        "ready": 0,
        "blocked": 0,
        "unmanaged": 0,
    }
    assert result["writes_performed"] is False
    assert run_dir.is_dir()
    assert not service.retention_root.exists()


def test_expired_unreferenced_fixture_is_ready_with_inventory(tmp_path) -> None:
    _, service = _run(tmp_path)
    result = service.plan(
        RUN_ID,
        now=datetime(2031, 7, 19, 6, 47, 32, tzinfo=timezone.utc),
    )
    assert result["status"] == "ready"
    entry = result["entries"][0]
    assert entry["retention_status"] == "expired"
    assert entry["blockers"] == []
    assert [item["path"] for item in entry["inventory"]] == [
        "evidence.jsonl",
        "manifest.json",
    ]


def test_expired_provider_run_blocks_release_references_and_unscoped_cache(tmp_path) -> None:
    _, service = _run(tmp_path, provider="openai_compatible")
    release = service.data_root / "releases" / "writing" / "candidate" / "manifest.json"
    atomic_write_json(release, {"run_id": RUN_ID}, mode=0o600)
    cache = service.data_root / "cache" / "llm" / "cache.json"
    atomic_write_json(cache, {"response": {}}, mode=0o600)
    result = service.plan(RUN_ID, now=datetime(2032, 1, 1, tzinfo=timezone.utc))
    assert result["status"] == "blocked"
    entry = result["entries"][0]
    assert entry["references"] == [str(release)]
    assert entry["blockers"] == [
        "run is referenced by candidate or release artifacts",
        "provider cache lacks per-run retention scope",
    ]
    with pytest.raises(RetentionDispositionError, match="not ready"):
        service.quarantine(
            RUN_ID,
            confirmed=True,
            now=datetime(2032, 1, 1, tzinfo=timezone.utc),
        )


def test_retention_quarantine_then_verified_purge(tmp_path) -> None:
    run_dir, service = _run(tmp_path)
    disposed_at = datetime(2032, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(RetentionDispositionError, match="explicit confirmation"):
        service.quarantine(RUN_ID, confirmed=False, now=disposed_at)
    receipt = service.quarantine(RUN_ID, confirmed=True, now=disposed_at)
    quarantine = service.quarantine_root / RUN_ID
    assert receipt["status"] == "quarantined"
    assert receipt["file_count"] == 2
    assert not run_dir.exists()
    assert quarantine.is_dir()
    assert service.intent_root.stat().st_mode & 0o777 == 0o700
    assert (service.receipt_root / f"{RUN_ID}.json").stat().st_mode & 0o777 == 0o600
    assert service.quarantine(RUN_ID, confirmed=True, now=disposed_at) == receipt
    with pytest.raises(RetentionDispositionError, match="grace period"):
        service.purge(
            RUN_ID,
            confirmed=True,
            now=datetime(2032, 1, 30, tzinfo=timezone.utc),
        )
    purged = service.purge(
        RUN_ID,
        confirmed=True,
        now=datetime(2032, 2, 1, tzinfo=timezone.utc),
    )
    assert purged["status"] == "purged"
    assert purged["run_artifacts_present"] is False
    assert purged["purge_reconciled"] is False
    assert not quarantine.exists()
    assert service.purge(
        RUN_ID,
        confirmed=True,
        now=datetime(2032, 2, 2, tzinfo=timezone.utc),
    ) == purged


def test_quarantine_recovers_move_to_receipt_interruption(tmp_path, monkeypatch) -> None:
    run_dir, service = _run(tmp_path)
    import knowledgehub.writing_rag.retention as retention_module

    real_write = retention_module.atomic_write_json
    failed = False

    def flaky_write(path: Path, value: object, *, mode: int = 0o644) -> Path:
        nonlocal failed
        if Path(path).parent == service.receipt_root and not failed:
            failed = True
            raise OSError("fixture receipt interruption")
        return real_write(path, value, mode=mode)

    monkeypatch.setattr(retention_module, "atomic_write_json", flaky_write)
    now = datetime(2032, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(OSError, match="receipt interruption"):
        service.quarantine(RUN_ID, confirmed=True, now=now)
    assert not run_dir.exists()
    assert (service.quarantine_root / RUN_ID).is_dir()
    receipt = service.quarantine(RUN_ID, confirmed=True, now=now)
    assert receipt["recovered_after_interruption"] is True


def test_purge_rejects_quarantine_content_drift(tmp_path) -> None:
    _, service = _run(tmp_path)
    service.quarantine(
        RUN_ID,
        confirmed=True,
        now=datetime(2032, 1, 1, tzinfo=timezone.utc),
    )
    changed = service.quarantine_root / RUN_ID / "unexpected.json"
    changed.write_text("{}", encoding="utf-8")
    changed.chmod(0o600)
    with pytest.raises(RetentionDispositionError, match="changed before purge"):
        service.purge(
            RUN_ID,
            confirmed=True,
            now=datetime(2032, 2, 1, tzinfo=timezone.utc),
        )


def test_expired_run_with_permission_drift_is_blocked(tmp_path) -> None:
    run_dir, service = _run(tmp_path)
    (run_dir / "manifest.json").chmod(0o640)
    result = service.plan(RUN_ID, now=datetime(2032, 1, 1, tzinfo=timezone.utc))
    assert result["status"] == "blocked"
    assert "accessible by group or other users" in " ".join(
        result["entries"][0]["blockers"]
    )


def test_retention_cli_requires_disposal_permission_and_explicit_destructive_flags() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_writing_material_parser(commands)
    plan = parser.parse_args(["writing-material", "retention", "plan", "--run-id", RUN_ID])
    quarantine = parser.parse_args(
        ["writing-material", "retention", "quarantine", "--run-id", RUN_ID, "--yes"]
    )
    purge = parser.parse_args(
        ["writing-material", "retention", "purge", "--run-id", RUN_ID, "--yes"]
    )
    assert _required_permission(plan) == "writing_material.retention_dispose"
    assert quarantine.yes is True
    assert purge.yes is True
