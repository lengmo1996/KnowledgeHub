#!/usr/bin/env python3
"""Read-only environment report used before any model or data operation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from knowledgehub.pipeline.config import RagConfig
from knowledgehub.pipeline.doctor import inspect_environment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("configs/rag/default.yaml"))
    args = parser.parse_args()
    print(json.dumps(inspect_environment(RagConfig.load(args.config)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
