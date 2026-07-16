"""Top-level CLI dispatch."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from knowledgehub.cli.hub import add_hub_parsers, run_hub_command
from knowledgehub.cli.rag import add_rag_parser, run_rag_command
from knowledgehub.cli.v2 import add_v2_parsers, run_v2_command
from knowledgehub.mcp.cli import add_mcp_parser, run_mcp_command
from knowledgehub.sources.zotero.cli import add_zotero_parser, run_zotero_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowledgehub", description="KnowledgeHub source synchronization"
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Source YAML file (defaults to configs/sources/zotero.yaml when present)",
    )
    parser.add_argument("--hub-config", type=Path, help="Multi-knowledge-base YAML file")
    subparsers = parser.add_subparsers(dest="source", required=True)
    add_zotero_parser(subparsers)
    add_rag_parser(subparsers)
    add_mcp_parser(subparsers)
    add_hub_parsers(subparsers)
    add_v2_parsers(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.source == "zotero":
        return run_zotero_command(args)
    if args.source == "rag":
        return run_rag_command(args)
    if args.source == "mcp":
        return run_mcp_command(args)
    if args.source in {"source", "environment", "sync", "build", "derive", "query"}:
        return run_hub_command(args)
    if args.source in {
        "index",
        "task",
        "validate",
        "symbol",
        "repository",
        "writing-v2",
        "evaluate",
        "clean",
        "prune",
        "release",
    }:
        return run_v2_command(args)
    parser.error(f"Unsupported source: {args.source}")


__all__ = ["build_parser", "main"]
