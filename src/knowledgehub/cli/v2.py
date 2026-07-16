"""Operational V2 commands; destructive recovery remains confirmation-gated."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from knowledgehub.code_rag.adapters import adapter_for
from knowledgehub.code_rag.symbols import SymbolIndex
from knowledgehub.code_rag.version_diff import compare_symbols
from knowledgehub.governance.snapshots import CollectionPromotionManager, IndexSnapshotManager
from knowledgehub.governance.tasks import TaskStore
from knowledgehub.governance.validation import HubValidator
from knowledgehub.hub.config import HubConfig
from knowledgehub.workflows.repository import RepositoryIntake
from knowledgehub.writing_rag.v2 import WritingFeedbackStore, similarity_risk


def add_v2_parsers(subparsers: Any) -> None:
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
    unlock = task_commands.add_parser("unlock")
    unlock.add_argument("lock_key")
    unlock.add_argument("--force", action="store_true", required=True)

    validate = subparsers.add_parser("validate", help="Validate cross-domain source and derived integrity")
    validate.add_argument("target", choices=("sources", "normalized", "writing", "all"))

    symbol = subparsers.add_parser("symbol", help="Build, inspect or compare the Python symbol catalog")
    symbol_commands = symbol.add_subparsers(dest="symbol_command", required=True)
    build = symbol_commands.add_parser("build")
    build.add_argument("library", choices=("pytorch", "transformers", "diffusers", "accelerate", "lightning"))
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

    repository = subparsers.add_parser("repository", help="Analyze a repository without executing it")
    repository_commands = repository.add_subparsers(dest="repository_command", required=True)
    analyze = repository_commands.add_parser("analyze")
    analyze.add_argument("path", type=Path)
    analyze.add_argument("--environment", default="current")
    analyze.add_argument("--output-root", type=Path, default=Path("/data/KnowledgeHub/reports"))

    writing = subparsers.add_parser("writing-v2", help="Writing similarity and feedback operations")
    writing_commands = writing.add_subparsers(dest="writing_v2_command", required=True)
    risk = writing_commands.add_parser("similarity")
    risk.add_argument("text")
    feedback = writing_commands.add_parser("feedback")
    feedback.add_argument("writing_id")
    feedback.add_argument("label")


def run_v2_command(args: argparse.Namespace) -> int:
    config = HubConfig.load(args.hub_config or "configs/knowledgehub.yaml")
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
            return _emit(manager.rollback(args.knowledge_base, args.snapshot_id, confirmed=args.yes))
        if args.index_command == "stage":
            return _emit(promotion.stage(args.knowledge_base, args.candidate_collection))
        if args.index_command == "promote":
            return _emit(promotion.promote(args.knowledge_base, collection, confirmed=args.yes))
        if args.index_command == "rollback-alias":
            return _emit(promotion.rollback(args.knowledge_base, confirmed=args.yes))
        return _emit(promotion.status(args.knowledge_base, collection))
    if args.source == "task":
        store = TaskStore(Path("/data/KnowledgeHub/state/tasks.sqlite3"))
        if args.task_command == "list":
            return _emit({"tasks": store.list_tasks(args.limit)})
        store.release(args.lock_key, force=args.force)
        return _emit({"status": "unlocked", "lock_key": args.lock_key})
    if args.source == "validate":
        validator = HubValidator(config.code.data_root, config.writing.data_root)
        result = getattr(validator, args.target)()
        _emit(result)
        return 0 if result["valid"] else 1
    if args.source == "symbol":
        catalog = SymbolIndex(config.code.data_root / "state" / "symbols.sqlite3")
        if args.symbol_command == "build":
            marker = config.code.data_root / "sources" / "repositories" / args.library / args.version / "current.json"
            value = json.loads(marker.read_text(encoding="utf-8"))
            root = Path(value["source_path"])
            result = catalog.build(args.library, args.version, root, adapter_for(args.library).discover_source(root))
            return _emit(result)
        if args.symbol_command == "inspect":
            return _emit(catalog.inspect(args.library, args.version, args.symbol) or {"status": "not_found"})
        old = catalog.inspect(args.library, args.from_version, args.symbol)
        new = catalog.inspect(args.library, args.to_version, args.symbol)
        return _emit(compare_symbols(old, new))
    if args.source == "repository":
        environment_path = config.code.data_root / "state" / "environments" / f"{args.environment}.json"
        environment = json.loads(environment_path.read_text(encoding="utf-8"))
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
    if args.source == "writing-v2":
        if args.writing_v2_command == "feedback":
            return _emit(WritingFeedbackStore(config.writing.data_root / "state" / "feedback.sqlite3").submit(args.writing_id, args.label))
        entries = [json.loads(line) for line in (config.writing.data_root / "derived" / "writing_entries.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        sources = [{"source_id": item["writing_id"], "text": item["original_text"]} for item in entries]
        return _emit(similarity_risk(args.text, sources))
    raise ValueError("unsupported V2 command")


def _emit(value: Any) -> int:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    return 0
