"""Operational V2 commands; destructive recovery remains confirmation-gated."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from knowledgehub.code_rag.adapters import adapter_for
from knowledgehub.code_rag.symbols import SymbolIndex
from knowledgehub.code_rag.version_diff import compare_symbols
from knowledgehub.evaluation.runner import (
    EvaluationRunner,
    compare_reports,
    live_query_callback,
    load_thresholds,
    write_report,
)
from knowledgehub.governance.maintenance import CleanupService
from knowledgehub.governance.release import validate_release_manifest
from knowledgehub.governance.snapshots import CollectionPromotionManager, IndexSnapshotManager
from knowledgehub.governance.tasks import TaskExecutor, TaskStore, default_task_store_path
from knowledgehub.governance.validation import HubValidator
from knowledgehub.hub.config import HubConfig
from knowledgehub.hub.query import HubQueryRequest, HubQueryService
from knowledgehub.workflows.adaptation import AdaptationWorkflow, parse_debug_log
from knowledgehub.workflows.repository import RepositoryIntake
from knowledgehub.writing_rag.v2 import (
    WritingFeedbackStore,
    WritingProfileStore,
    WritingTaskPlanner,
    similarity_risk,
)


def add_v2_parsers(subparsers: Any) -> None:
    release = subparsers.add_parser("release", help="Inspect deterministic release metadata")
    release_commands = release.add_subparsers(dest="release_command", required=True)
    release_validate = release_commands.add_parser(
        "validate", help="Validate a release manifest and its repository config hashes"
    )
    release_validate.add_argument(
        "manifest",
        type=Path,
        nargs="?",
        default=Path("state/releases/v2_0_4_manifest.json"),
    )
    release_validate.add_argument("--repository-root", type=Path, default=Path("."))

    clean = subparsers.add_parser("clean", help="Plan or execute bounded runtime cleanup")
    clean_commands = clean.add_subparsers(dest="clean_command", required=True)
    clean_cache = clean_commands.add_parser("cache")
    clean_cache.add_argument("--min-age-hours", type=int, default=24)
    clean_source = clean_commands.add_parser("source")
    clean_source.add_argument("--library", required=True)
    clean_source.add_argument("--version", required=True)
    clean_snapshots = clean_commands.add_parser("snapshots")
    clean_snapshots.add_argument("knowledge_base", choices=("code", "writing"))
    clean_snapshots.add_argument("--keep", type=int, default=3)
    for parser in (clean_cache, clean_source, clean_snapshots):
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--yes", action="store_true")

    prune = subparsers.add_parser("prune", help="Plan or remove unreferenced artifacts")
    prune_commands = prune.add_subparsers(dest="prune_command", required=True)
    unreferenced = prune_commands.add_parser("unreferenced")
    unreferenced.add_argument(
        "--knowledge-base", choices=("code", "writing", "all"), default="all"
    )
    unreferenced.add_argument("--execute", action="store_true")
    unreferenced.add_argument("--yes", action="store_true")

    evaluation = subparsers.add_parser(
        "evaluate", help="Run grouped evaluation or compare regression reports"
    )
    evaluation_commands = evaluation.add_subparsers(
        dest="evaluation_command", required=True
    )
    evaluation_run = evaluation_commands.add_parser("run")
    evaluation_run.add_argument("--domain", choices=("all", "code", "writing"), default="all")
    evaluation_run.add_argument("--mode", choices=("offline", "live"), default="offline")
    evaluation_run.add_argument("--profile", choices=("v1", "v2"), default="v2")
    evaluation_run.add_argument("--top-k", type=int, default=10)
    evaluation_run.add_argument("--eval-root", type=Path, default=Path("eval"))
    evaluation_run.add_argument("--output", type=Path)
    evaluation_compare = evaluation_commands.add_parser("compare")
    evaluation_compare.add_argument("baseline", type=Path)
    evaluation_compare.add_argument("candidate", type=Path)
    evaluation_compare.add_argument(
        "--thresholds", type=Path, default=Path("configs/evaluation/v2.yaml")
    )
    evaluation_compare.add_argument("--output", type=Path)

    index = subparsers.add_parser("index", help="Create, list or recover Qdrant snapshots")
    commands = index.add_subparsers(dest="index_command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("knowledge_base", choices=("literature", "code", "writing"))
    listing = commands.add_parser("list-snapshots")
    listing.add_argument("knowledge_base", choices=("literature", "code", "writing"))
    rollback = commands.add_parser("rollback")
    rollback.add_argument("knowledge_base", choices=("literature", "code", "writing"))
    rollback.add_argument("snapshot_id")
    rollback.add_argument("--yes", action="store_true")
    stage = commands.add_parser("stage", help="Register a populated physical candidate")
    stage.add_argument("knowledge_base", choices=("literature", "code", "writing"))
    stage.add_argument("candidate_collection")
    promote = commands.add_parser("promote", help="Atomically move the stable alias")
    promote.add_argument("knowledge_base", choices=("literature", "code", "writing"))
    promote.add_argument("--yes", action="store_true")
    alias_rollback = commands.add_parser("rollback-alias")
    alias_rollback.add_argument("knowledge_base", choices=("literature", "code", "writing"))
    alias_rollback.add_argument("--yes", action="store_true")
    alias_status = commands.add_parser("alias-status")
    alias_status.add_argument("knowledge_base", choices=("literature", "code", "writing"))

    task = subparsers.add_parser("task", help="Inspect unified tasks or force-unlock a resource")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    listing = task_commands.add_parser("list")
    listing.add_argument("--limit", type=int, default=50)
    inspect_task = task_commands.add_parser("inspect")
    inspect_task.add_argument("task_id")
    unlock = task_commands.add_parser("unlock")
    unlock.add_argument("lock_key")
    unlock.add_argument("--force", action="store_true", required=True)

    validate = subparsers.add_parser(
        "validate", help="Validate cross-domain source and derived integrity"
    )
    validate.add_argument(
        "target",
        choices=("sources", "normalized", "dependencies", "writing", "index", "all"),
    )
    validate.add_argument("knowledge_base", nargs="?", choices=("code", "writing"))
    validate.add_argument(
        "--offline",
        action="store_true",
        help="Validate local state and artifacts without contacting Qdrant",
    )

    symbol = subparsers.add_parser(
        "symbol", help="Build, inspect or compare the Python symbol catalog"
    )
    symbol_commands = symbol.add_subparsers(dest="symbol_command", required=True)
    build = symbol_commands.add_parser("build")
    build.add_argument(
        "library", choices=("pytorch", "transformers", "diffusers", "accelerate", "lightning")
    )
    build.add_argument("version")
    inspect = symbol_commands.add_parser("inspect")
    inspect.add_argument("library")
    inspect.add_argument("version")
    inspect.add_argument("symbol")
    compare = symbol_commands.add_parser("compare")
    compare.add_argument("library")
    compare.add_argument("from_version")
    compare.add_argument("to_version")
    compare.add_argument("symbol")

    repository = subparsers.add_parser(
        "repository", help="Analyze a repository without executing it"
    )
    repository_commands = repository.add_subparsers(dest="repository_command", required=True)
    analyze = repository_commands.add_parser("analyze")
    analyze.add_argument("path", type=Path)
    analyze.add_argument("--environment", default="current")
    analyze.add_argument("--output-root", type=Path, default=Path("/data/KnowledgeHub/reports"))
    evidence = repository_commands.add_parser("evidence")
    evidence.add_argument("path", type=Path)
    evidence.add_argument("--issue", required=True)
    evidence.add_argument("--environment", default="current")
    evidence.add_argument("--file", action="append", dest="files", required=True)
    evidence.add_argument("--query")
    evidence.add_argument("--library")
    evidence.add_argument("--version")
    evidence.add_argument("--symbol")
    evidence.add_argument("--strategy", required=True)
    evidence.add_argument("--confidence", type=float, required=True)
    evidence.add_argument("--output-root", type=Path, default=Path("/data/KnowledgeHub/reports"))
    change = repository_commands.add_parser("record-change")
    change.add_argument("path", type=Path)
    change.add_argument("--file", action="append", dest="files", required=True)
    change.add_argument("--reason", required=True)
    change.add_argument("--old-api")
    change.add_argument("--new-api")
    change.add_argument("--evidence-id", action="append", dest="evidence_ids", required=True)
    change.add_argument("--output-root", type=Path, default=Path("/data/KnowledgeHub/reports"))
    verification = repository_commands.add_parser("record-verification")
    verification.add_argument("path", type=Path)
    verification.add_argument("--name", required=True)
    verification.add_argument("--command", required=True)
    verification.add_argument("--exit-code", type=int, required=True)
    verification.add_argument("--output-file", type=Path)
    verification.add_argument("--output", default="")
    verification.add_argument("--scope", default="bounded")
    verification.add_argument(
        "--output-root", type=Path, default=Path("/data/KnowledgeHub/reports")
    )
    finalize = repository_commands.add_parser("finalize")
    finalize.add_argument("path", type=Path)
    finalize.add_argument("--risk", action="append", dest="risks", default=[])
    finalize.add_argument("--output-root", type=Path, default=Path("/data/KnowledgeHub/reports"))
    audit = repository_commands.add_parser("validate")
    audit.add_argument("path", type=Path)
    audit.add_argument("--output-root", type=Path, default=Path("/data/KnowledgeHub/reports"))
    debug_log = repository_commands.add_parser("debug-log")
    debug_log.add_argument("path", type=Path)
    debug_log.add_argument("--log-file", type=Path, required=True)

    writing = subparsers.add_parser("writing-v2", help="Writing similarity and feedback operations")
    writing_commands = writing.add_subparsers(dest="writing_v2_command", required=True)
    risk = writing_commands.add_parser("similarity")
    risk.add_argument("text")
    feedback = writing_commands.add_parser("feedback")
    feedback.add_argument("writing_id")
    feedback.add_argument("label")
    profile = writing_commands.add_parser("profile")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    venue = profile_commands.add_parser("venue")
    venue.add_argument("name")
    venue.add_argument("--paper-id", action="append", dest="paper_ids", required=True)
    venue.add_argument(
        "--section",
        action="append",
        dest="sections",
        choices=("Introduction", "Method", "Experiment"),
        default=[],
    )
    personal = profile_commands.add_parser("personal")
    personal.add_argument("name")
    personal.add_argument("--draft", action="append", type=Path, dest="drafts", required=True)
    profiles = writing_commands.add_parser("profiles")
    profiles.add_argument("--type", choices=("venue", "personal"), dest="profile_type")
    writing_task = writing_commands.add_parser("task")
    writing_task.add_argument(
        "task",
        choices=tuple(sorted(WritingTaskPlanner.TASKS)),
    )
    writing_task.add_argument("objective")
    writing_task.add_argument("--text")
    writing_task.add_argument("--section")
    writing_task.add_argument("--function", dest="writing_function")
    writing_task.add_argument("--domain")
    writing_task.add_argument("--venue")


def run_v2_command(args: argparse.Namespace) -> int:
    if args.source == "release":
        result = validate_release_manifest(args.manifest, repository_root=args.repository_root)
        _emit(result)
        return 0 if result["valid"] else 1
    config = HubConfig.load(args.hub_config or "configs/knowledgehub.yaml")
    if args.source in {"clean", "prune"}:
        service = CleanupService(
            code_root=config.code.data_root,
            rag_dirs={
                "code": config.knowledge_bases["code"].data_dir,
                "writing": config.knowledge_bases["writing"].data_dir,
            },
            index_root=Path("/data/KnowledgeHub/indexes"),
        )
        qdrant_client = None
        if args.source == "clean" and args.clean_command == "cache":
            plan = service.plan_cache(min_age_hours=args.min_age_hours)
        elif args.source == "clean" and args.clean_command == "source":
            plan = service.plan_source(args.library, args.version)
        elif args.source == "clean":
            plan = service.plan_snapshots(args.knowledge_base, keep=args.keep)
            if args.execute:
                from qdrant_client import QdrantClient

                qdrant_client = QdrantClient(
                    url=config.rag_config(args.knowledge_base).qdrant_url
                )
        else:
            names = (
                ("code", "writing")
                if args.knowledge_base == "all"
                else (args.knowledge_base,)
            )
            plan = service.plan_unreferenced(names)
        try:
            result = (
                service.execute(plan, confirmed=args.yes, qdrant_client=qdrant_client)
                if args.execute
                else plan
            )
        finally:
            if qdrant_client is not None:
                qdrant_client.close()
        return _emit(result)
    if args.source == "evaluate":
        if args.evaluation_command == "run":
            query = live_query_callback(config) if args.mode == "live" else None
            report = EvaluationRunner(args.eval_root, query=query).run(
                domain=args.domain,
                mode=args.mode,
                profile=args.profile,
                top_k=args.top_k,
            )
            if args.output:
                write_report(args.output, report)
                report["report_path"] = str(args.output)
            _emit(report)
            return 1 if report["summary"]["failed_groups"] else 0
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
        comparison = compare_reports(
            baseline,
            candidate,
            load_thresholds(args.thresholds),
        )
        if args.output:
            write_report(args.output, comparison)
            comparison["report_path"] = str(args.output)
        _emit(comparison)
        return 0 if comparison["passed"] else 1
    if args.source == "index":
        from qdrant_client import QdrantClient

        manager = IndexSnapshotManager(
            Path("/data/KnowledgeHub/indexes"),
            QdrantClient(url=config.rag_config(args.knowledge_base).qdrant_url),
        )
        collection = config.knowledge_bases[args.knowledge_base].collection
        promotion = CollectionPromotionManager(Path("/data/KnowledgeHub/indexes"), manager.client)
        if args.index_command == "snapshot":
            return _emit(manager.create(args.knowledge_base, collection))
        if args.index_command == "list-snapshots":
            return _emit({"snapshots": manager.list(args.knowledge_base)})
        if args.index_command == "rollback":
            return _emit(
                manager.rollback(args.knowledge_base, args.snapshot_id, confirmed=args.yes)
            )
        if args.index_command == "stage":
            return _emit(promotion.stage(args.knowledge_base, args.candidate_collection))
        if args.index_command == "promote":
            return _emit(promotion.promote(args.knowledge_base, collection, confirmed=args.yes))
        if args.index_command == "rollback-alias":
            return _emit(promotion.rollback(args.knowledge_base, confirmed=args.yes))
        return _emit(promotion.status(args.knowledge_base, collection))
    if args.source == "task":
        store = TaskStore(default_task_store_path())
        if args.task_command == "list":
            return _emit({"tasks": store.list_tasks(args.limit)})
        if args.task_command == "inspect":
            task_value = store.get(args.task_id)
            if task_value is None:
                _emit({"status": "not_found", "task_id": args.task_id})
                return 1
            return _emit(
                {
                    "task": task_value,
                    "result": store.result(args.task_id),
                    "attempts": store.list_attempts(args.task_id),
                }
            )
        store.release(args.lock_key, force=args.force)
        return _emit({"status": "unlocked", "lock_key": args.lock_key})
    if args.source == "validate":
        validator = HubValidator(
            config.code.data_root,
            config.writing.data_root,
            rag_dirs={
                "code": config.knowledge_bases["code"].data_dir,
                "writing": config.knowledge_bases["writing"].data_dir,
            },
        )
        if args.target == "index" and args.knowledge_base is None:
            return _emit_error("knowledge_base is required for index validation")
        if args.target != "index" and args.knowledge_base is not None:
            return _emit_error("knowledge_base is valid only with target=index")
        validation_names: tuple[str, ...]
        if args.target == "index":
            validation_names = (str(args.knowledge_base),)
        elif args.target == "all":
            validation_names = ("code", "writing")
        else:
            validation_names = ()
        clients: dict[str, Any] = {}
        indexes: dict[str, tuple[Any | None, str | None]] = {}
        try:
            if not args.offline:
                from qdrant_client import QdrantClient

                for name in validation_names:
                    rag_config = config.rag_config(name)
                    client = QdrantClient(url=rag_config.qdrant_url)
                    clients[name] = client
                    indexes[name] = (client, rag_config.qdrant_collection)
            if args.target == "index":
                knowledge_base = str(args.knowledge_base)
                client, validation_collection = indexes.get(
                    knowledge_base, (None, None)
                )
                result = validator.index(
                    knowledge_base,
                    qdrant_client=client,
                    collection=validation_collection,
                )
            elif args.target == "all":
                result = validator.all(indexes=indexes)
            else:
                result = getattr(validator, args.target)()
        finally:
            for client in clients.values():
                client.close()
        _emit(result)
        return 0 if result["valid"] else 1
    if args.source == "symbol":
        catalog = SymbolIndex(config.code.data_root / "state" / "symbols.sqlite3")
        if args.symbol_command == "build":
            marker = (
                config.code.data_root
                / "sources"
                / "repositories"
                / args.library
                / args.version
                / "current.json"
            )
            value = json.loads(marker.read_text(encoding="utf-8"))
            root = Path(value["source_path"])
            def symbol_operation() -> dict[str, Any]:
                return {
                    "status": "success",
                    **catalog.build(
                        args.library,
                        args.version,
                        root,
                        adapter_for(args.library).discover_source(root),
                    ),
                }

            result = TaskExecutor(TaskStore(default_task_store_path())).execute(
                "symbol_build",
                symbol_operation,
                knowledge_base="code",
                library=args.library,
                version=args.version,
                inputs={"source_commit": value.get("commit")},
                input_manifest=str(marker),
                lock_keys=(
                    f"library:{args.library}",
                    "symbols:catalog",
                ),
                output_manifest=lambda _result: str(catalog.path),
            )
            return _emit(result)
        if args.symbol_command == "inspect":
            return _emit(
                catalog.inspect(args.library, args.version, args.symbol) or {"status": "not_found"}
            )
        old = catalog.inspect(args.library, args.from_version, args.symbol)
        new = catalog.inspect(args.library, args.to_version, args.symbol)
        return _emit(compare_symbols(old, new))
    if args.source == "repository":
        if args.repository_command == "debug-log":
            return _emit(
                parse_debug_log(
                    args.log_file.read_text(encoding="utf-8", errors="replace"),
                    args.path,
                )
            )
        workflow = AdaptationWorkflow(args.path, args.output_root)
        if args.repository_command in {"analyze", "evidence"}:
            environment_path = (
                config.code.data_root / "state" / "environments" / f"{args.environment}.json"
            )
            environment = json.loads(environment_path.read_text(encoding="utf-8"))
            if args.repository_command == "analyze":
                result = RepositoryIntake(args.path).analyze(environment, args.output_root)
                profile = result["profile"]
                return _emit(
                    {
                        "repository": profile["repository"],
                        "version": profile.get("version"),
                        "commit": profile.get("commit"),
                        "dependencies": len(profile["dependencies"]),
                        "api_libraries": len(profile["api_usage"]),
                        "compatibility_statuses": {
                            status: sum(
                                item["status"] == status for item in result["compatibility_matrix"]
                            )
                            for status in ("likely_compatible", "conflict", "unknown")
                        },
                        "report": result["report"],
                    }
                )
            evidence_values: list[dict[str, Any]] = []
            warnings: list[str] = []
            if args.symbol and args.library and args.version:
                catalog = SymbolIndex(
                    config.code.data_root / "state" / "symbols.sqlite3", read_only=True
                )
                symbol = catalog.inspect(args.library, args.version, args.symbol)
                if symbol is None:
                    warnings.append("exact_symbol_not_found")
                else:
                    marker_path = (
                        config.code.data_root
                        / "sources"
                        / "repositories"
                        / args.library
                        / args.version
                        / "current.json"
                    )
                    marker = json.loads(marker_path.read_text(encoding="utf-8"))
                    source_path = Path(marker["source_path"]) / symbol["path"]
                    source_lines = source_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    start = max(0, int(symbol["start_line"]) - 1)
                    end = min(len(source_lines), int(symbol["end_line"]))
                    repository = str(marker["repository"])
                    commit = str(marker["commit"])
                    evidence_values.append(
                        {
                            "source_type": "source_code",
                            "library": args.library,
                            "version": args.version,
                            "symbol": symbol["qualified_name"],
                            "content": f"Signature: {symbol['signature']}\n\n"
                            + "\n".join(source_lines[start:end]),
                            "source_url": f"https://github.com/{repository}/blob/{commit}/{symbol['path']}#L{symbol['start_line']}",
                            "commit": commit,
                            "evidence_role": "exact_symbol_source",
                            "inference": False,
                        }
                    )
            if args.query:
                filters = {
                    key: value
                    for key, value in {
                        "library": args.library,
                        "version": args.version,
                        "symbol": args.symbol,
                    }.items()
                    if value
                }
                response = HubQueryService(config).search(
                    HubQueryRequest(
                        knowledge_base="code",
                        query=args.query,
                        intent="compatibility",
                        filters=filters,
                        top_k=8,
                    )
                )
                warnings.extend(response.warnings)
                evidence_values.extend(
                    [
                        {
                            "source_type": hit.payload.get("source_type"),
                            "library": hit.payload.get("library"),
                            "version": hit.payload.get("version"),
                            "symbol": hit.payload.get("symbol"),
                            "content": hit.payload.get("text"),
                            "source_url": hit.payload.get("source_url"),
                            "commit": hit.payload.get("commit"),
                            "evidence_role": hit.payload.get("evidence_role"),
                            "inference": hit.payload.get("inference", False),
                        }
                        for hit in response.hits
                    ]
                )
            else:
                if not evidence_values:
                    warnings.append("no_code_rag_query_was_requested")
            return _emit(
                workflow.create_evidence(
                    issue=args.issue,
                    environment=environment,
                    affected_files=args.files,
                    retrieved_evidence=evidence_values,
                    recommended_strategy=args.strategy,
                    confidence=args.confidence,
                    warnings=warnings,
                )
            )
        if args.repository_command == "validate":
            result = workflow.validate()
            _emit(result)
            return 0 if result["valid"] else 1
        if args.repository_command == "record-change":
            return _emit(
                workflow.record_change(
                    affected_files=args.files,
                    reason=args.reason,
                    old_api=args.old_api,
                    new_api=args.new_api,
                    evidence_ids=args.evidence_ids,
                )
            )
        if args.repository_command == "record-verification":
            output = (
                args.output_file.read_text(encoding="utf-8", errors="replace")
                if args.output_file
                else args.output
            )
            return _emit(
                workflow.record_verification(
                    name=args.name,
                    command=args.command,
                    exit_code=args.exit_code,
                    output=output,
                    scope=args.scope,
                )
            )
        return _emit(workflow.finalize(unresolved_risks=args.risks))
    if args.source == "writing-v2":
        if args.writing_v2_command == "feedback":
            return _emit(
                WritingFeedbackStore(
                    config.writing.data_root / "state" / "feedback.sqlite3"
                ).submit(args.writing_id, args.label)
            )
        profile_store = WritingProfileStore(config.writing.data_root / "manifests" / "profiles")
        if args.writing_v2_command == "profiles":
            return _emit({"profiles": profile_store.list(args.profile_type)})
        if args.writing_v2_command == "profile":
            if args.profile_command == "personal":
                return _emit(profile_store.build_personal(name=args.name, drafts=args.drafts))
            entries = [
                json.loads(line)
                for line in (config.writing.data_root / "derived" / "writing_entries.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            return _emit(
                profile_store.build_venue(
                    entries,
                    name=args.name,
                    paper_ids=args.paper_ids,
                    sections=args.sections,
                )
            )
        if args.writing_v2_command == "task":
            filters = {
                key: value
                for key, value in {
                    "section": args.section,
                    "writing_function": args.writing_function,
                    "research_domain": args.domain,
                    "venue": args.venue,
                }.items()
                if value
            }
            plan = WritingTaskPlanner().plan(
                args.task,
                objective=args.objective,
                text=args.text,
                filters=filters,
            )
            if args.task != "audit_source_similarity":
                return _emit(plan)
            entries = [
                json.loads(line)
                for line in (config.writing.data_root / "derived" / "writing_entries.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            sources = [
                {"source_id": item["writing_id"], "text": item["original_text"]}
                for item in entries
            ]
            return _emit({"plan": plan, "similarity_audit": similarity_risk(args.text, sources)})
        entries = [
            json.loads(line)
            for line in (config.writing.data_root / "derived" / "writing_entries.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        sources = [
            {"source_id": item["writing_id"], "text": item["original_text"]} for item in entries
        ]
        return _emit(similarity_risk(args.text, sources))
    raise ValueError("unsupported V2 command")


def _emit(value: Any) -> int:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    return 0


def _emit_error(message: str) -> int:
    _emit({"valid": False, "errors": [message]})
    return 2
