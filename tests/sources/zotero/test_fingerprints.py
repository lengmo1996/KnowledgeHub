from __future__ import annotations

import pytest

from knowledgehub.sources.zotero.fingerprints import (
    DeltaReason,
    build_fingerprints,
    canonical_json,
    chunk_required,
    content_fingerprint,
    document_fingerprint,
    metadata_fingerprint,
    normalise_metadata,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def _metadata() -> dict[str, object]:
    return {
        "key": "IGNORED",
        "version": 42,
        "data": {
            "title": "  A   Paper ",
            "creators": [
                {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"},
                {"creatorType": "author", "name": "Example Institute"},
            ],
            "abstractNote": " An abstract. ",
            "publicationTitle": " Journal ",
            "date": "2026-03-02",
            "DOI": " 10.1000 / ABC ",
            "url": "https://example.test/paper",
            "language": "en",
            "rights": "CC-BY",
            "itemType": "journalArticle",
            "relations": {"dc:relation": ["B", "A", "A"]},
            "tags": [{"tag": "rag"}, {"tag": "AI"}, {"tag": "rag"}],
            "collection_refs": [
                {"key": "C2", "path": "Research/Two"},
                {"path": "Research/One", "key": "C1"},
            ],
            "dateModified": "ignored",
        },
        "updated_at": "ignored",
    }


def test_metadata_normalisation_covers_contract_fields() -> None:
    result = normalise_metadata(_metadata())

    assert result == {
        "title": "A Paper",
        "creators": [
            {
                "first_name": "Ada",
                "last_name": "Lovelace",
                "creator_type": "author",
            },
            {"name": "Example Institute", "creator_type": "author"},
        ],
        "abstract": "An abstract.",
        "publication": "Journal",
        "date": "2026-03-02",
        "year": "2026",
        "doi": "10.1000/abc",
        "url": "https://example.test/paper",
        "language": "en",
        "rights": "CC-BY",
        "item_type": "journalArticle",
        "relations": {"dc:relation": ["A", "B"]},
        "tags": ["AI", "rag"],
        "collections": [
            {"key": "C1", "path": "Research/One"},
            {"key": "C2", "path": "Research/Two"},
        ],
    }


def test_metadata_fingerprint_ignores_runtime_and_json_order() -> None:
    first = _metadata()
    second = _metadata()
    second["updated_at"] = "a different run"
    data = second["data"]
    assert isinstance(data, dict)
    data["dateModified"] = "tomorrow"
    # Tag and collection order are set semantics.
    data["tags"] = list(reversed(data["tags"]))  # type: ignore[arg-type]
    data["collection_refs"] = list(reversed(data["collection_refs"]))  # type: ignore[arg-type]

    assert metadata_fingerprint(first) == metadata_fingerprint(second)


def test_creator_order_changes_metadata_fingerprint() -> None:
    first = _metadata()
    second = _metadata()
    data = second["data"]
    assert isinstance(data, dict)
    data["creators"] = list(reversed(data["creators"]))  # type: ignore[arg-type]

    assert metadata_fingerprint(first) != metadata_fingerprint(second)


def test_content_fingerprint_is_pdf_hash_or_null() -> None:
    assert content_fingerprint(None) is None
    assert content_fingerprint(SHA_A.upper()) == SHA_A
    with pytest.raises(ValueError, match="64-character"):
        content_fingerprint("not-a-digest")


def test_document_fingerprint_includes_status_content_and_schema() -> None:
    metadata = metadata_fingerprint(_metadata())
    baseline = document_fingerprint(metadata, SHA_A, "ready")

    assert baseline == document_fingerprint(metadata, SHA_A, "ready")
    assert baseline != document_fingerprint(metadata, SHA_B, "ready")
    assert baseline != document_fingerprint(metadata, SHA_A, "missing_pdf")
    assert baseline != document_fingerprint(metadata, SHA_A, "ready", schema_version=2)


def test_build_fingerprints_returns_all_three_values() -> None:
    result = build_fingerprints(_metadata(), SHA_A, "ready")
    assert result.metadata == metadata_fingerprint(_metadata())
    assert result.content == SHA_A
    assert len(result.document) == 64


@pytest.mark.parametrize(
    "reason",
    [
        DeltaReason.NEW_DOCUMENT,
        DeltaReason.ATTACHMENT_BECAME_AVAILABLE,
        DeltaReason.CONTENT_CHANGED,
        DeltaReason.ATTACHMENT_REPLACED,
    ],
)
def test_chunk_policy_requires_ready_content(reason: DeltaReason) -> None:
    assert chunk_required(reason, ready=True)
    assert not chunk_required(reason, ready=False)


@pytest.mark.parametrize(
    "reason",
    [
        DeltaReason.ZOTERO_ITEM_DELETED,
        DeltaReason.ZOTERO_ATTACHMENT_DELETED,
        DeltaReason.ATTACHMENT_MISSING,
        DeltaReason.ATTACHMENT_BECAME_INVALID,
        DeltaReason.COLLECTION_CHANGED,
    ],
)
def test_chunk_policy_never_chunks_non_content_reasons(reason: DeltaReason) -> None:
    assert not chunk_required(reason, ready=True)
    assert not chunk_required(reason, ready=False)


def test_metadata_chunk_policy_is_configurable_but_defaults_off() -> None:
    assert not chunk_required(DeltaReason.METADATA_CHANGED, ready=True)
    assert chunk_required(
        DeltaReason.METADATA_CHANGED,
        ready=True,
        chunk_on_metadata_change=True,
    )
    assert not chunk_required(
        DeltaReason.METADATA_CHANGED,
        ready=False,
        chunk_on_metadata_change=True,
    )


def test_wire_values_match_manifest_contract() -> None:
    assert DeltaReason.ATTACHMENT_REPLACED.value == "attachment_replaced"
    assert DeltaReason.ATTACHMENT_MISSING.value == "attachment_missing"


def test_canonical_json_rejects_nan_and_is_stable() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    with pytest.raises(ValueError):
        canonical_json({"value": float("nan")})
