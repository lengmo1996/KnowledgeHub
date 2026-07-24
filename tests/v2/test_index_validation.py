from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from knowledgehub.cli.main import build_parser
from knowledgehub.core.hashing import sha256_json, sha256_text
from knowledgehub.governance.validation import HubValidator


class FakeQdrant:
    def __init__(self, points: list[SimpleNamespace], *, count: int | None = None) -> None:
        self.points = points
        self.point_count = len(points) if count is None else count

    def get_collection(self, _collection: str) -> SimpleNamespace:
        return SimpleNamespace(points_count=self.point_count, status="green")

    def count(self, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(count=self.point_count)

    def scroll(self, **_kwargs: Any) -> tuple[list[SimpleNamespace], None]:
        return self.points, None


def _write_state(
    rag: Path,
    *,
    document_id: str,
    content_hash: str,
    metadata_hash: str,
    processor: str,
) -> None:
    path = rag / "state" / "index.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents (
              document_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
              metadata_hash TEXT NOT NULL, processor_version TEXT NOT NULL,
              embedding_fingerprint TEXT NOT NULL, active INTEGER NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE tombstones (
              document_id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL, reason TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,1,?)",
            (document_id, content_hash, metadata_hash, processor, "e" * 64, "now"),
        )


def _write_artifact(rag: Path, document_id: str, chunk: dict[str, Any]) -> Path:
    root = rag / "chunks"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{sha256_json(document_id)[:32]}.jsonl"
    path.write_text(json.dumps(chunk) + "\n", encoding="utf-8")
    return path


def _code_fixture(tmp_path: Path) -> tuple[HubValidator, dict[str, Any], FakeQdrant]:
    code_root = tmp_path / "code-source"
    writing_root = tmp_path / "writing-source"
    rag = tmp_path / "rag-code"
    document_id = "code:owner/demo@1.0:src/demo.py"
    text = "def demo():\n    return 1"
    source_text = text + "\n"
    metadata = {
        "knowledge_base": "code",
        "library": "demo",
        "version": "1.0",
        "source_type": "source_code",
        "source_url": "https://example.test/demo.py",
        "commit": "a" * 40,
    }
    source = {
        "document_id": document_id,
        "content_hash": sha256_text(source_text),
        "metadata": metadata,
    }
    normalized = code_root / "normalized" / "demo" / "1.0.jsonl"
    normalized.parent.mkdir(parents=True)
    normalized.write_text(json.dumps(source) + "\n", encoding="utf-8")
    _write_state(
        rag,
        document_id=document_id,
        content_hash=source["content_hash"],
        metadata_hash=sha256_json(metadata),
        processor="code-test-v1",
    )
    chunk = {
        "chunk_id": "11111111-1111-1111-1111-111111111111",
        "document_id": document_id,
        "chunk_index": 0,
        "chunk_fingerprint": "f" * 64,
        "text": text,
        "text_sha256": sha256_text(text),
        "metadata": metadata,
    }
    _write_artifact(rag, document_id, chunk)
    point = SimpleNamespace(
        id=chunk["chunk_id"],
        payload={**metadata, "chunk_id": chunk["chunk_id"], "document_id": document_id},
    )
    validator = HubValidator(
        code_root,
        writing_root,
        rag_dirs={"code": rag},
    )
    return validator, chunk, FakeQdrant([point])


def _writing_fixture(tmp_path: Path) -> tuple[HubValidator, dict[str, Any], FakeQdrant]:
    code_root = tmp_path / "code-source"
    writing_root = tmp_path / "writing-source"
    rag = tmp_path / "rag-writing"
    document_id = "writing:pattern-1"
    text = "Writing function: research_gap\nPattern: However, [gap] remains."
    metadata = {
        "knowledge_base": "writing",
        "writing_id": document_id,
        "source_paper_id": "paper-1",
        "writing_function": "research_gap",
        "source_type": "writing_pattern",
    }
    source = {
        "writing_id": document_id,
        "source_paper_id": "paper-1",
        "writing_function": "research_gap",
    }
    derived = writing_root / "derived" / "writing_entries.jsonl"
    derived.parent.mkdir(parents=True)
    derived.write_text(json.dumps(source) + "\n", encoding="utf-8")
    _write_state(
        rag,
        document_id=document_id,
        content_hash=sha256_text(text),
        metadata_hash=sha256_json(metadata),
        processor="rules-test-v1",
    )
    chunk = {
        "chunk_id": "22222222-2222-2222-2222-222222222222",
        "document_id": document_id,
        "chunk_index": 0,
        "chunk_fingerprint": "f" * 64,
        "text": text,
        "text_sha256": sha256_text(text),
        "metadata": metadata,
    }
    _write_artifact(rag, document_id, chunk)
    point = SimpleNamespace(
        id=chunk["chunk_id"],
        payload={**metadata, "chunk_id": chunk["chunk_id"], "document_id": document_id},
    )
    validator = HubValidator(
        code_root,
        writing_root,
        rag_dirs={"writing": rag},
    )
    return validator, chunk, FakeQdrant([point])


def _writing_material_fixture(
    tmp_path: Path,
) -> tuple[HubValidator, Path, FakeQdrant]:
    code_root = tmp_path / "code-source"
    writing_root = tmp_path / "writing-source"
    rag = tmp_path / "writing-material-candidate"
    document_id = "strategy:material-1"
    text = "Use a bounded contrast to state the research problem."
    metadata = {
        "accepted_snapshot_only": True,
        "asset_type": "strategy",
        "category": "problem_statement",
        "evidence_ids": ["evidence:1"],
        "provenance": [{"document_id": "paper:1"}],
        "quality_score": 0.9,
    }
    _write_state(
        rag,
        document_id=document_id,
        content_hash=sha256_text(text),
        metadata_hash=sha256_json(metadata),
        processor="writing-material-v1",
    )
    chunk = {
        "chunk_id": "33333333-3333-3333-3333-333333333333",
        "document_id": document_id,
        "chunk_index": 0,
        "chunk_fingerprint": "f" * 64,
        "text": text,
        "text_sha256": sha256_text(text),
        "metadata": metadata,
    }
    _write_artifact(rag, document_id, chunk)
    accepted_hash = "a" * 64
    collection = "writing-material-v1"
    candidate_payload = {
        "schema_name": "writing_material_candidate",
        "schema_version": "writing-material-candidate-v1",
        "status": "success",
        "run_id": "run-1",
        "candidate_collection": collection,
        "candidate_data_dir": str(rag),
        "accepted_manifest_sha256": accepted_hash,
        "source_verified": True,
        "accepted_only": True,
        "promotion_performed": False,
        "dry_run": False,
        "selected": 1,
        "indexed": 1,
        "failures": [],
    }
    candidate = {
        **candidate_payload,
        "artifact_fingerprint": sha256_json(candidate_payload),
    }
    (rag / "writing-material-candidate.json").write_text(
        json.dumps(candidate),
        encoding="utf-8",
    )
    release_payload = {
        "schema_name": "writing_material_release",
        "schema_version": "writing-material-release-v1",
        "status": "validated",
        "promotion_eligible": True,
        "candidate_collection": collection,
        "candidate_data_dir": str(rag),
        "accepted_manifest_sha256": accepted_hash,
        "expected_additions": 1,
        "expected_candidate_points": 2,
        "candidate_validation": {"status": "green", "points": 2},
        "merge_result": {"status": "success", "indexed": 1, "failures": []},
    }
    release = {
        **release_payload,
        "artifact_fingerprint": sha256_json(release_payload),
    }
    release_path = tmp_path / "release.json"
    release_path.write_text(json.dumps(release), encoding="utf-8")
    active = {
        "status": "active",
        "active_collection": collection,
        "rag_data_dir": str(rag),
        "release_manifest": str(release_path),
        "artifact_fingerprint": release["artifact_fingerprint"],
        "candidate_points": 2,
    }
    local_point = SimpleNamespace(
        id=chunk["chunk_id"],
        payload={
            **metadata,
            "chunk_id": chunk["chunk_id"],
            "document_id": document_id,
        },
    )
    legacy_point = SimpleNamespace(
        id="22222222-2222-2222-2222-222222222222",
        payload={
            "chunk_id": "22222222-2222-2222-2222-222222222222",
            "document_id": "writing:legacy",
            "knowledge_base": "writing",
        },
    )
    validator = HubValidator(
        code_root,
        writing_root,
        rag_dirs={"writing": rag},
        active_releases={"writing": active},
    )
    return validator, release_path, FakeQdrant([legacy_point, local_point])


def test_code_index_validation_traces_state_artifact_source_and_qdrant(
    tmp_path: Path,
) -> None:
    validator, _chunk, client = _code_fixture(tmp_path)
    result = validator.index("code", qdrant_client=client, collection="code-current")
    assert result["valid"] is True
    assert result["checked"] == {
        "state_documents": 1,
        "active_documents": 1,
        "tombstones": 0,
        "artifacts": 1,
        "chunks": 1,
        "source_records": 1,
    }
    assert result["qdrant"]["points"] == 1


def test_index_validation_detects_hash_and_remote_count_drift(tmp_path: Path) -> None:
    validator, chunk, _client = _code_fixture(tmp_path)
    chunk["text_sha256"] = "0" * 64
    rag = validator.rag_dirs["code"]
    _write_artifact(rag, chunk["document_id"], chunk)
    result = validator.index("code", qdrant_client=FakeQdrant([], count=0), collection="code")
    assert result["valid"] is False
    assert any("text hash mismatch" in error for error in result["errors"])
    assert any("Qdrant/local chunk count mismatch" in error for error in result["errors"])


def test_writing_index_validation_checks_paper_traceability(tmp_path: Path) -> None:
    validator, chunk, client = _writing_fixture(tmp_path)
    assert validator.index("writing", qdrant_client=client, collection="writing-current")["valid"]
    chunk["metadata"]["source_paper_id"] = "wrong-paper"
    _write_artifact(validator.rag_dirs["writing"], chunk["document_id"], chunk)
    result = validator.index("writing")
    assert result["valid"] is False
    assert any("source_paper_id mismatch" in error for error in result["errors"])


def test_writing_index_validation_accepts_frozen_v1_identity_metadata(tmp_path: Path) -> None:
    validator, chunk, client = _writing_fixture(tmp_path)
    del chunk["metadata"]["writing_id"]
    rag = validator.rag_dirs["writing"]
    _write_artifact(rag, chunk["document_id"], chunk)
    client.points[0].payload.pop("writing_id")
    with sqlite3.connect(rag / "state" / "index.sqlite3") as connection:
        connection.execute(
            "UPDATE documents SET metadata_hash=?",
            (sha256_json(chunk["metadata"]),),
        )
    assert validator.index("writing", qdrant_client=client, collection="writing-v1")["valid"]


def test_writing_material_release_validation_accepts_mixed_promoted_index(
    tmp_path: Path,
) -> None:
    validator, _release_path, client = _writing_material_fixture(tmp_path)
    result = validator.index(
        "writing",
        qdrant_client=client,
        collection="knowledgehub_writing_current",
    )
    assert result["valid"] is True
    assert result["index_schema"] == "writing-material-release-v1"
    assert result["checked"]["chunks"] == 1
    assert result["checked"]["release_points"] == 2
    assert result["qdrant"]["points"] == 2


def test_writing_material_release_validation_rejects_promotion_fingerprint_drift(
    tmp_path: Path,
) -> None:
    validator, _release_path, client = _writing_material_fixture(tmp_path)
    validator.active_releases["writing"]["artifact_fingerprint"] = "0" * 64
    result = validator.index(
        "writing",
        qdrant_client=client,
        collection="knowledgehub_writing_current",
    )
    assert result["valid"] is False
    assert "active writing-material fingerprint mismatch" in result["errors"]


def test_writing_material_release_validation_rejects_candidate_metadata_drift(
    tmp_path: Path,
) -> None:
    validator, _release_path, _client = _writing_material_fixture(tmp_path)
    rag = validator.rag_dirs["writing"]
    artifact = next((rag / "chunks").glob("*.jsonl"))
    chunk = json.loads(artifact.read_text(encoding="utf-8"))
    chunk["metadata"]["accepted_snapshot_only"] = False
    artifact.write_text(json.dumps(chunk) + "\n", encoding="utf-8")
    result = validator.index("writing")
    assert result["valid"] is False
    assert any("not accepted-snapshot-only" in error for error in result["errors"])


def test_writing_material_release_validation_rejects_qdrant_metadata_drift(
    tmp_path: Path,
) -> None:
    validator, _release_path, client = _writing_material_fixture(tmp_path)
    client.points[-1].payload["quality_score"] = 0.1
    result = validator.index(
        "writing",
        qdrant_client=client,
        collection="knowledgehub_writing_current",
    )
    assert result["valid"] is False
    assert any("writing-material metadata mismatch" in error for error in result["errors"])


def test_validate_index_cli_shape_and_offline_mode() -> None:
    args = build_parser().parse_args(["validate", "index", "code", "--offline"])
    assert args.target == "index"
    assert args.knowledge_base == "code"
    assert args.offline is True


def test_validate_dependencies_cli_shape() -> None:
    args = build_parser().parse_args(["validate", "dependencies", "--offline"])
    assert args.target == "dependencies"
    assert args.knowledge_base is None
