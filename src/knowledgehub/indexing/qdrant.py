"""Qdrant named-vector schema and idempotent per-document operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from knowledgehub.pipeline.models import ChunkRecord


class QdrantSchemaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SearchPoint:
    point_id: str
    score: float
    payload: Mapping[str, Any]


class QdrantIndex:
    def __init__(
        self,
        url: str,
        collection: str,
        dimension: int | None = None,
        *,
        dense_dim: int | None = None,
        upsert_batch_size: int = 32,
    ) -> None:
        from qdrant_client import QdrantClient

        self.collection = collection
        resolved_dimension = dimension if dimension is not None else dense_dim
        if resolved_dimension is None or resolved_dimension <= 0:
            raise ValueError("dense vector dimension must be positive")
        if upsert_batch_size <= 0:
            raise ValueError("upsert batch size must be positive")
        self.dimension = resolved_dimension
        self.upsert_batch_size = upsert_batch_size
        self.client: Any = QdrantClient(url=url, timeout=60)

    def ensure_collection(self) -> None:
        from qdrant_client import models

        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config={
                    "dense": models.VectorParams(
                        size=self.dimension, distance=models.Distance.COSINE, on_disk=True
                    )
                },
                sparse_vectors_config={
                    "bm25": models.SparseVectorParams(
                        index=models.SparseIndexParams(on_disk=True), modifier=models.Modifier.IDF
                    )
                },
            )
            return
        info = self.client.get_collection(self.collection)
        vectors = info.config.params.vectors
        sparse = info.config.params.sparse_vectors
        dense = vectors.get("dense") if isinstance(vectors, dict) else None
        if dense is None or int(dense.size) != self.dimension:
            raise QdrantSchemaError("existing collection dense vector schema does not match")
        if not isinstance(sparse, dict) or "bm25" not in sparse:
            raise QdrantSchemaError("existing collection sparse vector schema does not match")

    def delete_document(self, document_id: str) -> None:
        from qdrant_client import models

        self.client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="document_id", match=models.MatchValue(value=document_id)
                        )
                    ]
                )
            ),
            wait=True,
        )

    def replace_document(
        self,
        document_id: str,
        chunks: Sequence[ChunkRecord],
        dense_vectors: Sequence[Sequence[float]],
        sparse_vectors: Sequence[tuple[Sequence[int], Sequence[float]]],
        *,
        embedding_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        from qdrant_client import models

        if not (len(chunks) == len(dense_vectors) == len(sparse_vectors)):
            raise ValueError("chunk and vector counts do not match")
        self.delete_document(document_id)
        points = []
        for chunk, dense, sparse in zip(chunks, dense_vectors, sparse_vectors, strict=True):
            payload = {
                **dict(chunk.metadata),
                **dict(embedding_metadata or {}),
                "attachment_key": chunk.attachment_key,
                "chunk_id": chunk.chunk_id,
                "chunk_index": chunk.chunk_index,
                "document_id": chunk.document_id,
                "page_end": chunk.page_end,
                "page_start": chunk.page_start,
                "section_path": list(chunk.section_path),
                "text": chunk.text,
                "text_sha256": chunk.text_sha256,
                "token_count": chunk.token_count,
            }
            points.append(
                models.PointStruct(
                    id=chunk.chunk_id,
                    vector={
                        "dense": list(dense),
                        "bm25": models.SparseVector(
                            indices=list(sparse[0]), values=list(sparse[1])
                        ),
                    },
                    payload=payload,
                )
            )
        for offset in range(0, len(points), self.upsert_batch_size):
            self.client.upsert(
                collection_name=self.collection,
                points=points[offset : offset + self.upsert_batch_size],
                wait=True,
            )

    def update_document_payload(self, document_id: str, payload: Mapping[str, Any]) -> None:
        from qdrant_client import models

        self.client.set_payload(
            collection_name=self.collection,
            payload=dict(payload),
            points=models.Filter(
                must=[
                    models.FieldCondition(
                        key="document_id", match=models.MatchValue(value=document_id)
                    )
                ]
            ),
            wait=True,
        )

    def update_payload(self, document_id: str, payload: Mapping[str, Any]) -> None:
        self.update_document_payload(document_id, payload)

    def query(
        self,
        *,
        dense: Sequence[float] | None,
        sparse: tuple[Sequence[int], Sequence[float]],
        mode: str,
        limit: int,
        prefetch_limit: int,
        query_filter: Any = None,
    ) -> list[SearchPoint]:
        from qdrant_client import models

        sparse_vector = models.SparseVector(indices=list(sparse[0]), values=list(sparse[1]))
        if mode == "sparse":
            response = self.client.query_points(
                collection_name=self.collection,
                query=sparse_vector,
                using="bm25",
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
        elif mode == "hybrid":
            if dense is None:
                raise ValueError("hybrid query requires a dense vector")
            response = self.client.query_points(
                collection_name=self.collection,
                prefetch=[
                    models.Prefetch(query=list(dense), using="dense", limit=prefetch_limit),
                    models.Prefetch(query=sparse_vector, using="bm25", limit=prefetch_limit),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
        else:
            raise ValueError(f"unsupported query mode: {mode}")
        return [
            SearchPoint(
                point_id=str(value.id), score=float(value.score), payload=value.payload or {}
            )
            for value in response.points
        ]

    def count(self) -> int:
        return int(self.client.count(self.collection, exact=True).count)

    def snapshot(self) -> str:
        return str(self.client.create_snapshot(self.collection).name)
