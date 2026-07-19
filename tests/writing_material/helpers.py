from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from knowledgehub.core.hashing import sha256_text
from knowledgehub.pipeline.artifacts import safe_document_name

DOCUMENT_ID = "zotero:user:42:ITEMKEY:ATTACHKEY:0"
PARAGRAPH_TEXT = (
    "However, prior approaches remain limited under distribution shift. "
    "We therefore evaluate a scoped extension against the same baseline."
)


def build_literature_fixture(root: Path, *, document_count: int = 1) -> Path:
    if document_count <= 0:
        raise ValueError("document_count must be positive")
    state = root / "state"
    state.mkdir(parents=True)
    connection = sqlite3.connect(state / "pipeline.sqlite3")
    connection.execute(
        """CREATE TABLE pipeline_documents (
        document_id TEXT PRIMARY KEY, source_status TEXT, parse_status TEXT,
        parser_name TEXT, parser_version TEXT, parse_fingerprint TEXT,
        source_content_fingerprint TEXT, attachment_key TEXT, metadata_json TEXT
        )"""
    )
    connection.execute(
        """CREATE TABLE consumed_deltas (
        source TEXT, sequence INTEGER, sync_id TEXT, delta_sha256 TEXT, status TEXT
        )"""
    )
    connection.execute(
        "INSERT INTO consumed_deltas VALUES ('zotero',1,'sync-1',?,'success')",
        ("d" * 64,),
    )
    parsed_json = root / "parsed" / "json"
    parsed_markdown = root / "parsed" / "markdown"
    parsed_json.mkdir(parents=True)
    parsed_markdown.mkdir(parents=True)
    for index in range(document_count):
        suffix = "" if index == 0 else f"{index:02d}"
        item_key = f"ITEMKEY{suffix}"
        attachment_key = f"ATTACHKEY{suffix}"
        document_id = f"zotero:user:42:{item_key}:{attachment_key}:0"
        parse_fingerprint = f"parse-{index + 1}"
        connection.execute(
            "INSERT INTO pipeline_documents VALUES (?,?,?,?,?,?,?,?,?)",
            (
                document_id,
                "ready",
                "ready",
                "docling",
                "2.112.0",
                parse_fingerprint,
                f"source-{index + 1}",
                attachment_key,
                json.dumps(
                    {
                        "item_key": item_key,
                        "attachment_key": attachment_key,
                        "title": f"Fixture paper {index + 1}",
                        "collections": [
                            {
                                "key": "COLLKEY",
                                "name": "Fixture Collection",
                                "path": "Tests/Fixture",
                            }
                        ],
                    }
                ),
            ),
        )
        name = safe_document_name(document_id)
        payload = {
            "document_id": document_id,
            "page_count": 1,
            "parse_fingerprint": parse_fingerprint,
            "parser_name": "docling",
            "parser_version": "2.112.0",
            "structured": {
                "schema_name": "DoclingDocument",
                "version": "1.10.0",
                "body": {"children": [{"cref": "#/texts/0"}, {"cref": "#/texts/1"}]},
                "groups": [],
                "texts": [
                    {
                        "self_ref": "#/texts/0",
                        "label": "section_header",
                        "orig": "Introduction",
                        "text": "Introduction",
                        "prov": [],
                    },
                    {
                        "self_ref": "#/texts/1",
                        "label": "text",
                        "orig": PARAGRAPH_TEXT,
                        "text": PARAGRAPH_TEXT,
                        "prov": [
                            {
                                "page_no": 1,
                                "charspan": [0, len(PARAGRAPH_TEXT)],
                                "bbox": {"l": 10.0, "t": 20.0, "r": 500.0, "b": 40.0},
                            }
                        ],
                    },
                ],
            },
        }
        (parsed_json / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
        (parsed_markdown / f"{name}.md").write_text(
            f"# Introduction\n\n{PARAGRAPH_TEXT}\n", encoding="utf-8"
        )
    connection.commit()
    connection.close()
    return root


def write_runtime_contract(root: Path) -> tuple[Path, Path, Path]:
    taxonomy = root / "taxonomy.yaml"
    classify = root / "classify.md"
    abstract = root / "abstract.md"
    source_taxonomy = Path("configs/writing/taxonomy-v1.yaml")
    taxonomy.write_text(source_taxonomy.read_text(encoding="utf-8"), encoding="utf-8")
    classify.write_text("classify exact evidence", encoding="utf-8")
    abstract.write_text("abstract verified evidence", encoding="utf-8")
    return taxonomy, classify, abstract


def based_on(value: dict[str, object]) -> str:
    from knowledgehub.core.hashing import sha256_json

    return sha256_json(value)


def paragraph_hash() -> str:
    return sha256_text(PARAGRAPH_TEXT)
