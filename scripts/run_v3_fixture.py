#!/usr/bin/env python3
"""Run the controlled V3 fixture through the public project services."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from knowledgehub.project.fixture import FixtureOrchestrator
from knowledgehub.project.registry import ProjectRegistry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--state-root", type=Path, default=Path("state/fixtures"))
    args = parser.parse_args()
    result = FixtureOrchestrator(
        args.repository_root,
        ProjectRegistry(args.state_root),
    ).run_all()
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if result["validation"]["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
