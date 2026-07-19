from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledgehub.cli.writing_material import _run_release_command
from knowledgehub.pipeline.config import RagConfig
from knowledgehub.writing_rag.extract import WritingMaterialExtractionService
from knowledgehub.writing_rag.release import QdrantReleaseBackend, WritingMaterialReleaseService
from knowledgehub.writing_rag.review import WritingMaterialReviewService

from .helpers import based_on, build_literature_fixture
from .test_extract_review import FakeAnalyzer, _config, _selection


class FakeReleaseBackend:
    def __init__(self) -> None:
        self.collections = {
            "active-writing": {
                "exists": True,
                "status": "green",
                "points": 134,
                "schema": {"dense": 1024, "sparse": "bm25"},
            }
        }
        self.snapshot_calls = 0

    def inspect(self, collection):
        return self.collections.get(collection, {"exists": False})

    def snapshot(self, collection):
        self.snapshot_calls += 1
        return {"snapshot_id": "snapshot-1", "collection": collection}

    def restore(self, snapshot, target_collection):
        assert snapshot["collection"] == "active-writing"
        self.collections[target_collection] = dict(self.collections["active-writing"])


class FakePromotion:
    def __init__(self) -> None:
        self.last_release = None

    def stage(self, knowledge_base, candidate, *, verified_release):
        assert knowledge_base == "writing"
        assert verified_release["validation"]["valid"] is True
        self.last_release = verified_release
        return {"status": "staged", "candidate": candidate}

    def promote(self, knowledge_base, fallback, *, confirmed=False):
        if not confirmed:
            raise ValueError("confirmation required")
        return {"status": "active", "previous": fallback}

    def rollback(self, knowledge_base, *, confirmed=False):
        if not confirmed:
            raise ValueError("confirmation required")
        return {"status": "rolled_back"}


class FakeQdrantClient:
    def __init__(self) -> None:
        self.collections = {
            "active-writing": SimpleNamespace(
                status=SimpleNamespace(value="green"),
                points_count=134,
                config=SimpleNamespace(
                    params=SimpleNamespace(
                        vectors={"dense": {"size": 1024, "distance": "Cosine"}},
                        sparse_vectors={"sparse": {}},
                    )
                ),
            )
        }
        self.recoveries: list[tuple[str, str, str | None, bool]] = []
        self.closed = False

    def collection_exists(self, collection: str) -> bool:
        return collection in self.collections

    def get_collection(self, collection: str):
        return self.collections[collection]

    def create_snapshot(self, collection: str, *, wait: bool):
        assert collection == "active-writing"
        assert wait is True
        return SimpleNamespace(name="snapshot-1.snapshot", checksum="sha256:fixture")

    def recover_snapshot(
        self,
        collection: str,
        *,
        location: str,
        checksum: str | None,
        wait: bool,
    ) -> bool:
        self.recoveries.append((collection, location, checksum, wait))
        self.collections[collection] = self.collections["active-writing"]
        return True

    def close(self) -> None:
        self.closed = True


def _reviewed_run(tmp_path):
    literature = build_literature_fixture(tmp_path / "literature")
    config = _config(tmp_path, literature)
    result = WritingMaterialExtractionService(config, analyzer=FakeAnalyzer()).extract(
        selection=_selection(tmp_path)
    )
    review = WritingMaterialReviewService(config.data_root, literature)
    records = review._records(review.run_dir(str(result["run_id"])))
    decisions = []
    fields = {
        "evidence": "evidence_id",
        "strategy": "strategy_id",
        "template": "template_id",
        "phrase": "phrase_id",
    }
    for asset_type, values in records.items():
        for value in values:
            decisions.append(
                {
                    "asset_id": value[fields[asset_type]],
                    "decision": "accepted",
                    "based_on_hash": based_on(value),
                    "reviewer": "release-reviewer",
                    "reason": "fixture verified",
                }
            )
    path = tmp_path / "release-decisions.jsonl"
    path.write_text("".join(json.dumps(value) + "\n" for value in decisions), encoding="utf-8")
    review.apply(str(result["run_id"]), path)
    return review, str(result["run_id"])


def test_release_clone_merge_validates_counts_and_never_promotes_during_build(tmp_path) -> None:
    review, run_id = _reviewed_run(tmp_path)
    backend = FakeReleaseBackend()
    promotion = FakePromotion()
    service = WritingMaterialReleaseService(
        review, backend, tmp_path / "releases", promotion=promotion
    )

    planned = service.build(
        run_id,
        active_collection="active-writing",
        candidate_collection="candidate-writing",
        candidate_data_dir=tmp_path / "candidate-data",
        merge=lambda _collection: pytest.fail("dry-run must not merge"),
        dry_run=True,
    )
    assert planned["status"] == "planned"
    assert planned["expected_candidate_points"] == 137
    assert planned["candidate_data_dir"] == str((tmp_path / "candidate-data").resolve())
    assert backend.snapshot_calls == 0

    def merge(collection):
        backend.collections[collection]["points"] += 3
        return {"status": "success", "indexed": 3, "failures": []}

    built = service.build(
        run_id,
        active_collection="active-writing",
        candidate_collection="candidate-writing",
        candidate_data_dir=tmp_path / "candidate-data",
        merge=merge,
    )
    assert built["status"] == "validated"
    assert built["promotion_performed"] is False
    assert built["candidate_validation"]["points"] == 137
    assert backend.collections["active-writing"]["points"] == 134
    with pytest.raises(ValueError, match="explicit confirmation"):
        service.stage(Path(built["manifest_path"]))
    staged = service.stage(Path(built["manifest_path"]), confirmed=True)
    assert staged["status"] == "staged"
    assert promotion.last_release["rag_data_dir"] == str(
        (tmp_path / "candidate-data").resolve()
    )
    with pytest.raises(ValueError, match="confirmation"):
        service.promote("active-writing")
    assert service.promote("active-writing", confirmed=True)["status"] == "active"
    assert service.rollback(confirmed=True)["status"] == "rolled_back"


def test_release_rejects_merge_count_or_schema_drift(tmp_path) -> None:
    review, run_id = _reviewed_run(tmp_path)
    backend = FakeReleaseBackend()
    service = WritingMaterialReleaseService(review, backend, tmp_path / "releases")

    def bad_merge(collection):
        backend.collections[collection]["points"] += 2
        return {"status": "success", "indexed": 2, "failures": []}

    with pytest.raises(RuntimeError, match="accepted snapshot"):
        service.build(
            run_id,
            active_collection="active-writing",
            candidate_collection="bad-candidate",
            merge=bad_merge,
        )


def test_qdrant_release_backend_inspects_snapshots_and_restores_clone() -> None:
    client = FakeQdrantClient()
    backend = QdrantReleaseBackend(client)

    active = backend.inspect("active-writing")
    assert active == {
        "exists": True,
        "status": "green",
        "points": 134,
        "schema": {
            "vectors": {"dense": {"size": 1024, "distance": "Cosine"}},
            "sparse_vectors": {"sparse": {}},
        },
    }
    assert backend.inspect("candidate-writing") == {"exists": False}
    snapshot = backend.snapshot("active-writing")
    backend.restore(snapshot, "candidate-writing")
    assert client.recoveries == [
        (
            "candidate-writing",
            "file:///qdrant/snapshots/active-writing/snapshot-1.snapshot",
            "sha256:fixture",
            True,
        )
    ]
    assert backend.inspect("candidate-writing")["points"] == 134
    with pytest.raises(ValueError, match="already exists"):
        backend.restore(snapshot, "candidate-writing")


def test_release_rejects_unsafe_collection_before_backend_access(tmp_path) -> None:
    review, run_id = _reviewed_run(tmp_path)
    backend = FakeReleaseBackend()
    service = WritingMaterialReleaseService(review, backend, tmp_path / "releases")

    with pytest.raises(ValueError, match="unsafe"):
        service.build(
            run_id,
            active_collection="active-writing",
            candidate_collection="../candidate",
            merge=lambda _collection: pytest.fail("unsafe collection must not merge"),
        )
    assert backend.snapshot_calls == 0


def test_release_cli_dry_run_is_read_only(tmp_path, monkeypatch) -> None:
    review, run_id = _reviewed_run(tmp_path)
    client = FakeQdrantClient()
    monkeypatch.setattr("qdrant_client.QdrantClient", lambda **_kwargs: client)
    index_root = tmp_path / "indexes"
    alias_dir = index_root / "writing" / "aliases"
    alias_dir.mkdir(parents=True)
    (alias_dir / "current.json").write_text(
        json.dumps(
            {
                "status": "active",
                "alias": "knowledgehub_writing_current",
                "active_collection": "active-writing",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KH_INDEX_ROOT", str(index_root))
    rag = RagConfig(
        data_dir=tmp_path / "active-rag",
        qdrant_collection="knowledgehub_writing_current",
        qdrant_url="http://fixture.invalid",
    )
    config = SimpleNamespace(
        writing_materials=SimpleNamespace(data_root=tmp_path / "materials"),
        knowledge_bases={"writing": SimpleNamespace(collection="active-writing")},
        rag_config=lambda _knowledge_base: rag,
    )
    result = _run_release_command(
        Namespace(
            writing_material_release_command="build",
            run_id=run_id,
            candidate_collection="candidate-writing",
            dry_run=True,
        ),
        config,
        review,
    )

    assert result["status"] == "planned"
    assert result["active_collection"] == "active-writing"
    assert result["promotion_performed"] is False
    assert client.recoveries == []
    assert client.closed is True
    assert not (tmp_path / "materials" / "releases").exists()
    assert not (tmp_path / "materials" / "release-candidates").exists()


def test_release_rejects_stable_alias_as_active_snapshot_source(tmp_path) -> None:
    review, run_id = _reviewed_run(tmp_path)
    backend = FakeReleaseBackend()
    service = WritingMaterialReleaseService(review, backend, tmp_path / "releases")

    with pytest.raises(ValueError, match="physical names"):
        service.build(
            run_id,
            active_collection="knowledgehub_writing_current",
            candidate_collection="candidate-writing",
            merge=lambda _collection: pytest.fail("stable alias must not be snapshotted"),
        )
    assert backend.snapshot_calls == 0
