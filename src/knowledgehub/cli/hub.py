"""CLI groups for multi-knowledge-base operations."""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
from typing import Any

from knowledgehub.code_rag.build import CodeBuildService
from knowledgehub.code_rag.environment import EnvironmentCapture
from knowledgehub.code_rag.maintenance import OnDemandVersionImporter, ReleaseWatchService
from knowledgehub.code_rag.registry import CodeSourceRegistry
from knowledgehub.code_rag.sync import CodeSyncService
from knowledgehub.hub.config import HubConfig
from knowledgehub.hub.query import (
    HubQueryRequest,
    HubQueryService,
    build_code_query_plan,
)
from knowledgehub.writing_rag.derive import WritingDerivationService


def add_hub_parsers(subparsers: Any) -> None:
    source = subparsers.add_parser("source", help="Inspect configured Code knowledge sources")
    source_commands = source.add_subparsers(dest="hub_source_command", required=True)
    source_commands.add_parser("list")
    inspect = source_commands.add_parser("inspect")
    inspect.add_argument("library")

    environment = subparsers.add_parser("environment", help="Capture a sanitized environment")
    environment_commands = environment.add_subparsers(dest="environment_command", required=True)
    capture = environment_commands.add_parser("capture")
    capture.add_argument("--name", default="current")
    capture.add_argument("--project", type=Path)
    capture.add_argument("--dry-run", action="store_true")

    sync = subparsers.add_parser("sync", help="Synchronize configured knowledge sources")
    sync_commands = sync.add_subparsers(dest="sync_domain", required=True)
    sync_code = sync_commands.add_parser("code")
    sync_code.add_argument("--library", default="transformers")
    sync_code.add_argument("--version")
    sync_code.add_argument("--all", action="store_true")
    sync_code.add_argument("--dry-run", action="store_true")
    sync_releases = sync_commands.add_parser("releases")
    sync_releases.add_argument("--library", default="transformers")
    sync_releases.add_argument("--all", action="store_true")
    sync_releases.add_argument("--dry-run", action="store_true")
    sync_version = sync_commands.add_parser("version")
    sync_version.add_argument("--library", required=True)
    sync_version.add_argument("--version", required=True)
    sync_version.add_argument("--allow-download", action="store_true")
    sync_version.add_argument("--build-limit", type=int, default=20)
    sync_version.add_argument("--dry-run", action="store_true")

    build = subparsers.add_parser("build", help="Build a derived knowledge index")
    build_commands = build.add_subparsers(dest="build_domain", required=True)
    build_code = build_commands.add_parser("code")
    build_code.add_argument("--library", default="transformers")
    build_code.add_argument("--version")
    build_code.add_argument("--incremental", action="store_true")
    build_code.add_argument("--limit", type=int)
    build_code.add_argument(
        "--candidate-collection",
        help="Build into an explicit physical candidate before index stage/promote",
    )
    build_code.add_argument("--dry-run", action="store_true")
    build_code.add_argument("--prune", action="store_true")

    derive = subparsers.add_parser("derive", help="Derive knowledge from an existing base")
    derive_commands = derive.add_subparsers(dest="derive_domain", required=True)
    writing = derive_commands.add_parser("writing")
    writing.add_argument("--collection")
    writing.add_argument("--paper-id")
    writing.add_argument("--limit", type=int)
    writing.add_argument("--all", action="store_true")
    writing.add_argument("--dry-run", action="store_true")
    writing.add_argument("--prune", action="store_true")

    query = subparsers.add_parser("query", help="Query one logical knowledge base")
    query.add_argument("knowledge_base", choices=("literature", "code", "writing"))
    query.add_argument("query")
    query.add_argument("--intent")
    query.add_argument("--library")
    query.add_argument("--version")
    query.add_argument("--installed-version")
    query.add_argument("--target-version")
    query.add_argument("--source-type", action="append", dest="source_types")
    query.add_argument("--symbol")
    query.add_argument("--section")
    query.add_argument("--writing-function")
    query.add_argument("--research-domain")
    query.add_argument("--return-mode", choices=("pattern_first", "include_original"), default="pattern_first")
    query.add_argument("--mode", choices=("dense", "sparse", "hybrid"), default="hybrid")
    query.add_argument("--reranker", choices=("off", "light", "quality"), default="off")
    query.add_argument("--top-k", type=int, default=10)
    query.add_argument("--environment", default="current")
    query.add_argument("--explain-plan", action="store_true")
    query.add_argument("--allow-auto-import", action="store_true")
    query.add_argument("--allow-issues", action="store_true")


def run_hub_command(args: argparse.Namespace) -> int:
    try:
        config = HubConfig.load(args.hub_config or Path("configs/knowledgehub.yaml"))
        registry = CodeSourceRegistry.load(config.code.registry)
        if args.source == "source":
            if args.hub_source_command == "list":
                _emit(
                    {
                        "libraries": [
                            {
                                "name": item.name,
                                "package": item.package_name,
                                "repository": item.repository,
                                "enabled": item.enabled,
                                "installed_version": item.installed_version(),
                            }
                            for item in registry.list()
                        ]
                    }
                )
            else:
                item = registry.get(args.library)
                _emit(dataclasses.asdict(item) | {"installed_version": item.installed_version()})
            return 0
        if args.source == "environment":
            result = EnvironmentCapture(config.code.data_root).capture(
                name=args.name,
                project=args.project,
                packages=tuple(item.package_name for item in registry.list()),
                dry_run=args.dry_run,
            )
            _emit(result)
            return 0
        if args.source == "sync":
            if args.sync_domain == "releases":
                names = (
                    [item.name for item in registry.list(enabled_only=True)]
                    if args.all
                    else [args.library]
                )
                results = [
                    ReleaseWatchService(config, registry).check(name, dry_run=args.dry_run)
                    for name in names
                ]
                _emit({"results": results})
                return 0
            if args.sync_domain == "version":
                result = OnDemandVersionImporter(config, registry).import_version(
                    args.library,
                    args.version,
                    allowed=args.allow_download,
                    build_limit=args.build_limit,
                    dry_run=args.dry_run,
                )
                _emit(result)
                return 0 if result["status"] != "permission_required" else 2
            sync_service = CodeSyncService(
                registry,
                config.code.data_root,
                token_env=config.code.github_token_env,
                timeout_seconds=config.code.timeout_seconds,
                max_retries=config.code.max_retries,
            )
            names = [item.name for item in registry.list(enabled_only=True)] if args.all else [args.library]
            sync_results = [
                sync_service.sync(name, version=args.version, dry_run=args.dry_run)
                for name in names
            ]
            _emit({"results": sync_results})
            return 0 if all(
                item["status"] in {"success", "planned"} for item in sync_results
            ) else 1
        if args.source == "build":
            rag_config = config.rag_config("code")
            if args.candidate_collection:
                if args.candidate_collection == config.knowledge_bases["code"].collection:
                    raise ValueError("candidate collection must differ from the configured production collection")
                rag_config = rag_config.with_overrides(
                    qdrant_collection=args.candidate_collection
                )
            build_service = CodeBuildService(
                registry, config.code.data_root, rag_config
            )
            try:
                build_result = build_service.build(
                    args.library,
                    version=args.version,
                    limit=args.limit,
                    dry_run=args.dry_run,
                    prune=args.prune,
                )
            finally:
                build_service.close()
            _emit(build_result)
            return 0 if build_result["status"] == "success" else 1
        if args.source == "derive":
            writing_service = WritingDerivationService(
                literature_data_dir=config.writing.literature_data_dir,
                data_root=config.writing.data_root,
                rag_config=config.rag_config("writing"),
                processor_version=config.writing.processor_version,
                minimum_quality=config.writing.minimum_quality,
            )
            try:
                writing_result = writing_service.derive(
                    paper_id=args.paper_id,
                    collection=args.collection,
                    limit=None if args.all else (args.limit or config.writing.default_limit),
                    dry_run=args.dry_run,
                    prune=args.prune,
                )
            finally:
                writing_service.close()
            _emit(writing_result)
            return 0 if writing_result["status"] == "success" else 1
        if args.source == "query":
            filters = {
                key: value
                for key, value in {
                    "library": args.library,
                    "version": args.version,
                    "installed_version": args.installed_version,
                    "target_version": args.target_version,
                    "source_types": tuple(args.source_types or ()),
                    "symbol": args.symbol,
                    "section": args.section,
                    "writing_function": args.writing_function,
                    "research_domain": args.research_domain,
                }.items()
                if value not in (None, (), "")
            }
            plan = (
                build_code_query_plan(
                    args.query,
                    environment=args.environment,
                    library=args.library,
                    symbol=args.symbol,
                    allow_auto_import=args.allow_auto_import,
                    allow_issues=args.allow_issues,
                )
                if args.knowledge_base == "code"
                else None
            )
            if args.explain_plan:
                _emit({"plan": plan})
                return 0
            search_result = HubQueryService(config).search(
                HubQueryRequest(
                    knowledge_base=args.knowledge_base,
                    query=args.query,
                    intent=args.intent,
                    filters=filters,
                    top_k=args.top_k,
                    mode=args.mode,
                    return_mode=args.return_mode,
                    reranker=args.reranker,
                )
            )
            payload = dataclasses.asdict(search_result)
            if plan is not None:
                payload["query_plan"] = plan
            _emit(payload)
            return 0
        raise ValueError(f"unsupported KnowledgeHub command: {args.source}")
    except (ValueError, RuntimeError, OSError) as exc:
        _emit({"status": "failed", "error_code": "hub_error", "error": str(exc)})
        return 2 if isinstance(exc, ValueError) else 1


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
