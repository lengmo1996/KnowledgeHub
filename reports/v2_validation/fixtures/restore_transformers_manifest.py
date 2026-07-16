"""Rebuild the frozen Transformers 5.13.1 manifest against its current checkout."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from knowledgehub.code_rag.build import CodeBuildService
from knowledgehub.code_rag.registry import CodeSourceRegistry
from knowledgehub.core.atomic import atomic_write_jsonl
from knowledgehub.core.hashing import sha256_json
from knowledgehub.pipeline.config import RagConfig


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if args.execute != args.yes:
        parser.error("execution requires both --execute and --yes")

    code_root = Path("/data/KnowledgeHub/code")
    rag_root = Path("/data/KnowledgeHub/rag/code")
    marker = json.loads(
        (code_root / "sources/repositories/transformers/5.13.1/current.json").read_text()
    )
    source_root = Path(marker["source_path"])
    registry = CodeSourceRegistry.load(Path("configs/sources/code.yaml"))
    service = CodeBuildService(
        registry,
        code_root,
        RagConfig.load(Path("configs/rag/default.yaml")),
    )
    library = registry.get("transformers")
    license_value = service._license(source_root)
    frozen = [
        json.loads(line)
        for line in (code_root / "normalized/transformers.jsonl").read_text().splitlines()
        if line.strip()
    ]
    documents = []
    connection = sqlite3.connect(
        f"file:{rag_root / 'state/index.sqlite3'}?mode=ro", uri=True
    )
    try:
        for record in frozen:
            relative = str(record["metadata"]["path"])
            document = service._file_document(
                library,
                marker,
                source_root / relative,
                relative,
                license_value,
            )
            row = connection.execute(
                "SELECT metadata_hash FROM documents WHERE document_id=? AND active=1",
                (document.document_id,),
            ).fetchone()
            if row is None or row[0] != sha256_json(document.metadata):
                raise RuntimeError(f"state identity mismatch: {document.document_id}")
            documents.append(document.to_dict(include_content=False))
    finally:
        connection.close()
    if len(documents) != 20:
        raise RuntimeError(f"expected 20 frozen records, got {len(documents)}")
    target = code_root / "normalized/transformers/5.13.1.jsonl"
    if args.execute:
        atomic_write_jsonl(target, documents, sort_key=lambda item: item["document_id"])
    print({"records": len(documents), "target": str(target), "execute": args.execute})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
