"""Canonical Zotero document fingerprints and downstream chunk policy."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DeltaReason(str, Enum):
    ZOTERO_ITEM_DELETED = "zotero_item_deleted"
    ZOTERO_ATTACHMENT_DELETED = "zotero_attachment_deleted"
    NEW_DOCUMENT = "new_document"
    ATTACHMENT_BECAME_AVAILABLE = "attachment_became_available"
    ATTACHMENT_MISSING = "attachment_missing"
    ATTACHMENT_BECAME_MISSING = "attachment_missing"
    ATTACHMENT_BECAME_INVALID = "attachment_became_invalid"
    CONTENT_CHANGED = "content_changed"
    ATTACHMENT_REPLACED = "attachment_replaced"
    ARCHIVE_REPLACED = "attachment_replaced"
    COLLECTION_CHANGED = "collection_changed"
    METADATA_CHANGED = "metadata_changed"


@dataclass(frozen=True, slots=True)
class Fingerprints:
    metadata: str
    content: str | None
    document: str


def canonical_json(value: object) -> str:
    """Serialize a JSON value in the single representation used for hashing."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _first(data: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _normalise_creator(value: object) -> dict[str, str] | None:
    if isinstance(value, str):
        name = _clean_text(value)
        return {"name": name} if name else None
    if not isinstance(value, Mapping):
        return None
    creator_type = _clean_text(value.get("creatorType"))
    organisation = _clean_text(value.get("name"))
    if organisation:
        result = {"name": organisation}
    else:
        first_name = _clean_text(value.get("firstName"))
        last_name = _clean_text(value.get("lastName"))
        if not first_name and not last_name:
            return None
        result = {}
        if first_name:
            result["first_name"] = first_name
        if last_name:
            result["last_name"] = last_name
    if creator_type:
        result["creator_type"] = creator_type
    return result


def _normalise_creators(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    # Creator order carries author semantics and must not be sorted.
    result: list[dict[str, str]] = []
    for creator in value:
        normalised = _normalise_creator(creator)
        if normalised is not None:
            result.append(normalised)
    return result


def _normalise_tags(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    tags: set[str] = set()
    for raw in value:
        candidate = raw.get("tag") if isinstance(raw, Mapping) else raw
        tag = _clean_text(candidate)
        if tag:
            tags.add(tag)
    return sorted(tags)


def _normalise_collection_refs(value: object) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    refs: dict[str, object] = {}
    for raw in value:
        if isinstance(raw, str):
            cleaned = _clean_text(raw)
            if cleaned:
                refs[canonical_json(cleaned)] = cleaned
        elif isinstance(raw, Mapping):
            ref: dict[str, str] = {}
            key = _clean_text(raw.get("key"))
            path = _clean_text(raw.get("path"))
            if key:
                ref["key"] = key
            if path:
                ref["path"] = path
            if ref:
                refs[canonical_json(ref)] = ref
    return [refs[key] for key in sorted(refs)]


def _normalise_json(value: object, *, sort_lists: bool = False) -> object:
    """Reduce relation data to deterministic JSON-compatible values."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clean_text(value) or ""
    if isinstance(value, Mapping):
        return {
            str(key): _normalise_json(item, sort_lists=sort_lists)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = [_normalise_json(item, sort_lists=sort_lists) for item in value]
        if sort_lists:
            unique = {canonical_json(item): item for item in items}
            return [unique[key] for key in sorted(unique)]
        return items
    return str(value)


def _normalise_relations(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): _normalise_json(item, sort_lists=True)
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
    }


def normalise_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    """Return the exact metadata projection included in the v1 fingerprint."""

    nested = metadata.get("data")
    data = nested if isinstance(nested, Mapping) else metadata
    date = _clean_text(_first(data, "date"))
    year = _clean_text(_first(data, "year"))
    if year is None and date:
        match = re.search(r"(?<!\d)(\d{4})(?!\d)", date)
        year = match.group(1) if match else None
    doi_value = _clean_text(_first(data, "DOI", "doi"))
    doi = re.sub(r"\s+", "", doi_value).lower() if doi_value else None

    return {
        "title": _clean_text(_first(data, "title")),
        "creators": _normalise_creators(_first(data, "creators", "authors")),
        "abstract": _clean_text(_first(data, "abstractNote", "abstract")),
        "publication": _clean_text(_first(data, "publicationTitle", "container_title", "journal")),
        "date": date,
        "year": year,
        "doi": doi,
        "url": _clean_text(_first(data, "url", "URL")),
        "language": _clean_text(_first(data, "language")),
        "rights": _clean_text(_first(data, "rights")),
        "item_type": _clean_text(_first(data, "itemType", "item_type", "type")),
        "relations": _normalise_relations(_first(data, "relations")),
        "tags": _normalise_tags(_first(data, "tags")),
        "collections": _normalise_collection_refs(_first(data, "collection_refs", "collections")),
    }


def metadata_fingerprint(metadata: Mapping[str, object]) -> str:
    return canonical_sha256(normalise_metadata(metadata))


def content_fingerprint(pdf_sha256: str | None) -> str | None:
    """The content fingerprint is the PDF SHA-256 itself, not a second hash."""

    if pdf_sha256 is None:
        return None
    normalised = pdf_sha256.strip().lower()
    if not _SHA256.fullmatch(normalised):
        raise ValueError("pdf_sha256 must be a 64-character hexadecimal digest")
    return normalised


def document_fingerprint(
    metadata_digest: str,
    content_digest: str | None,
    status: str | Enum,
    *,
    schema_version: int = SCHEMA_VERSION,
) -> str:
    """Hash all values that can alter downstream document semantics."""

    if not _SHA256.fullmatch(metadata_digest.lower()):
        raise ValueError("metadata_digest must be a SHA-256 digest")
    content = content_fingerprint(content_digest)
    status_value = status.value if isinstance(status, Enum) else str(status)
    return canonical_sha256(
        {
            "schema_version": schema_version,
            "metadata_fingerprint": metadata_digest.lower(),
            "content_fingerprint": content,
            "status": status_value,
        }
    )


def build_fingerprints(
    metadata: Mapping[str, object],
    pdf_sha256: str | None,
    status: str | Enum,
    *,
    schema_version: int = SCHEMA_VERSION,
) -> Fingerprints:
    metadata_digest = metadata_fingerprint(metadata)
    content_digest = content_fingerprint(pdf_sha256)
    return Fingerprints(
        metadata=metadata_digest,
        content=content_digest,
        document=document_fingerprint(
            metadata_digest,
            content_digest,
            status,
            schema_version=schema_version,
        ),
    )


_REQUIRES_READY_CONTENT = {
    DeltaReason.NEW_DOCUMENT.value,
    DeltaReason.ATTACHMENT_BECAME_AVAILABLE.value,
    DeltaReason.CONTENT_CHANGED.value,
    DeltaReason.ARCHIVE_REPLACED.value,
}


def chunk_required(
    reason: str | DeltaReason,
    *,
    ready: bool,
    chunk_on_metadata_change: bool = False,
) -> bool:
    """Single v1 policy for deciding whether a delta needs PDF chunking."""

    value = reason.value if isinstance(reason, DeltaReason) else str(reason)
    if value == DeltaReason.METADATA_CHANGED.value:
        return bool(ready and chunk_on_metadata_change)
    return bool(ready and value in _REQUIRES_READY_CONTENT)


__all__ = [
    "SCHEMA_VERSION",
    "DeltaReason",
    "Fingerprints",
    "build_fingerprints",
    "canonical_json",
    "canonical_sha256",
    "chunk_required",
    "content_fingerprint",
    "document_fingerprint",
    "metadata_fingerprint",
    "normalise_metadata",
]
