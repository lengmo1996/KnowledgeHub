#!/usr/bin/env python3
"""Write V2 envelopes beside V1 JSON/JSONL input; never overwrite the source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from knowledgehub.core.atomic import atomic_write_jsonl
from knowledgehub.governance.schema import SchemaRegistry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("schema_name")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.source.resolve() == args.destination.resolve():
        raise SystemExit("source and destination must differ")
    records = []
    for line in args.source.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(
                SchemaRegistry().migrate(args.schema_name, json.loads(line)).to_dict()
            )
    atomic_write_jsonl(args.destination, records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
