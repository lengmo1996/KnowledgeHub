"""Read-only catalog over stable manifests and fixed SQLite databases."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterator


def _ro_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


class ReadOnlyCatalog:
    def __init__(self, manifest_path: Path, pipeline_path: Path, zotero_path: Path) -> None:
        self.manifest_path = manifest_path
        self.pipeline_path = pipeline_path
        self.zotero_path = zotero_path
        self._manifest_mtime_ns = -1
        self._documents: dict[str, dict[str, Any]] = {}

    def _reload_manifest(self) -> None:
        stat = self.manifest_path.stat()
        if stat.st_mtime_ns == self._manifest_mtime_ns:
            return
        documents: dict[str, dict[str, Any]] = {}
        with self.manifest_path.open(encoding="utf-8") as stream:
            for line in stream:
                value = json.loads(line)
                if isinstance(value, dict) and value.get("document_id"):
                    documents[str(value["document_id"])] = value
        self._documents = documents
        self._manifest_mtime_ns = stat.st_mtime_ns

    def _values(self) -> Iterator[dict[str, Any]]:
        self._reload_manifest()
        return iter(self._documents.values())

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        self._reload_manifest()
        manifest = self._documents.get(document_id)
        if manifest is None:
            return None
        safe_keys = {
            "abstract",
            "attachment_key",
            "collections",
            "creators",
            "date",
            "document_fingerprint",
            "document_id",
            "doi",
            "item_key",
            "language",
            "metadata_fingerprint",
            "publication_title",
            "source",
            "status",
            "tags",
            "title",
            "updated_at",
            "year",
        }
        result = {key: manifest.get(key) for key in sorted(safe_keys)}
        with _ro_connection(self.pipeline_path) as connection:
            row = connection.execute(
                """
                SELECT source_document_fingerprint, source_metadata_fingerprint,
                       source_content_fingerprint, parse_fingerprint, chunk_fingerprint,
                       embedding_fingerprint, chunk_count, last_processed_at
                FROM pipeline_documents WHERE document_id=?
                """,
                (document_id,),
            ).fetchone()
            chunks = connection.execute(
                """
                SELECT chunk_id, chunk_index, page_start, page_end, section_path_json,
                       token_count, text_sha256, chunk_fingerprint
                FROM chunks WHERE document_id=? AND active=1 ORDER BY chunk_index
                """,
                (document_id,),
            ).fetchall()
        result["pipeline"] = dict(row) if row else None
        chunk_values: list[dict[str, Any]] = [
            {
                **dict(value),
                "section_path": json.loads(value["section_path_json"]),
                "page_numbers": _page_numbers(value["page_start"], value["page_end"]),
            }
            for value in chunks
        ]
        for value in chunk_values:
            value.pop("section_path_json", None)
        result["chunks"] = chunk_values
        return result

    def neighbor_ids(self, chunk_id: str, *, before: int, after: int) -> list[str]:
        with _ro_connection(self.pipeline_path) as connection:
            row = connection.execute(
                "SELECT document_id, chunk_index FROM chunks WHERE chunk_id=? AND active=1",
                (chunk_id,),
            ).fetchone()
            if row is None:
                return []
            rows = connection.execute(
                """
                SELECT chunk_id FROM chunks
                WHERE document_id=? AND active=1 AND chunk_index BETWEEN ? AND ?
                ORDER BY chunk_index
                """,
                (
                    row["document_id"],
                    int(row["chunk_index"]) - before,
                    int(row["chunk_index"]) + after,
                ),
            ).fetchall()
        return [str(value["chunk_id"]) for value in rows]

    def resolve_reference(
        self,
        *,
        doi: str | None = None,
        citation_key: str | None = None,
        item_key: str | None = None,
        attachment_key: str | None = None,
        title: str | None = None,
    ) -> list[dict[str, Any]]:
        self._reload_manifest()
        citation_items = self._citation_item_keys(citation_key) if citation_key else set()
        needle_title = (title or "").casefold().strip()
        result = []
        for value in self._documents.values():
            matches = (
                (doi and str(value.get("doi") or "").casefold() == doi.casefold())
                or (item_key and value.get("item_key") == item_key)
                or (attachment_key and value.get("attachment_key") == attachment_key)
                or (needle_title and needle_title in str(value.get("title") or "").casefold())
                or (citation_items and value.get("item_key") in citation_items)
            )
            if matches:
                result.append(
                    {
                        "document_id": value.get("document_id"),
                        "title": value.get("title"),
                        "doi": value.get("doi"),
                        "item_key": value.get("item_key"),
                        "attachment_key": value.get("attachment_key"),
                        "year": value.get("year"),
                    }
                )
        return sorted(result, key=lambda value: (str(value["title"]), str(value["document_id"])))[
            :50
        ]

    def _citation_item_keys(self, citation_key: str) -> set[str]:
        with _ro_connection(self.zotero_path) as connection:
            rows = connection.execute(
                """
                SELECT object_key FROM objects
                WHERE lower(json_extract(raw_json, '$.data.citationKey'))=lower(?)
                LIMIT 51
                """,
                (citation_key,),
            ).fetchall()
        return {str(value["object_key"]) for value in rows}

    def list_facets(self, facet: str, *, cursor: str | None, limit: int) -> dict[str, Any]:
        if facet not in {"collection", "tag", "year", "source"}:
            raise ValueError("unsupported facet")
        counts: Counter[str] = Counter()
        for document in self._values():
            if facet == "collection":
                values = [
                    str(value.get("path") or value.get("name") or value.get("key"))
                    for value in document.get("collections", [])
                    if isinstance(value, dict)
                ]
            elif facet == "tag":
                values = [str(value) for value in document.get("tags", [])]
            else:
                raw = document.get(facet)
                values = [str(raw)] if raw not in {None, ""} else []
            counts.update(value for value in values if value)
        ordered = sorted(counts.items(), key=lambda value: (-value[1], value[0]))
        offset = int(cursor or "0")
        page = ordered[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(ordered) else None
        return {
            "facet": facet,
            "values": [{"value": value, "count": count} for value, count in page],
            "next_cursor": next_cursor,
        }

    def status(self) -> dict[str, Any]:
        self._reload_manifest()
        with _ro_connection(self.pipeline_path) as connection:
            pipeline_chunks = int(connection.execute("SELECT count(*) FROM chunks").fetchone()[0])
            chunk_count = int(
                connection.execute("SELECT count(*) FROM chunks WHERE active=1").fetchone()[0]
            )
        return {
            "documents": len(self._documents),
            "pipeline_chunks": pipeline_chunks,
            "active_pipeline_chunks": chunk_count,
        }


def _page_numbers(start: int | None, end: int | None) -> list[int]:
    if start is None or end is None:
        return []
    return list(range(int(start), int(end) + 1))
