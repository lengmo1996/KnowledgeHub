"""Deterministic task-level data parallel parser workers."""

from __future__ import annotations

import hashlib
import logging
import multiprocessing as mp
import os
import queue
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from knowledgehub.chunking.fingerprints import document_chunk_fingerprint
from knowledgehub.chunking.structural import StructuralChunker
from knowledgehub.pipeline.artifacts import write_chunks_parquet, write_parsed
from knowledgehub.pipeline.config import GPUPlan, RagConfig
from knowledgehub.pipeline.models import ChunkRecord, SourceDocument

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ParseWorkerResult:
    document_id: str
    worker_id: str
    gpu_id: int | None
    status: str
    parser_name: str | None
    parser_version: str | None
    parse_fingerprint: str | None
    chunk_fingerprint: str | None
    chunks: tuple[ChunkRecord, ...]
    page_count: int
    duration_seconds: float
    error: str | None = None


def stable_partition(document_id: str, workers: int) -> int:
    if workers <= 0:
        raise ValueError("workers must be positive")
    digest = hashlib.sha256(document_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % workers


def partition_documents(
    documents: Iterable[SourceDocument], workers: int
) -> tuple[tuple[SourceDocument, ...], ...]:
    partitions: list[list[SourceDocument]] = [[] for _ in range(workers)]
    for document in sorted(documents, key=lambda value: value.document_id):
        partitions[stable_partition(document.document_id, workers)].append(document)
    return tuple(tuple(values) for values in partitions)


def run_parse_workers(
    config: RagConfig,
    plan: GPUPlan,
    documents: Sequence[SourceDocument],
) -> list[ParseWorkerResult]:
    if not documents:
        return []
    worker_count = plan.parser_workers
    partitions = partition_documents(documents, worker_count)
    context = mp.get_context("spawn")
    output: Any = context.Queue()
    processes: list[Any] = []
    for partition_id, tasks in enumerate(partitions):
        gpu_id = plan.gpu_ids[partition_id] if plan.gpu_ids else None
        process = context.Process(
            target=_worker_main,
            args=(config, partition_id, gpu_id, tasks, output),
            name=f"knowledgehub-parser-{partition_id}",
        )
        process.start()
        processes.append(process)

    expected = len(documents)
    results: list[ParseWorkerResult] = []
    while len(results) < expected and any(process.is_alive() for process in processes):
        try:
            payload = output.get(timeout=0.5)
        except queue.Empty:
            continue
        results.append(_result_from_mapping(payload))
    for process in processes:
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    while len(results) < expected:
        try:
            results.append(_result_from_mapping(output.get_nowait()))
        except queue.Empty:
            break
    completed = {value.document_id for value in results}
    for document in documents:
        if document.document_id not in completed:
            results.append(
                ParseWorkerResult(
                    document_id=document.document_id,
                    worker_id="coordinator",
                    gpu_id=None,
                    status="failed",
                    parser_name=None,
                    parser_version=None,
                    parse_fingerprint=None,
                    chunk_fingerprint=None,
                    chunks=(),
                    page_count=0,
                    duration_seconds=0.0,
                    error="parser worker exited without returning a result",
                )
            )
    return sorted(results, key=lambda value: value.document_id)


def _worker_main(
    config: RagConfig,
    partition_id: int,
    gpu_id: int | None,
    documents: Sequence[SourceDocument],
    output: Any,
) -> None:
    if gpu_id is None:
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
        device = "cpu"
    else:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
        device = "cuda"
    os.environ["OMP_NUM_THREADS"] = str(config.parse_cpu_threads_per_worker)
    # CUDA-aware libraries are imported only after visibility is fixed above.
    from knowledgehub.parsing.base import create_parser

    primary = create_parser(
        config.parser_name,
        device=device,
        ocr=config.ocr_enabled,
        num_threads=config.parse_cpu_threads_per_worker,
    )
    fallback = None
    chunker = StructuralChunker(config)
    worker_id = f"parser-{partition_id}"
    for document in documents:
        started = time.monotonic()
        try:
            parser = primary
            try:
                parsed = parser.parse(document)
            except Exception as exc:
                if not config.parser_fallback:
                    raise
                _LOG.warning(
                    "Primary parser %s rejected document %s (attachment %s); "
                    "retrying the entire document with %s: %s",
                    primary.name,
                    document.document_id,
                    document.attachment_key,
                    config.parser_fallback,
                    exc,
                )
                if fallback is None:
                    fallback = create_parser(
                        config.parser_fallback,
                        device="cpu",
                        ocr=False,
                        num_threads=config.parse_cpu_threads_per_worker,
                    )
                parser = fallback
                parsed = parser.parse(document)
            chunks = chunker.chunk(document, parsed)
            write_parsed(config.data_dir, parsed)
            write_chunks_parquet(config.data_dir, document.document_id, chunks)
            chunk_fingerprint = document_chunk_fingerprint(config, parsed.parse_fingerprint)
            output.put(
                _result_mapping(
                    ParseWorkerResult(
                        document_id=document.document_id,
                        worker_id=worker_id,
                        gpu_id=gpu_id,
                        status="success",
                        parser_name=parser.name,
                        parser_version=parser.version,
                        parse_fingerprint=parsed.parse_fingerprint,
                        chunk_fingerprint=chunk_fingerprint,
                        chunks=tuple(chunks),
                        page_count=parsed.page_count,
                        duration_seconds=round(time.monotonic() - started, 6),
                    )
                )
            )
        except Exception as exc:
            output.put(
                _result_mapping(
                    ParseWorkerResult(
                        document_id=document.document_id,
                        worker_id=worker_id,
                        gpu_id=gpu_id,
                        status="failed",
                        parser_name=None,
                        parser_version=None,
                        parse_fingerprint=None,
                        chunk_fingerprint=None,
                        chunks=(),
                        page_count=0,
                        duration_seconds=round(time.monotonic() - started, 6),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            )


def _result_mapping(value: ParseWorkerResult) -> dict[str, Any]:
    return {
        "document_id": value.document_id,
        "worker_id": value.worker_id,
        "gpu_id": value.gpu_id,
        "status": value.status,
        "parser_name": value.parser_name,
        "parser_version": value.parser_version,
        "parse_fingerprint": value.parse_fingerprint,
        "chunk_fingerprint": value.chunk_fingerprint,
        "chunks": [chunk.to_dict() for chunk in value.chunks],
        "page_count": value.page_count,
        "duration_seconds": value.duration_seconds,
        "error": value.error,
    }


def _result_from_mapping(value: Mapping[str, Any]) -> ParseWorkerResult:
    chunks = tuple(ChunkRecord(**dict(item)) for item in value.get("chunks") or [])
    return ParseWorkerResult(
        document_id=str(value["document_id"]),
        worker_id=str(value["worker_id"]),
        gpu_id=int(value["gpu_id"]) if value.get("gpu_id") is not None else None,
        status=str(value["status"]),
        parser_name=str(value["parser_name"]) if value.get("parser_name") else None,
        parser_version=str(value["parser_version"]) if value.get("parser_version") else None,
        parse_fingerprint=(
            str(value["parse_fingerprint"]) if value.get("parse_fingerprint") else None
        ),
        chunk_fingerprint=(
            str(value["chunk_fingerprint"]) if value.get("chunk_fingerprint") else None
        ),
        chunks=chunks,
        page_count=int(value.get("page_count") or 0),
        duration_seconds=float(value.get("duration_seconds") or 0.0),
        error=str(value["error"]) if value.get("error") else None,
    )
