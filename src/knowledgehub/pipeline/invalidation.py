"""Central stage invalidation policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from knowledgehub.pipeline.models import SourceDocument


@dataclass(frozen=True, slots=True)
class InvalidationDecision:
    parse: bool
    chunk: bool
    embedding: bool
    dense_index: bool
    sparse_index: bool
    payload_only: bool
    reason: str


def decide_invalidation(
    previous: Mapping[str, Any] | None,
    current: SourceDocument,
    *,
    parse_fingerprint: str,
    chunk_fingerprint: str,
    embedding_fingerprint: str,
    sparse_fingerprint: str,
) -> InvalidationDecision:
    if previous is None:
        return InvalidationDecision(True, True, True, True, True, False, "new_document")
    content_changed = (
        previous.get("source_content_fingerprint") != current.source_content_fingerprint
        or previous.get("pdf_sha256") != current.pdf_sha256
        or previous.get("source_status") != "ready"
    )
    if content_changed or previous.get("parse_fingerprint") != parse_fingerprint:
        return InvalidationDecision(True, True, True, True, True, False, "content_or_parser")
    if previous.get("chunk_fingerprint") != chunk_fingerprint:
        return InvalidationDecision(False, True, True, True, True, False, "chunk_config")
    if previous.get("embedding_fingerprint") != embedding_fingerprint:
        return InvalidationDecision(False, False, True, True, False, False, "embedding_config")
    metadata_changed = (
        previous.get("source_metadata_fingerprint") != current.source_metadata_fingerprint
    )
    if metadata_changed:
        return InvalidationDecision(False, False, False, False, False, True, "metadata")
    sparse_stale = previous.get("sparse_index_status") != f"ready:{sparse_fingerprint}"
    if sparse_stale:
        return InvalidationDecision(False, False, False, False, True, False, "sparse_config")
    return InvalidationDecision(False, False, False, False, False, False, "unchanged")
