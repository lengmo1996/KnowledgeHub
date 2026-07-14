from __future__ import annotations

from pathlib import Path

import pytest

from knowledgehub.manifests.catalog import (
    append_delta_catalog,
    read_delta_catalog,
    validate_delta_files,
)
from knowledgehub.pipeline.config import GPUDevice, RagConfig
from knowledgehub.pipeline.source import SourceContractError, ZoteroManifestSource
from knowledgehub.pipeline.state import PipelineState


def test_default_config_parses_yaml_off_and_environment_override(tmp_path: Path) -> None:
    config = RagConfig.load(
        Path("configs/rag/default.yaml"),
        environ={"KH_RAG_DATA_DIR": str(tmp_path), "KH_GPU_MODE": "cpu"},
    )
    assert config.reranker_profile == "off"
    assert config.data_dir == tmp_path
    assert config.gpu_mode == "cpu"


def test_dual_gpu_plan_records_physical_identity(tmp_path: Path) -> None:
    devices = tuple(
        GPUDevice(
            logical_id=index,
            physical_id=str(index),
            name="NVIDIA GeForce RTX 3090",
            total_memory_mb=24576,
            free_memory_mb=20000,
            uuid=f"GPU-{index}",
            pci_bus_id=f"0000:{index + 1:02x}:00.0",
        )
        for index in range(2)
    )
    config = RagConfig(data_dir=tmp_path, gpu_mode="dual", gpu_ids=(0, 1))
    plan = config.resolve_gpu_plan(devices)
    assert plan.resolved_mode == "dual"
    assert plan.gpu_ids == (0, 1)
    assert [value.uuid for value in plan.devices] == ["GPU-0", "GPU-1"]


def test_delta_catalog_includes_empty_delta_and_detects_tampering(tmp_path: Path) -> None:
    manifests = tmp_path / "manifests"
    deltas = manifests / "deltas"
    deltas.mkdir(parents=True)
    delta = deltas / "sync-1.jsonl"
    delta.write_text("", encoding="utf-8")
    catalog = manifests / "delta-catalog.jsonl"
    append_delta_catalog(
        current_path=catalog,
        output_path=catalog,
        sync_id="sync-1",
        from_version=0,
        target_version=4,
        staged_delta_path=delta,
        row_count=0,
        created_at="2026-01-01T00:00:00+00:00",
    )
    entries = read_delta_catalog(catalog)
    assert entries[0].sequence == 1
    assert entries[0].row_count == 0
    validate_delta_files(manifests, entries)
    delta.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_delta_files(manifests, entries)


def test_source_rejects_changed_previously_consumed_delta(tmp_path: Path) -> None:
    manifests = tmp_path / "source" / "manifests"
    deltas = manifests / "deltas"
    deltas.mkdir(parents=True)
    snapshot = manifests / "documents.jsonl"
    snapshot.write_text("", encoding="utf-8")
    delta = deltas / "sync-1.jsonl"
    delta.write_text("", encoding="utf-8")
    catalog = manifests / "delta-catalog.jsonl"
    entry = append_delta_catalog(
        current_path=catalog,
        output_path=catalog,
        sync_id="sync-1",
        from_version=0,
        target_version=1,
        staged_delta_path=delta,
        row_count=0,
    )
    state = PipelineState(tmp_path / "rag")
    state.initialize()
    with state.transaction() as connection:
        state.mark_delta_consumed(
            connection,
            source="zotero",
            sequence=1,
            sync_id="sync-1",
            delta_path=entry.delta_path,
            delta_sha256="0" * 64,
        )
    source = ZoteroManifestSource(snapshot, catalog)
    with pytest.raises(SourceContractError, match="hash changed"):
        source.pending_deltas(state)
