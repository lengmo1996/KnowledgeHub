"""Unified RAG CLI; orchestration remains in pipeline services."""

from __future__ import annotations

import argparse
import dataclasses
import json
import time
from pathlib import Path
from typing import Any

from knowledgehub.core.atomic import atomic_write_json, atomic_write_text
from knowledgehub.core.hashing import sha256_json
from knowledgehub.core.locking import LockBusyError
from knowledgehub.pipeline.benchmarking import GPUMemoryMonitor
from knowledgehub.pipeline.config import RagConfig, RagConfigError
from knowledgehub.pipeline.doctor import inspect_environment
from knowledgehub.pipeline.orchestrator import PipelineOrchestrator
from knowledgehub.pipeline.validation import validate_pipeline


def add_rag_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("rag", help="Parse, index and query source manifests")
    parser.add_argument("--profile", type=Path, help="Optional RAG profile YAML")
    commands = parser.add_subparsers(dest="rag_command", required=True)
    doctor = commands.add_parser("doctor", help="Inspect the host without changing it")
    doctor.add_argument("--source", dest="rag_source", default="zotero")
    doctor.add_argument("--dry-run", action="store_true")
    _gpu(doctor)
    plan = commands.add_parser("plan", help="Show deterministic work selection")
    _selection(plan)
    _gpu(plan)
    ingest = commands.add_parser("ingest", help="Run full or incremental ingestion")
    modes = ingest.add_mutually_exclusive_group()
    modes.add_argument("--full", action="store_true")
    modes.add_argument("--incremental", action="store_true")
    modes.add_argument("--resume", action="store_true")
    modes.add_argument("--reconcile", action="store_true")
    _selection(ingest)
    _gpu(ingest)
    ingest.add_argument("--dry-run", action="store_true")
    ingest.add_argument("--force", action="store_true")
    ingest.add_argument("--prune", action="store_true")
    parse = commands.add_parser("parse", help="Parse/chunk without embedding")
    _selection(parse)
    _gpu(parse)
    parse.add_argument("--dry-run", action="store_true")
    parse.add_argument("--force", action="store_true")
    embed = commands.add_parser("embed", help="Embed and index ready chunk artifacts")
    embed.add_argument("--endpoints")
    _selection(embed)
    embed.add_argument("--dry-run", action="store_true")
    embed.add_argument("--force", action="store_true")
    _gpu(embed)
    query = commands.add_parser("query", help="Run hybrid or sparse retrieval")
    query.add_argument("query")
    query.add_argument("--source", dest="rag_source", default="zotero")
    query.add_argument("--mode", choices=("hybrid", "sparse"), default="hybrid")
    query.add_argument("--reranker", choices=("off", "light", "quality"), default="off")
    query.add_argument("--top-k", type=int, default=10)
    query.add_argument("--prefetch-limit", type=int, default=50)
    query.add_argument("--collection-key")
    query.add_argument("--tag")
    query.add_argument("--year-from", type=int)
    query.add_argument("--year-to", type=int)
    query.add_argument("--doi")
    query.add_argument("--document-id")
    validate = commands.add_parser("validate", help="Validate state and artifacts")
    validate.add_argument("--source", dest="rag_source", default="zotero")
    validate.add_argument("--qdrant", action="store_true")
    benchmark = commands.add_parser("benchmark", help="Run bounded parser/embedding benchmark")
    benchmark.add_argument("--source", dest="rag_source", default="zotero")
    benchmark.add_argument("--stage", choices=("parsing", "embedding", "online"), required=True)
    benchmark.add_argument("--compare")
    benchmark.add_argument("--limit", type=int, choices=(1, 20, 100), default=20)
    benchmark.add_argument(
        "--query",
        default="How can retrieval augmented generation improve research workflows?",
    )
    benchmark.add_argument("--dry-run", action="store_true")


def run_rag_command(args: argparse.Namespace) -> int:
    try:
        config = _config(args)
        command = args.rag_command
        if command == "doctor":
            _emit(inspect_environment(config))
            return 0
        if command == "validate":
            report = validate_pipeline(config, check_qdrant=args.qdrant)
            _emit(report.to_dict())
            return 0 if report.valid else 1
        if command == "query":
            return _query(config, args)
        if command == "benchmark":
            return _benchmark(config, args)
        read_only = command == "plan" or getattr(args, "dry_run", False)
        orchestrator = PipelineOrchestrator(config, initialize=not read_only)
        try:
            if command == "plan" or getattr(args, "dry_run", False):
                _emit(
                    orchestrator.plan(
                        limit=getattr(args, "limit", None),
                        document_id=getattr(args, "document_id", None),
                        attachment_key=getattr(args, "attachment_key", None),
                    )
                )
                return 0
            if command == "ingest":
                if args.full:
                    summary = orchestrator.ingest_full(
                        limit=args.limit,
                        document_id=args.document_id,
                        attachment_key=args.attachment_key,
                        force=args.force,
                        prune=args.prune,
                    )
                elif args.resume:
                    summary = orchestrator.resume(
                        limit=args.limit,
                        document_id=args.document_id,
                        attachment_key=args.attachment_key,
                    )
                elif args.reconcile:
                    if args.limit or args.document_id or args.attachment_key:
                        raise RagConfigError("reconcile requires the complete source snapshot")
                    summary = orchestrator.reconcile()
                else:
                    if args.limit or args.document_id or args.attachment_key:
                        raise RagConfigError(
                            "incremental ingest consumes whole catalog entries; "
                            "selection options require --full or --resume"
                        )
                    summary = orchestrator.ingest_incremental()
            elif command == "parse":
                summary = orchestrator.parse_pending(
                    limit=args.limit,
                    document_id=args.document_id,
                    attachment_key=args.attachment_key,
                    force=args.force,
                )
            elif command == "embed":
                summary = orchestrator.embed_pending(
                    limit=args.limit,
                    document_id=args.document_id,
                    attachment_key=args.attachment_key,
                    force=args.force,
                )
            else:
                raise RagConfigError(f"unsupported RAG command: {command}")
            _emit(summary.to_dict())
            return 0 if summary.status == "success" else 1
        finally:
            orchestrator.close()
    except LockBusyError as exc:
        _emit({"status": "failed", "error_code": "lock_busy", "error": str(exc)})
        return 3
    except (RagConfigError, ValueError) as exc:
        _emit({"status": "failed", "error_code": "config_error", "error": str(exc)})
        return 2
    except Exception as exc:
        _emit({"status": "failed", "error_code": "runtime_error", "error": str(exc)})
        return 1


def _config(args: argparse.Namespace) -> RagConfig:
    config_path = args.config or Path("configs/rag/default.yaml")
    overrides: dict[str, Any] = {}
    gpu_mode = getattr(args, "gpu_mode", None)
    gpu_ids = getattr(args, "gpu_ids", None)
    endpoints = getattr(args, "endpoints", None)
    if gpu_mode:
        overrides["gpu_mode"] = gpu_mode
    if gpu_ids:
        overrides["gpu_ids"] = gpu_ids
        overrides["parse_gpu_ids"] = gpu_ids
    if endpoints:
        overrides["embedding_endpoints"] = endpoints
    return RagConfig.load(
        config_path,
        profile_path=args.profile,
        overrides=overrides,
    )


def _query(config: RagConfig, args: argparse.Namespace) -> int:
    from knowledgehub.retrieval.models import SearchRequest
    from knowledgehub.services.search_api import build_retrieval

    config = config.with_overrides(reranker_profile=args.reranker)
    service = build_retrieval(config)
    try:
        response = service.search(
            SearchRequest(
                query=args.query,
                mode=args.mode,
                limit=args.top_k,
                prefetch_limit=args.prefetch_limit,
                collection_key=args.collection_key,
                tag=args.tag,
                year_from=args.year_from,
                year_to=args.year_to,
                doi=args.doi,
                document_id=args.document_id,
                use_reranker=args.reranker != "off",
                reranker_profile=args.reranker,
            )
        )
        _emit(dataclasses.asdict(response))
        return 0
    finally:
        service.endpoint_pool.close()
        if service.reranker:
            service.reranker.close()


def _benchmark(config: RagConfig, args: argparse.Namespace) -> int:
    default_compare = "embedding,hybrid,light,quality" if args.stage == "online" else "single,dual"
    modes = [
        value.strip() for value in (args.compare or default_compare).split(",") if value.strip()
    ]
    if args.dry_run:
        _emit({"stage": args.stage, "limit": args.limit, "modes": modes, "dry_run": True})
        return 0
    if args.stage == "online":
        return _online_benchmark(config, args.query, args.limit, modes)
    results: list[dict[str, Any]] = []
    for mode in modes:
        gpu_ids = (0, 1) if mode == "dual" else (0,)
        candidate = config.with_overrides(
            gpu_mode=mode,
            gpu_ids=gpu_ids,
            parse_gpu_ids=gpu_ids,
            qdrant_collection=config.qdrant_smoke_collection,
        )
        orchestrator = PipelineOrchestrator(candidate)
        started = time.monotonic()
        monitor = GPUMemoryMonitor()
        monitor.start()
        qdrant_snapshot: str | None = None
        try:
            if args.stage == "parsing":
                summary = orchestrator.ingest_full(limit=args.limit, force=True, parse_only=True)
            elif args.stage == "embedding":
                orchestrator.ingest_full(limit=args.limit, force=False, parse_only=True)
                summary = orchestrator.embed_pending(limit=args.limit, force=True)
                qdrant_snapshot = orchestrator.snapshot_index()
            else:
                raise RagConfigError(f"unsupported benchmark stage: {args.stage}")
            fingerprints = _pipeline_fingerprints(orchestrator)
        finally:
            gpu_metrics = monitor.stop()
            orchestrator.close()
        duration = round(time.monotonic() - started, 6)
        results.append(
            {
                "mode": mode,
                "duration_seconds": duration,
                "fingerprints_sha256": fingerprints,
                "gpu": gpu_metrics,
                "metrics": _benchmark_metrics(summary.to_dict(), duration, args.stage),
                "qdrant_snapshot": qdrant_snapshot,
                "summary": summary.to_dict(),
            }
        )
    digests = {str(value["fingerprints_sha256"]) for value in results}
    output = {
        "stage": args.stage,
        "limit": args.limit,
        "fingerprints_consistent": len(digests) <= 1,
        "results": results,
    }
    destination = config.data_dir / "build" / "benchmarks" / f"{int(time.time())}-{args.stage}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(destination, output)
    markdown = destination.with_suffix(".md")
    lines = [
        f"# {args.stage.title()} benchmark",
        "",
        f"- Limit: {args.limit}",
        f"- Modes: {', '.join(modes)}",
        "",
        "| Mode | Seconds | Status | Parsed | Embedded | Pages/s | Texts/s | Tokens/s | P95 | Failures |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for value in results:
        summary_value = value["summary"]
        metrics = value["metrics"]
        lines.append(
            f"| {value['mode']} | {value['duration_seconds']} | "
            f"{summary_value['status']} | {summary_value['parsed']} | "
            f"{summary_value['embedded']} | {metrics['pages_per_second']} | "
            f"{metrics['texts_per_second']} | {metrics['tokens_per_second']} | "
            f"{metrics['p95_seconds']} | {metrics['failures']} |"
        )
    atomic_write_text(markdown, "\n".join(lines) + "\n")
    _emit({**output, "output": str(destination), "markdown": str(markdown)})
    return 0


def _online_benchmark(config: RagConfig, query: str, limit: int, modes: list[str]) -> int:
    from knowledgehub.retrieval.models import SearchRequest
    from knowledgehub.services.search_api import build_retrieval

    results: list[dict[str, Any]] = []
    for mode in modes:
        profile = mode if mode in {"light", "quality"} else "off"
        candidate = config.with_overrides(reranker_profile=profile)
        service = build_retrieval(candidate)
        started = time.monotonic()
        monitor = GPUMemoryMonitor()
        monitor.start()
        try:
            if mode == "embedding":
                prompt = f"Instruct: {candidate.embedding_query_instruction}\nQuery: {query}"
                embedding = service.endpoint_pool.embed([prompt])
                payload: dict[str, Any] = {
                    "endpoint": embedding.endpoint,
                    "raw_dimension": embedding.raw_dimension,
                    "final_dimension": embedding.final_dimension,
                }
            else:
                response = service.search(
                    SearchRequest(
                        query=query,
                        mode="hybrid",
                        limit=min(limit, 100),
                        prefetch_limit=max(50, min(limit, 100)),
                        use_reranker=profile != "off",
                        reranker_profile=profile,
                    )
                )
                payload = dataclasses.asdict(response)
        finally:
            gpu_metrics = monitor.stop()
            service.endpoint_pool.close()
            if service.reranker:
                service.reranker.close()
        results.append(
            {
                "mode": mode,
                "duration_seconds": round(time.monotonic() - started, 6),
                "gpu": gpu_metrics,
                "result": payload,
            }
        )
    output = {"stage": "online", "limit": limit, "query": query, "results": results}
    destination = config.data_dir / "build" / "benchmarks" / f"{int(time.time())}-online.json"
    atomic_write_json(destination, output)
    markdown = destination.with_suffix(".md")
    lines = [
        "# Online benchmark",
        "",
        f"- Query: {query}",
        f"- Limit: {limit}",
        "",
        "| Mode | Seconds | Result count | Endpoint |",
        "| --- | ---: | ---: | --- |",
    ]
    for value in results:
        result = value["result"]
        hits = result.get("hits") if isinstance(result, dict) else None
        endpoint = result.get("endpoint", "") if isinstance(result, dict) else ""
        lines.append(
            f"| {value['mode']} | {value['duration_seconds']} | "
            f"{len(hits) if isinstance(hits, list) else 0} | {endpoint} |"
        )
    atomic_write_text(markdown, "\n".join(lines) + "\n")
    _emit({**output, "output": str(destination), "markdown": str(markdown)})
    return 0


def _selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", dest="rag_source", default="zotero")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--document-id")
    parser.add_argument("--attachment-key")


def _pipeline_fingerprints(orchestrator: PipelineOrchestrator) -> str:
    rows = orchestrator.state.documents(active_only=True)
    return sha256_json(
        {
            document_id: {
                "chunk": row.get("chunk_fingerprint"),
                "embedding": row.get("embedding_fingerprint"),
                "parse": row.get("parse_fingerprint"),
            }
            for document_id, row in sorted(rows.items())
        }
    )


def _benchmark_metrics(
    summary: dict[str, Any], duration_seconds: float, stage: str
) -> dict[str, Any]:
    duration = max(duration_seconds, 1e-9)
    pages = sum(int(value) for value in summary.get("per_gpu_pages", {}).values())
    endpoint_stats = summary.get("endpoint_stats", {})
    texts = sum(int(value.get("texts", 0)) for value in endpoint_stats.values())
    endpoint_p95 = max(
        (float(value.get("p95_latency_seconds", 0.0)) for value in endpoint_stats.values()),
        default=0.0,
    )
    tokens = int(
        summary.get("embedded_tokens", 0)
        if stage == "embedding"
        else summary.get("chunk_tokens", 0)
    )
    return {
        "documents_per_second": round(
            int(summary.get("embedded" if stage == "embedding" else "parsed", 0)) / duration,
            6,
        ),
        "failures": len(summary.get("errors", [])),
        "pages_per_second": round(pages / duration, 6),
        "p95_seconds": round(
            endpoint_p95 if stage == "embedding" else float(summary.get("parse_p95_seconds", 0)),
            6,
        ),
        "texts_per_second": round(texts / duration, 6),
        "tokens_per_second": round(tokens / duration, 6),
    }


def _gpu(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--gpu-mode", choices=("auto", "dual", "single", "cpu"))
    parser.add_argument("--gpu-ids")


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
