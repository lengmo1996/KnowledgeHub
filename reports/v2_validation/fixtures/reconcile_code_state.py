"""Reconcile local Code index state to a verified Qdrant snapshot.

This bounded recovery helper is intentionally fail-closed. It only removes
local state rows and chunk artifacts that are absent from the restored Qdrant
collection, and only after exact point/document-count checks pass.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from qdrant_client import QdrantClient

from knowledgehub.core.hashing import sha256_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--expected-points", type=int, required=True)
    parser.add_argument("--expected-documents", type=int, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    if args.execute != args.yes:
        parser.error("execution requires both --execute and --yes")

    client = QdrantClient(url="http://127.0.0.1:6333")
    offset = None
    point_count = 0
    qdrant_ids: set[str] = set()
    while True:
        points, offset = client.scroll(
            args.collection,
            limit=256,
            offset=offset,
            with_payload=["document_id"],
            with_vectors=False,
        )
        point_count += len(points)
        for point in points:
            document_id = str((point.payload or {}).get("document_id") or "")
            if not document_id:
                raise RuntimeError("Qdrant point is missing document_id")
            qdrant_ids.add(document_id)
        if offset is None:
            break
    client.close()
    if point_count != args.expected_points or len(qdrant_ids) != args.expected_documents:
        raise RuntimeError(
            f"snapshot identity mismatch: points={point_count}, documents={len(qdrant_ids)}"
        )

    state_path = args.data_dir / "state" / "index.sqlite3"
    connection = sqlite3.connect(state_path)
    try:
        state_ids = {
            str(row[0]) for row in connection.execute("SELECT document_id FROM documents")
        }
        missing_state = sorted(qdrant_ids - state_ids)
        if missing_state:
            raise RuntimeError(f"restored Qdrant documents missing local state: {missing_state[:5]}")
        extra_state = sorted(state_ids - qdrant_ids)
        expected_artifacts = {
            f"{sha256_json(document_id)[:32]}.jsonl" for document_id in qdrant_ids
        }
        chunk_dir = args.data_dir / "chunks"
        extra_artifacts = sorted(
            path for path in chunk_dir.glob("*.jsonl") if path.name not in expected_artifacts
        )
        result = {
            "collection": args.collection,
            "qdrant_points": point_count,
            "qdrant_documents": len(qdrant_ids),
            "state_before": len(state_ids),
            "extra_state_rows": len(extra_state),
            "extra_chunk_artifacts": len(extra_artifacts),
            "execute": args.execute,
        }
        if args.execute:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "DELETE FROM documents WHERE document_id=?",
                ((document_id,) for document_id in extra_state),
            )
            connection.executemany(
                "DELETE FROM tombstones WHERE document_id=?",
                ((document_id,) for document_id in extra_state),
            )
            connection.commit()
            for path in extra_artifacts:
                path.unlink()
            result["state_after"] = connection.execute(
                "SELECT COUNT(*) FROM documents"
            ).fetchone()[0]
            result["artifacts_after"] = len(list(chunk_dir.glob("*.jsonl")))
        print(result)
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
