#!/usr/bin/env python3
"""Thin executable wrapper around the unified bounded benchmark command."""

from __future__ import annotations

import sys

from knowledgehub.cli.main import main

if __name__ == "__main__":
    raise SystemExit(
        main(["--config", "configs/rag/default.yaml", "rag", "benchmark", *sys.argv[1:]])
    )
