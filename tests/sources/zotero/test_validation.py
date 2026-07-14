from __future__ import annotations

import json

from knowledgehub.manifests.writer import write_snapshot
from knowledgehub.sources.zotero.state import ZoteroStateStore
from knowledgehub.sources.zotero.validation import ValidationReport, validate_source


def _initialized_empty_source(config) -> ZoteroStateStore:
    config.prepare_runtime()
    store = ZoteroStateStore(config.data_dir)
    store.initialize()
    store.bind_library(config.library_type, int(config.library_id))
    write_snapshot(config.data_dir / "manifests" / "documents.jsonl", [])
    return store


def test_missing_state_is_reported_without_exception(zotero_config_factory) -> None:
    config = zotero_config_factory()

    report = validate_source(config)

    assert not report.valid
    assert [issue.code for issue in report.issues] == ["missing_state"]
    assert report.to_dict()["valid"] is False


def test_empty_initialized_source_validates_cleanly(zotero_config_factory) -> None:
    config = zotero_config_factory()
    _initialized_empty_source(config)

    report = validate_source(config)

    assert report.valid
    assert report.issues == []
    assert report.checks["sqlite_quick_check"] == ["ok"]
    assert report.checks["library"] == {"type": "user", "id": 42, "version": 0}
    assert report.checks["current_document_count"] == 0
    assert report.checks["pending_publish_intents"] == []


def test_library_mismatch_and_pending_publication_are_errors(
    zotero_config_factory,
) -> None:
    config = zotero_config_factory()
    _initialized_empty_source(config)
    intent = config.data_dir / "runs" / "abandoned" / "publish-intent.json"
    intent.parent.mkdir(parents=True)
    intent.write_text("{}", encoding="utf-8")

    report = validate_source(zotero_config_factory(data_dir=config.data_dir, library_id=99))
    codes = {issue.code for issue in report.issues}

    assert not report.valid
    assert {"library_mismatch", "pending_publication"} <= codes
    assert report.checks["pending_publish_intents"] == [str(intent)]

    type_report = validate_source(
        zotero_config_factory(
            data_dir=config.data_dir,
            webdav_dir=config.webdav_dir,
            library_type="group",
            library_id=42,
        )
    )
    assert "library_mismatch" in {issue.code for issue in type_report.issues}


def test_snapshot_json_order_duplicates_and_fingerprint_are_checked(
    zotero_config_factory,
) -> None:
    config = zotero_config_factory()
    _initialized_empty_source(config)
    record = {
        "document_id": "zotero:user:42:B:B:0",
        "metadata_fingerprint": "wrong",
        "document_fingerprint": "wrong",
        "content_fingerprint": None,
        "status": "missing_archive",
        # Corrupt optional arrays must produce validation issues, never crash
        # the validator while it tries to recompute fingerprints.
        "creators": None,
        "collections": [],
    }
    other = {**record, "document_id": "zotero:user:42:A:A:0"}
    path = config.data_dir / "manifests" / "documents.jsonl"
    path.write_text(
        "\n".join(json.dumps(value) for value in (record, other, other)) + "\n",
        encoding="utf-8",
    )

    report = validate_source(config)
    codes = {issue.code for issue in report.issues}

    assert not report.valid
    assert {
        "fingerprint_mismatch",
        "duplicate_document_id",
        "snapshot_order",
        "document_set_mismatch",
    } <= codes


def test_invalid_delta_order_and_unknown_document_are_reported(
    zotero_config_factory,
) -> None:
    config = zotero_config_factory()
    _initialized_empty_source(config)
    delta = config.data_dir / "manifests" / "deltas" / "sync.jsonl"
    values = [
        {"document_id": "zotero:user:42:B:B:0"},
        {"document_id": "zotero:user:42:A:A:0"},
        {"document_id": "zotero:user:42:A:A:0"},
    ]
    delta.write_text("\n".join(json.dumps(value) for value in values) + "\n", encoding="utf-8")

    report = validate_source(config)
    codes = [issue.code for issue in report.issues]

    assert report.checks["delta_records"] == 3
    assert codes.count("unknown_delta_document") == 3
    assert "delta_order" in codes


def test_orphan_raw_attachment_is_reported_even_without_projection(
    zotero_config_factory,
) -> None:
    config = zotero_config_factory()
    store = _initialized_empty_source(config)
    with store.transaction() as connection:
        store.upsert_remote_object(
            connection,
            "item",
            {
                "key": "ORPHAN",
                "version": 1,
                "data": {
                    "key": "ORPHAN",
                    "version": 1,
                    "itemType": "attachment",
                    "parentItem": "MISSING",
                    "contentType": "text/plain",
                },
            },
        )

    report = validate_source(config)

    assert not report.valid
    assert report.checks["missing_parent_relations"] == [
        {"attachment_key": "ORPHAN", "parent_item_key": "MISSING"}
    ]
    assert "missing_parent" in {issue.code for issue in report.issues}


def test_validation_report_warnings_do_not_make_report_invalid() -> None:
    report = ValidationReport()
    report.add("warning", "mapping_unverified", "message", "suggestion", sample_count=0)

    assert report.valid
    assert report.to_dict()["issues"] == [
        {
            "severity": "warning",
            "code": "mapping_unverified",
            "message": "message",
            "suggestion": "suggestion",
            "context": {"sample_count": 0},
        }
    ]


def test_corrupt_attachment_projection_is_reported_without_validator_crash(
    zotero_config_factory,
) -> None:
    config = zotero_config_factory()
    store = _initialized_empty_source(config)
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO attachments(
                attachment_key, attachment_version, prop_exists, resolver_status,
                pdf_candidates_json, updated_at
            ) VALUES ('A', 1, 0, 'missing_archive', '{', 'now')
            """
        )

    report = validate_source(config)

    assert not report.valid
    assert {issue.code for issue in report.issues} == {"invalid_state_data"}


def test_unreadable_utf8_snapshot_and_delta_are_structured_validation_errors(
    zotero_config_factory,
) -> None:
    config = zotero_config_factory()
    _initialized_empty_source(config)
    snapshot = config.data_dir / "manifests" / "documents.jsonl"
    snapshot.write_bytes(b"\xff\n")
    delta = config.data_dir / "manifests" / "deltas" / "broken.jsonl"
    delta.write_bytes(b"\xff\n")

    report = validate_source(config)
    codes = {issue.code for issue in report.issues}

    assert not report.valid
    assert {"invalid_snapshot_file", "invalid_delta_file"} <= codes
