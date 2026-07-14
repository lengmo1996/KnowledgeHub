"""Qdrant schema validation, idempotent replacement and hybrid search."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from knowledgehub.pipeline.models import ChunkRecord


class QdrantSchemaError(RuntimeError):
    pass


class QdrantIndex:
    def __init__(
        self,
        url: str,
        collection: str,
        *,
        dense_dim: int,
        client: Any | None = None,
    ) -> None:
        if client is None:
            from qdrant_client import QdrantClient

            client = QdrantClient(url=url, timeout=60)
        self.client = client
        self.collection = collection
        self.dense_dim = dense_dim

    def ensure_collection(self) -> None:
        from qdrant_client import models

        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config={
                    "dense": models.VectorParams(
                        size=self.dense_dim, distance=models.Distance.COSINE, on_disk=True
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
        dense = vectors.get("dense") if isinstance(vectors, Mapping) else None
        sparse = info.config.params.sparse_vectors
        if dense is None or int(dense.size) != self.dense_dim or "bm25" not in (sparse or {}):
            raise QdrantSchemaError(
                f"collection {self.collection} schema does not match dense={self.dense_dim}+bm25"
            )

    def replace_document(
        self,
        document_id: str,
        chunks: Sequence[ChunkRecord],
        dense_vectors: Sequence[Sequence[float]],
        sparse_vectors: Sequence[tuple[Sequence[int], Sequence[float]]],
    ) -> None:
        from qdrant_client import models

        if not (len(chunks) == len(dense_vectors) == len(sparse_vectors)):
            raise ValueError("chunk, dense and sparse batch sizes differ")
        self.delete_document(document_id)
        points = []
        for chunk, dense, sparse in zip(chunks, dense_vectors, sparse_vectors):
            if len(dense) != self.dense_dim:
                raise ValueError("dense vector dimension mismatch")
            payload = {
                **dict(chunk.metadata),
                "attachment_key": chunk.attachment_key,
                "chunk_id": chunk.chunk_id,
                "chunk_index": chunk.chunk_index,
                "document_id": chunk.document_id,
                "page_end": chunk.page_end,
                "page_start": chunk.page_start,
                "section_path": list(chunk.section_path),
                "text": chunk.text,
                "text_sha256": chunk.text_sha256,
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
        if points:
            self.client.upsert(collection_name=self.collection, points=points, wait=True)

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

    def update_payload(self, document_id: str, payload: Mapping[str, Any]) -> None:
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

    def query(
        self,
        *,
        dense: Sequence[float] | None,
        sparse: tuple[Sequence[int], Sequence[float]],
        mode: str,
        limit: int,
        prefetch_limit: int,
        query_filter: Any | None = None,
    ) -> list[Any]:
        from qdrant_client import models

        sparse_query = models.SparseVector(indices=list(sparse[0]), values=list(sparse[1]))
        if mode == "sparse":
            response = self.client.query_points(
                self.collection,
                query=sparse_query,
                using="bm25",
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
        elif mode == "hybrid":
            if dense is None:
                raise ValueError("hybrid search requires a dense query vector")
            response = self.client.query_points(
                self.collection,
                prefetch=[
                    models.Prefetch(
                        query=list(dense), using="dense", limit=prefetch_limit, filter=query_filter
                    ),
                    models.Prefetch(
                        query=sparse_query,
                        using="bm25",
                        limit=prefetch_limit,
                        filter=query_filter,
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=limit,
                with_payload=True,
            )
        else:
            raise ValueError(f"unsupported query mode: {mode}")
        return list(response.points)
