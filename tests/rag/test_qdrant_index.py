from __future__ import annotations

from knowledgehub.core.hashing import sha256_text
from knowledgehub.indexing.qdrant import QdrantIndex
from knowledgehub.pipeline.models import ChunkRecord


class FakeQdrantClient:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.upsert_batches: list[list[object]] = []

    def delete(self, *, points_selector, **_kwargs) -> None:
        condition = points_selector.filter.must[0]
        self.deleted.append(str(condition.match.value))

    def upsert(self, *, points, **_kwargs) -> None:
        self.upsert_batches.append(list(points))


def _chunk(index: int) -> ChunkRecord:
    text = f"chunk {index}"
    return ChunkRecord(
        chunk_id=f"00000000-0000-0000-0000-{index:012d}",
        document_id="document-1",
        attachment_key="ATTACH01",
        chunk_index=index,
        text=text,
        text_sha256=sha256_text(text),
        chunk_fingerprint=sha256_text(f"fingerprint {index}"),
        token_count=2,
    )


def test_replace_document_splits_large_point_sets_into_bounded_requests() -> None:
    client = FakeQdrantClient()
    index = QdrantIndex.__new__(QdrantIndex)
    index.collection = "papers"
    index.dimension = 2
    index.upsert_batch_size = 2
    index.client = client
    chunks = [_chunk(value) for value in range(5)]

    index.replace_document(
        "document-1",
        chunks,
        [[1.0, 0.0] for _ in chunks],
        [([1], [1.0]) for _ in chunks],
    )

    assert client.deleted == ["document-1"]
    assert [len(batch) for batch in client.upsert_batches] == [2, 2, 1]
    assert [point.payload["chunk_index"] for batch in client.upsert_batches for point in batch] == [
        0,
        1,
        2,
        3,
        4,
    ]
