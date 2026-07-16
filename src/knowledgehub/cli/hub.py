"""CLI groups for multi-knowledge-base operations."""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
from typing import Any, Mapping

from knowledgehub.code_rag.build import CodeBuildService
from knowledgehub.code_rag.dependencies import DependencyManifestService
from knowledgehub.code_rag.diffs import VersionDiffBuildService
from knowledgehub.code_rag.environment import EnvironmentCapture
from knowledgehub.code_rag.maintenance import OnDemandVersionImporter, ReleaseWatchService
from knowledgehub.code_rag.registry import CodeSourceRegistry
from knowledgehub.code_rag.sync import CodeSyncService
from knowledgehub.governance.maintenance import SyncPlanner
from knowledgehub.governance.tasks import TaskExecutor, TaskStore, default_task_store_path
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
    dependencies = source_commands.add_parser("dependencies")
    dependencies.add_argument("library")
    dependencies.add_argument("--version", required=True)
    dependencies.add_argument("--dry-run", action="store_true")

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
    sync_plan = sync_commands.add_parser("plan")
    sync_plan.add_argument(
        "--trigger",
        choices=("manual", "periodic", "release", "config_change", "on_demand"),
        required=True,
    )
    sync_plan.add_argument("--library", action="append", dest="libraries", default=[])
    sync_plan.add_argument("--version")
    sync_plan.add_argument("--interval-hours", type=int)
    sync_plan.add_argument("--output", type=Path)

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
    build_diff = build_commands.add_parser("diff")
    build_diff.add_argument("--library", required=True)
    build_diff.add_argument("--from-version", required=True)
    build_diff.add_argument("--to-version", required=True)
    build_diff.add_argument("--symbol", action="append", dest="symbols", default=[])
    build_diff.add_argument("--limit", type=int, default=20)
    build_diff.add_argument("--dry-run", action="store_true")

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
    query.add_argument("--venue")
    query.add_argument("--expression-strength", choices=("cautious", "moderate", "strong"))
    query.add_argument("--tone", choices=("neutral", "cautious", "assertive", "critical"))
    query.add_argument("--paragraph-words-min", type=int)
    query.add_argument("--paragraph-words-max", type=int)
    query.add_argument("--contains-math", action=argparse.BooleanOptionalAction, default=None)
    query.add_argument(
        "--return-mode",
        choices=("pattern_first", "paragraph_structure", "include_original"),
        default="pattern_first",
    )
    query.add_argument("--mode", choices=("dense", "sparse", "hybrid"), default="hybrid")
    query.add_argument("--reranker", choices=("off", "light", "quality"), default="off")
    query.add_argument("--top-k", type=int, default=10)
    query.add_argument("--environment", default="current")
    query.add_argument("--explain-plan", action="store_true")
    query.add_argument("--allow-auto-import", action="store_true")
    query.add_argument("--allow-issues", action="store_true")
    query.add_argument("--evidence-envelope", action="store_true")
    query.add_argument("--max-tokens", type=int, default=4000)


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
            elif args.hub_source_command == "inspect":
                item = registry.get(args.library)
                _emit(dataclasses.asdict(item) | {"installed_version": item.installed_version()})
            else:
                dependency_service = DependencyManifestService(
                    registry, config.code.data_root
                )

                def dependency_operation() -> dict[str, Any]:
                    return dependency_service.capture(
                        args.library, args.version, dry_run=False
                    )

                if args.dry_run:
                    result = dependency_service.capture(
                        args.library, args.version, dry_run=True
                    )
                else:
                    marker = (
                        config.code.data_root
                        / "sources"
                        / "repositories"
                        / args.library
                        / args.version
                        / "current.json"
                    )
                    result = _task_executor().execute(
                        "dependency_capture",
                        dependency_operation,
                        knowledge_base="code",
                        library=args.library,
                        version=args.version,
                        inputs={"version": args.version},
                        input_manifest=str(marker),
                        lock_keys=(f"library:{args.library}",),
                        output_manifest=lambda value: str(value["manifest"]),
                    )
                _emit(result)
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
            if args.sync_domain == "plan":
                from knowledgehub.core.atomic import atomic_write_json

                result = SyncPlanner(registry).plan(
                    trigger=args.trigger,
                    libraries=args.libraries,
                    version=args.version,
                    interval_hours=args.interval_hours,
                )
                if args.output:
                    atomic_write_json(args.output, result)
                    result["plan_path"] = str(args.output)
                _emit(result)
                return 0
            if args.sync_domain == "releases":
                names = (
                    [item.name for item in registry.list(enabled_only=True)]
                    if args.all
                    else [args.library]
                )
                watch = ReleaseWatchService(config, registry)
                executor = None if args.dry_run else _task_executor()
                results: list[dict[str, Any]] = []
                for name in names:
                    def release_operation(selected: str = name) -> dict[str, Any]:
                        return watch.check(selected, dry_run=False)

                    def release_output_manifest(
                        _result: Mapping[str, Any], selected: str = name
                    ) -> str:
                        return str(
                            config.code.data_root
                            / "state"
                            / "release-watch"
                            / f"{selected}.json"
                        )

                    if executor is None:
                        result = watch.check(name, dry_run=True)
                    else:
                        result = executor.execute(
                            "release_watch",
                            release_operation,
                            knowledge_base="code",
                            library=name,
                            inputs={"library": name},
                            input_manifest=str(config.code.registry),
                            lock_keys=(f"library:{name}",),
                            output_manifest=release_output_manifest,
                        )
                    results.append(result)
                _emit({"results": results})
                return 0
            if args.sync_domain == "version":
                importer = OnDemandVersionImporter(config, registry)

                def version_operation() -> dict[str, Any]:
                    return importer.import_version(
                        args.library,
                        args.version,
                        allowed=True,
                        build_limit=args.build_limit,
                        dry_run=False,
                    )

                if args.dry_run or not args.allow_download:
                    result = importer.import_version(
                        args.library,
                        args.version,
                        allowed=args.allow_download,
                        build_limit=args.build_limit,
                        dry_run=args.dry_run,
                    )
                else:
                    code_collection = config.rag_config("code").qdrant_collection
                    result = _task_executor().execute(
                        "code_version_import",
                        version_operation,
                        knowledge_base="code",
                        library=args.library,
                        version=args.version,
                        inputs={
                            "allow_download": True,
                            "build_limit": args.build_limit,
                        },
                        input_manifest=str(config.code.registry),
                        lock_keys=(
                            f"library:{args.library}",
                            f"index:code:{code_collection}",
                        ),
                        output_manifest=_version_import_output,
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
            names = (
                [item.name for item in registry.list(enabled_only=True)]
                if args.all
                else [args.library]
            )
            sync_results: list[dict[str, Any]] = []
            executor = None if args.dry_run else _task_executor()
            for name in names:
                def sync_operation(selected: str = name) -> dict[str, Any]:
                    return sync_service.sync(
                        selected, version=args.version, dry_run=False
                    )

                def sync_output_manifest(
                    _result: Mapping[str, Any], selected: str = name
                ) -> str:
                    return str(
                        config.code.data_root
                        / "manifests"
                        / f"sync-{selected}.json"
                    )

                if executor is None:
                    result = sync_service.sync(
                        name, version=args.version, dry_run=True
                    )
                else:
                    result = executor.execute(
                        "code_sync",
                        sync_operation,
                        knowledge_base="code",
                        library=name,
                        version=args.version,
                        inputs={
                            "dry_run": False,
                            "registry": str(config.code.registry),
                            "version": args.version,
                        },
                        input_manifest=str(config.code.registry),
                        lock_keys=(f"library:{name}",),
                        output_manifest=sync_output_manifest,
                    )
                sync_results.append(result)
            _emit({"results": sync_results})
            return (
                0 if all(item["status"] in {"success", "planned"} for item in sync_results) else 1
            )
        if args.source == "build":
            rag_config = config.rag_config("code")
            if args.build_domain == "diff":
                diff_service = VersionDiffBuildService(
                    registry, config.code.data_root, rag_config
                )
                try:
                    def diff_operation() -> dict[str, Any]:
                        return diff_service.build(
                            args.library,
                            args.from_version,
                            args.to_version,
                            symbols=args.symbols,
                            limit=args.limit,
                            dry_run=False,
                        )

                    if args.dry_run:
                        diff_result = diff_service.build(
                            args.library,
                            args.from_version,
                            args.to_version,
                            symbols=args.symbols,
                            limit=args.limit,
                            dry_run=True,
                        )
                    else:
                        diff_result = _task_executor().execute(
                            "code_diff_build",
                            diff_operation,
                            knowledge_base="code",
                            library=args.library,
                            version=args.to_version,
                            inputs={
                                "from_version": args.from_version,
                                "limit": args.limit,
                                "symbols": list(args.symbols),
                                "to_version": args.to_version,
                            },
                            input_manifest=str(
                                config.code.data_root / "state" / "symbols.sqlite3"
                            ),
                            lock_keys=(
                                f"library:{args.library}",
                                f"index:code:{rag_config.qdrant_collection}",
                            ),
                            output_manifest=lambda result: str(
                                result.get("normalized_manifest") or ""
                            )
                            or None,
                        )
                finally:
                    diff_service.close()
                _emit(diff_result)
                return 0 if diff_result["status"] == "success" else 1
            if args.candidate_collection:
                if args.candidate_collection == config.knowledge_bases["code"].collection:
                    raise ValueError(
                        "candidate collection must differ from the configured production collection"
                    )
                rag_config = rag_config.with_overrides(qdrant_collection=args.candidate_collection)
            build_service = CodeBuildService(registry, config.code.data_root, rag_config)
            try:
                def build_operation() -> dict[str, Any]:
                    return build_service.build(
                        args.library,
                        version=args.version,
                        limit=args.limit,
                        dry_run=False,
                        prune=args.prune,
                        normalized_namespace=args.candidate_collection,
                    )

                if args.dry_run:
                    build_result = build_service.build(
                        args.library,
                        version=args.version,
                        limit=args.limit,
                        dry_run=True,
                        prune=args.prune,
                        normalized_namespace=args.candidate_collection,
                    )
                else:
                    build_result = _task_executor().execute(
                        "code_build",
                        build_operation,
                        knowledge_base="code",
                        library=args.library,
                        version=args.version,
                        inputs={
                            "candidate_collection": args.candidate_collection,
                            "incremental": args.incremental,
                            "limit": args.limit,
                            "prune": args.prune,
                        },
                        input_manifest=str(config.code.registry),
                        lock_keys=(
                            f"library:{args.library}",
                            f"index:code:{rag_config.qdrant_collection}",
                        ),
                        output_manifest=lambda result: str(
                            result.get("normalized_manifest") or ""
                        )
                        or None,
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
                selected_limit = (
                    None if args.all else (args.limit or config.writing.default_limit)
                )
                def writing_operation() -> dict[str, Any]:
                    return writing_service.derive(
                        paper_id=args.paper_id,
                        collection=args.collection,
                        limit=selected_limit,
                        dry_run=False,
                        prune=args.prune,
                    )

                if args.dry_run:
                    writing_result = writing_service.derive(
                        paper_id=args.paper_id,
                        collection=args.collection,
                        limit=selected_limit,
                        dry_run=True,
                        prune=args.prune,
                    )
                else:
                    writing_collection = config.rag_config("writing").qdrant_collection
                    writing_result = _task_executor().execute(
                        "writing_derive",
                        writing_operation,
                        knowledge_base="writing",
                        inputs={
                            "all": args.all,
                            "collection": args.collection,
                            "limit": selected_limit,
                            "paper_id": args.paper_id,
                            "processor_version": config.writing.processor_version,
                            "prune": args.prune,
                        },
                        input_manifest=str(
                            config.writing.literature_data_dir
                            / "state"
                            / "pipeline.sqlite3"
                        ),
                        lock_keys=(
                            "derive:writing",
                            f"index:writing:{writing_collection}",
                        ),
                        output_manifest=lambda result: str(
                            result.get("derived_manifest") or ""
                        )
                        or None,
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
                    "venue": args.venue,
                    "expression_strength": args.expression_strength,
                    "tone": args.tone,
                    "paragraph_words_min": args.paragraph_words_min,
                    "paragraph_words_max": args.paragraph_words_max,
                    "contains_math": args.contains_math,
                }.items()
                if value not in (None, (), "")
            }
            plan = (
                build_code_query_plan(
                    args.query,
                    intent=args.intent,
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
            request = HubQueryRequest(
                knowledge_base=args.knowledge_base,
                query=args.query,
                intent=args.intent,
                filters=filters,
                top_k=args.top_k,
                mode=args.mode,
                return_mode=args.return_mode,
                reranker=args.reranker,
            )
            if args.evidence_envelope:
                from knowledgehub.hub.evidence import KnowledgeQueryService, QueryBudget

                _emit(
                    KnowledgeQueryService(config).query(
                        request,
                        QueryBudget(
                            max_results=args.top_k,
                            max_tokens=args.max_tokens,
                            allow_auto_import=args.allow_auto_import,
                            allow_issues=args.allow_issues,
                        ),
                    )
                )
                return 0
            search_result = HubQueryService(config).search(request)
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


def _task_executor() -> TaskExecutor:
    return TaskExecutor(TaskStore(default_task_store_path()))


def _version_import_output(result: Mapping[str, Any]) -> str | None:
    build = result.get("build")
    if isinstance(build, Mapping) and build.get("normalized_manifest"):
        return str(build["normalized_manifest"])
    marker = result.get("marker")
    return str(marker) if marker else None
