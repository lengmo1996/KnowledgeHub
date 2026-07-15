from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest

from knowledgehub.chunking.fingerprints import (
    document_chunk_fingerprint,
    document_parse_fingerprint,
)
from knowledgehub.core.hashing import sha256_text
from knowledgehub.embeddings.models import EmbeddingBatchResult
from knowledgehub.pipeline.artifacts import write_chunks_parquet, write_parsed
from knowledgehub.pipeline.config import RagConfig
from knowledgehub.pipeline.models import ChunkRecord, ParsedDocument
from knowledgehub.pipeline.orchestrator import PipelineOrchestrator, RuntimeComponents
from knowledgehub.pipeline.workers import ParseWorkerResult


def _snapshot(pdf: Path, *, metadata: str = "m1", content: str = "c1") -> dict[str, Any]:
    pdf_hash = sha256_text(pdf.read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "source": "zotero",
        "document_id": "zotero:user:1:item:ATT:0",
        "attachment_key": "ATT",
        "document_fingerprint": f"doc-{metadata}-{content}",
        "metadata_fingerprint": metadata,
        "content_fingerprint": content,
        "status": "ready",
        "title": "A paper",
        "doi": "10.1/example",
        "tags": ["rag"],
        "collections": [{"key": "C", "path": "Research"}],
        "attachment": {"pdf_path": str(pdf), "pdf_sha256": pdf_hash},
    }


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, config, plan, documents):
        self.calls.append([value.document_id for value in documents])
        results = []
        for document in documents:
            parse = document_parse_fingerprint(
                document,
                parser_name="docling",
                parser_version=version("docling"),
                ocr=False,
            )
            chunk_fingerprint = document_chunk_fingerprint(config, parse)
            chunk = ChunkRecord(
                chunk_id="3dcfd67a-fcad-5b62-a27d-24e871b1c533",
                document_id=document.document_id,
                attachment_key=document.attachment_key,
                chunk_index=0,
                text="retrieval augmented generation",
                text_sha256=sha256_text("retrieval augmented generation"),
                chunk_fingerprint=sha256_text(document.source_content_fingerprint),
                token_count=3,
                metadata={"source": "zotero"},
            )
            write_parsed(
                config.data_dir,
                ParsedDocument(
                    document_id=document.document_id,
                    parser_name="docling",
                    parser_version=version("docling"),
                    parse_fingerprint=parse,
                    markdown="retrieval augmented generation",
                    structured={"pages": []},
                    page_count=1,
                ),
            )
            write_chunks_parquet(config.data_dir, document.document_id, [chunk])
            results.append(
                ParseWorkerResult(
                    document_id=document.document_id,
                    worker_id="fake-0",
                    gpu_id=0,
                    status="success",
                    parser_name="docling",
                    parser_version=version("docling"),
                    parse_fingerprint=parse,
                    chunk_fingerprint=chunk_fingerprint,
                    chunks=(chunk,),
                    page_count=1,
                    duration_seconds=0.01,
                )
            )
        return results


class FakePool:
    def __init__(self) -> None:
        self.texts = 0

    def embed(self, texts):
        self.texts += len(texts)
        return EmbeddingBatchResult(
            vectors=tuple((1.0, 0.0) for _ in texts),
            endpoint="fake",
            raw_dimension=2,
            final_dimension=2,
            text_count=len(texts),
            latency_seconds=0.01,
        )

    def stats(self):
        return {"fake": {"texts": self.texts}}


class FakeSparse:
    def encode(self, texts):
        return [([1], [1.0]) for _ in texts]


class FakeIndex:
    def __init__(self) -> None:
        self.replaced: list[str] = []
        self.deleted: list[str] = []
        self.payloads: list[str] = []

    def ensure_collection(self) -> None:
        return None

    def replace_document(self, document_id, chunks, dense, sparse):
        self.replaced.append(document_id)

    def delete_document(self, document_id: str) -> None:
        self.deleted.append(document_id)

    def update_payload(self, document_id, payload):
        self.payloads.append(document_id)


def _harness(tmp_path: Path):
    source = tmp_path / "source" / "manifests"
    source.mkdir(parents=True)
    pdf = tmp_path / "paper.pdf"
    pdf.write_text("pdf-v1", encoding="utf-8")
    snapshot = source / "documents.jsonl"
    snapshot.write_text(json.dumps(_snapshot(pdf)) + "\n", encoding="utf-8")
    (source / "delta-catalog.jsonl").write_text("", encoding="utf-8")
    config = RagConfig(
        source_snapshot_path=snapshot,
        source_delta_catalog_path=source / "delta-catalog.jsonl",
        data_dir=tmp_path / "rag",
        model_cache_dir=tmp_path / "models",
        gpu_mode="cpu",
        embedding_dim=2,
    )
    runner, pool, index = FakeRunner(), FakePool(), FakeIndex()
    orchestrator = PipelineOrchestrator(
        config,
        components=RuntimeComponents(
            parser_runner=runner,
            endpoint_pool=pool,
            sparse_encoder=FakeSparse(),
            index=index,
        ),
        gpu_devices=(),
    )
    return orchestrator, runner, pool, index, snapshot, pdf


def test_full_ingest_is_idempotent_and_metadata_only_updates_payload(tmp_path: Path) -> None:
    orchestrator, runner, pool, index, snapshot, pdf = _harness(tmp_path)
    first = orchestrator.ingest_full()
    assert first.status == "success"
    assert first.parsed == 1 and first.indexed == 1
    second = orchestrator.ingest_full()
    assert second.status == "success"
    assert second.parsed == 0 and second.indexed == 0
    assert runner.calls == [["zotero:user:1:item:ATT:0"], []]
    snapshot.write_text(json.dumps(_snapshot(pdf, metadata="m2")) + "\n", encoding="utf-8")
    third = orchestrator.ingest_full()
    assert third.parsed == 0 and third.payload_updated == 1
    assert index.payloads == ["zotero:user:1:item:ATT:0"]
    assert pool.texts == 1


def test_parse_command_selects_ready_snapshot_without_prior_ingest(tmp_path: Path) -> None:
    orchestrator, runner, _pool, _index, _snapshot_path, _pdf = _harness(tmp_path)
    assert orchestrator.state.documents() == {}
    summary = orchestrator.parse_pending(limit=20)
    orchestrator.close()
    assert summary.status == "success"
    assert summary.selected == 1 and summary.parsed == 1
    assert runner.calls == [["zotero:user:1:item:ATT:0"]]


def test_content_change_invalidates_and_prune_deletes(tmp_path: Path) -> None:
    orchestrator, _runner, _, index, snapshot, pdf = _harness(tmp_path)
    orchestrator.ingest_full()
    pdf.write_text("pdf-v2", encoding="utf-8")
    snapshot.write_text(json.dumps(_snapshot(pdf, content="c2")) + "\n", encoding="utf-8")
    changed = orchestrator.ingest_full()
    assert changed.parsed == 1 and len(index.replaced) == 2
    snapshot.write_text("", encoding="utf-8")
    pruned = orchestrator.ingest_full(prune=True)
    assert pruned.deleted >= 1
    assert "zotero:user:1:item:ATT:0" in index.deleted


def test_chunker_change_rechunks_without_reparsing(tmp_path: Path) -> None:
    orchestrator, runner, pool, index, snapshot, _pdf = _harness(tmp_path)
    orchestrator.ingest_full()
    changed = orchestrator.config.with_overrides(chunk_max_tokens=512)
    orchestrator.close()
    second = PipelineOrchestrator(
        changed,
        components=RuntimeComponents(
            parser_runner=runner,
            endpoint_pool=pool,
            sparse_encoder=FakeSparse(),
            index=index,
        ),
        gpu_devices=(),
    )
    summary = second.ingest_full()
    second.close()
    assert summary.status == "success"
    assert summary.parsed == 0 and summary.indexed == 1
    assert runner.calls == [["zotero:user:1:item:ATT:0"], []]
    assert pool.texts == 2
    assert snapshot.is_file()


def test_abandoned_parse_claim_is_recovered_on_resume(tmp_path: Path) -> None:
    orchestrator, runner, pool, index, _snapshot_path, _pdf = _harness(tmp_path)
    config = orchestrator.config
    orchestrator.close()

    def crash(config, plan, documents):
        raise RuntimeError("worker process disappeared")

    failing = PipelineOrchestrator(
        config,
        components=RuntimeComponents(
            parser_runner=crash,
            endpoint_pool=pool,
            sparse_encoder=FakeSparse(),
            index=index,
        ),
        gpu_devices=(),
    )
    with pytest.raises(RuntimeError, match="worker process disappeared"):
        failing.ingest_full()
    abandoned = failing.state.work_items("parse")
    failing.close()
    assert abandoned[0]["status"] == "running"
    assert abandoned[0]["attempts"] == 1

    resumed = PipelineOrchestrator(
        config,
        components=RuntimeComponents(
            parser_runner=runner,
            endpoint_pool=pool,
            sparse_encoder=FakeSparse(),
            index=index,
        ),
        gpu_devices=(),
    )
    summary = resumed.resume()
    work = resumed.state.work_items("parse")
    resumed.close()
    assert summary.status == "success"
    assert work[0]["status"] == "success"
    assert work[0]["attempts"] == 2
