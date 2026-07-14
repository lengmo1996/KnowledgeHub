"""Top-level CLI dispatch."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from knowledgehub.cli.rag import add_rag_parser, run_rag_command
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
    subparsers = parser.add_subparsers(dest="source", required=True)
    add_zotero_parser(subparsers)
    add_rag_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.source == "zotero":
        return run_zotero_command(args)
    if args.source == "rag":
        return run_rag_command(args)
    parser.error(f"Unsupported source: {args.source}")


__all__ = ["build_parser", "main"]
