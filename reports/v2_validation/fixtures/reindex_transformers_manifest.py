"""Reindex only the 20 restored Transformers documents after snapshot recovery."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from knowledgehub.code_rag.build import CodeBuildService
from knowledgehub.code_rag.registry import CodeSourceRegistry
from knowledgehub.core.hashing import sha256_json
from knowledgehub.indexing.incremental import IncrementalChunkIndexer, IndexInput
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
    rag_config = RagConfig.load(Path("configs/rag/default.yaml")).with_overrides(
        data_dir=rag_root,
        qdrant_collection="knowledgehub_code_qwen3_4b_1024_v1",
        embedding_query_instruction=(
            "Given a software engineering question, retrieve versioned official "
            "documentation, examples, source code and release evidence."
        ),
    )
    service = CodeBuildService(registry, code_root, rag_config)
    library = registry.get("transformers")
    license_value = service._license(source_root)
    records = [
        json.loads(line)
        for line in (code_root / "normalized/transformers/5.13.1.jsonl")
        .read_text()
        .splitlines()
        if line.strip()
    ]
    inputs: list[IndexInput] = []
    for record in records:
        relative = str(record["metadata"]["path"])
        document = service._file_document(
            library, marker, source_root / relative, relative, license_value
        )
        chunks = service.chunker.chunk(document)
        artifact = rag_root / "chunks" / f"{sha256_json(document.document_id)[:32]}.jsonl"
        observed = {
            json.loads(line)["chunk_id"]
            for line in artifact.read_text().splitlines()
            if line.strip()
        }
        generated = {chunk.chunk_id for chunk in chunks}
        if observed != generated:
            raise RuntimeError(f"local chunk identity mismatch: {document.document_id}")
        inputs.append(IndexInput(document, chunks, service.chunker.version))
    if len(inputs) != 20:
        raise RuntimeError(f"expected 20 inputs, got {len(inputs)}")
    result = {
        "documents": len(inputs),
        "chunks": sum(len(value.chunks) for value in inputs),
        "collection": rag_config.qdrant_collection,
        "execute": args.execute,
    }
    if args.execute:
        state_path = rag_root / "state/index.sqlite3"
        with sqlite3.connect(state_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "DELETE FROM documents WHERE document_id=?",
                ((value.document.document_id,) for value in inputs),
            )
        indexer = IncrementalChunkIndexer(rag_config)
        try:
            summary = indexer.build(inputs, knowledge_base="code")
        finally:
            indexer.close()
        if summary.failures or summary.indexed != 20:
            raise RuntimeError(f"bounded reindex failed: {summary.to_dict()}")
        result["summary"] = summary.to_dict()
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
