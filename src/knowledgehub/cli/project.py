"""CLI adapters for V3 project workspaces and the controlled fixture."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from knowledgehub.core.atomic import atomic_write_json
from knowledgehub.project.context import ProjectContextBuilder
from knowledgehub.project.fixture import FixtureOrchestrator
from knowledgehub.project.knowledge import FixtureKnowledgeRouter, ProjectQueryService
from knowledgehub.project.models import ContextBudget, Workspace
from knowledgehub.project.registry import ProjectRegistry
from knowledgehub.project.skills import ProjectSkillService


def _state(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-root", type=Path, default=Path("state/fixtures"))


def add_project_parsers(subparsers: Any) -> None:
    workspace = subparsers.add_parser("workspace", help="Manage project workspaces")
    workspace_commands = workspace.add_subparsers(dest="workspace_command", required=True)
    create = workspace_commands.add_parser("create")
    create.add_argument("config", type=Path)
    listing = workspace_commands.add_parser("list")
    listing.add_argument("--include-fixtures", action="store_true")
    show = workspace_commands.add_parser("show")
    show.add_argument("workspace_id")
    validate = workspace_commands.add_parser("validate")
    validate.add_argument("workspace_id")
    validate.add_argument("--repository-root", type=Path, default=Path("."))
    export = workspace_commands.add_parser("export")
    export.add_argument("workspace_id")
    export.add_argument("--output", type=Path)
    archive = workspace_commands.add_parser("archive")
    archive.add_argument("workspace_id")
    for parser in (create, listing, show, validate, export, archive):
        _state(parser)

    fixture = subparsers.add_parser("fixture", help="Run or clean an isolated fixture")
    fixture_commands = fixture.add_subparsers(dest="fixture_command", required=True)
    run = fixture_commands.add_parser("run")
    run.add_argument("workspace_id", nargs="?", default="fixture-vision-project")
    run.add_argument("--repository-root", type=Path, default=Path("."))
    fixture_validate = fixture_commands.add_parser("validate")
    fixture_validate.add_argument("workspace_id")
    fixture_validate.add_argument("--repository-root", type=Path, default=Path("."))
    clean = fixture_commands.add_parser("clean")
    clean.add_argument("workspace_id")
    clean.add_argument(
        "--execute", action="store_true", help="Execute the displayed plan; default is dry-run"
    )
    for parser in (run, fixture_validate, clean):
        _state(parser)

    project = subparsers.add_parser("project", help="Build or query project context")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    context = project_commands.add_parser("context")
    context.add_argument("workspace_id")
    context.add_argument("task", choices=tuple(sorted({
        "project_overview", "code_debugging", "experiment_analysis", "decision_review",
        "academic_writing",
    })))
    context.add_argument("--experiment-id", action="append", default=[])
    context.add_argument("--max-records", type=int, default=20)
    context.add_argument("--max-characters", type=int, default=12_000)
    context.add_argument("--include-raw-logs", action="store_true")
    context.add_argument("--include-paper-fragments", action="store_true")
    query = project_commands.add_parser("query")
    query.add_argument("workspace_id")
    query.add_argument("task")
    query.add_argument("query")
    query.add_argument("--experiment-id", action="append", default=[])
    query.add_argument("--fixture-root", type=Path, default=Path("fixtures/v3/fixture_vision_project"))
    skill = project_commands.add_parser("skill")
    skill.add_argument("skill", choices=(
        "code-debugging", "research-result-analysis", "research-decision-review", "writing-academic"
    ))
    skill.add_argument("workspace_id")
    skill.add_argument("--experiment-id", action="append", default=[])
    skill.add_argument("--section", default="Results")
    skill.add_argument("--writing-function", default="experimental_comparison")
    skill.add_argument("--fixture-root", type=Path, default=Path("fixtures/v3/fixture_vision_project"))
    for parser in (context, query, skill):
        _state(parser)


def run_project_command(args: argparse.Namespace) -> int:
    registry = ProjectRegistry(args.state_root)
    try:
        if args.source == "workspace":
            if args.workspace_command == "create":
                raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))
                return _emit(registry.create(Workspace.from_dict(raw)))
            if args.workspace_command == "list":
                return _emit(
                    {"workspaces": registry.list_workspaces(include_fixtures=args.include_fixtures)}
                )
            if args.workspace_command == "show":
                return _emit(registry.get(args.workspace_id))
            if args.workspace_command == "validate":
                result = registry.validate(args.workspace_id, repository_root=args.repository_root)
                _emit(result)
                return 0 if result["valid"] else 1
            if args.workspace_command == "export":
                result = registry.export(args.workspace_id)
                if args.output:
                    atomic_write_json(args.output, result)
                return _emit(result | ({"output": str(args.output)} if args.output else {}))
            return _emit(registry.archive(args.workspace_id))
        if args.source == "fixture":
            if args.fixture_command == "run":
                if args.workspace_id != "fixture-vision-project":
                    raise ValueError("only the controlled fixture-vision-project can be run")
                return _emit(FixtureOrchestrator(args.repository_root, registry).run_all())
            if args.fixture_command == "validate":
                result = registry.validate(args.workspace_id, repository_root=args.repository_root)
                _emit(result)
                return 0 if result["valid"] else 1
            return _emit(registry.cleanup(args.workspace_id, execute=args.execute))
        builder = ProjectContextBuilder(registry)
        if args.project_command == "context":
            budget = ContextBudget(
                max_records=args.max_records,
                max_characters=args.max_characters,
                experiment_ids=tuple(args.experiment_id),
                include_raw_logs=args.include_raw_logs,
                include_paper_fragments=args.include_paper_fragments,
            )
            return _emit(builder.build(args.workspace_id, args.task, budget=budget))
        router = FixtureKnowledgeRouter(args.fixture_root)
        query_service = ProjectQueryService(builder, router)
        if args.project_command == "query":
            return _emit(query_service.query(
                args.workspace_id,
                args.task,
                args.query,
                experiment_ids=tuple(args.experiment_id),
            ))
        return _emit(ProjectSkillService(registry, query_service).run(
            args.skill,
            args.workspace_id,
            experiment_ids=tuple(args.experiment_id),
            section=args.section,
            writing_function=args.writing_function,
        ))
    except (FileNotFoundError, KeyError, PermissionError, ValueError) as exc:
        _emit({"error": str(exc), "error_type": type(exc).__name__})
        return 2


def _emit(value: Any) -> int:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    return 0
