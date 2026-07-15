from __future__ import annotations

import json
import sqlite3

from knowledgehub.mcp.catalog import ReadOnlyCatalog


def test_catalog_reads_fixed_sources_and_pages(tmp_path) -> None:
    manifest = tmp_path / "documents.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "document_id": "doc-1",
                "item_key": "ITEM",
                "attachment_key": "ATT",
                "title": "A Paper",
                "doi": "10.1/example",
                "year": 2025,
                "source": "zotero",
                "tags": ["rag"],
                "collections": [{"key": "C", "name": "AI", "path": "Research/AI"}],
                "creators": [],
                "abstract": "abstract",
                "document_fingerprint": "d",
                "metadata_fingerprint": "m",
            }
        )
        + "\n"
    )
    pipeline = tmp_path / "pipeline.sqlite3"
    connection = sqlite3.connect(pipeline)
    connection.executescript(
        """
        CREATE TABLE pipeline_documents (
          document_id TEXT, source_document_fingerprint TEXT, source_metadata_fingerprint TEXT,
          source_content_fingerprint TEXT, parse_fingerprint TEXT, chunk_fingerprint TEXT,
          embedding_fingerprint TEXT, chunk_count INTEGER, last_processed_at TEXT
        );
        CREATE TABLE chunks (
          chunk_id TEXT, document_id TEXT, chunk_index INTEGER, page_start INTEGER, page_end INTEGER,
          section_path_json TEXT, token_count INTEGER, text_sha256 TEXT,
          chunk_fingerprint TEXT, active INTEGER
        );
        INSERT INTO pipeline_documents VALUES ('doc-1','sdf','smf','scf','pf','cf','ef',1,'now');
        INSERT INTO chunks VALUES ('chunk-1','doc-1',0,2,4,'["section"]',10,'sha','cf',1);
        """
    )
    connection.commit()
    connection.close()
    zotero = tmp_path / "zotero.sqlite3"
    connection = sqlite3.connect(zotero)
    connection.executescript(
        """
        CREATE TABLE objects (object_key TEXT, raw_json TEXT);
        INSERT INTO objects VALUES ('ITEM', '{"data":{"citationKey":"Smith2025"}}');
        """
    )
    connection.commit()
    connection.close()

    catalog = ReadOnlyCatalog(manifest, pipeline, zotero)
    document = catalog.get_document("doc-1")
    assert document["chunks"][0]["page_numbers"] == [2, 3, 4]
    assert catalog.neighbor_ids("chunk-1", before=1, after=1) == ["chunk-1"]
    assert catalog.resolve_reference(citation_key="Smith2025")[0]["document_id"] == "doc-1"
    assert catalog.list_facets("tag", cursor=None, limit=10)["values"] == [
        {"value": "rag", "count": 1}
    ]
