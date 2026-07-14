"""Atomic parsed-document and canonical Parquet artifact publication."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from knowledgehub.core.atomic import atomic_write_json, atomic_write_text, fsync_directory
from knowledgehub.core.hashing import sha256_text
from knowledgehub.pipeline.models import ChunkRecord, ParsedDocument


def safe_document_name(document_id: str) -> str:
    return sha256_text(document_id)[:32]


def write_parsed(data_dir: Path, parsed: ParsedDocument) -> tuple[Path, Path]:
    name = safe_document_name(parsed.document_id)
    json_path = data_dir / "parsed" / "json" / f"{name}.json"
    markdown_path = data_dir / "parsed" / "markdown" / f"{name}.md"
    atomic_write_json(
        json_path,
        {
            "document_id": parsed.document_id,
            "page_count": parsed.page_count,
            "parse_fingerprint": parsed.parse_fingerprint,
            "parser_name": parsed.parser_name,
            "parser_version": parsed.parser_version,
            "structured": parsed.structured,
        },
    )
    markdown = parsed.markdown.rstrip() + "\n"
    atomic_write_text(markdown_path, markdown)
    return json_path, markdown_path


def write_chunks_parquet(
    data_dir: Path, document_id: str, chunks: Sequence[ChunkRecord]
) -> Path:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("pyarrow is required to write canonical chunk artifacts") from exc

    output = data_dir / "chunks" / f"{safe_document_name(document_id)}.parquet"
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "attachment_key": chunk.attachment_key,
            "chunk_fingerprint": chunk.chunk_fingerprint,
            "chunk_id": chunk.chunk_id,
            "chunk_index": chunk.chunk_index,
            "document_id": chunk.document_id,
            "page_end": chunk.page_end,
            "page_start": chunk.page_start,
            "text": chunk.text,
            "text_sha256": chunk.text_sha256,
            "token_count": chunk.token_count,
            "metadata_json": json.dumps(
                chunk.metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "section_path_json": json.dumps(
                list(chunk.section_path), ensure_ascii=False, separators=(",", ":")
            ),
        }
        for chunk in chunks
    ]
    schema = pa.schema(
        [
            ("attachment_key", pa.string()),
            ("chunk_fingerprint", pa.string()),
            ("chunk_id", pa.string()),
            ("chunk_index", pa.int64()),
            ("document_id", pa.string()),
            ("page_end", pa.int64()),
            ("page_start", pa.int64()),
            ("text", pa.string()),
            ("text_sha256", pa.string()),
            ("token_count", pa.int64()),
            ("metadata_json", pa.string()),
            ("section_path_json", pa.string()),
        ]
    )
    table = pa.Table.from_pylist(rows, schema=schema)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        pq.write_table(table, temporary, compression="zstd")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        verified = pq.read_table(temporary)
        if verified.num_rows != len(chunks):
            raise RuntimeError("Parquet row count verification failed")
        os.replace(temporary, output)
        fsync_directory(output.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def read_chunks_parquet(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("pyarrow is required to read chunk artifacts") from exc
    table = pq.read_table(path)
    result: list[dict[str, Any]] = []
    for row in table.to_pylist():
        metadata = json.loads(row.pop("metadata_json"))
        section_path = json.loads(row.pop("section_path_json"))
        row["metadata"] = metadata
        row["section_path"] = section_path
        result.append(row)
    return result
