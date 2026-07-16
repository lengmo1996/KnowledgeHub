from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from knowledgehub.core.hashing import sha256_text
from knowledgehub.core.models import KnowledgeDocument
from knowledgehub.hub.query import HubQueryRequest, HubQueryService, classify_code_intent
from knowledgehub.indexing.incremental import IncrementalChunkIndexer, IndexInput
from knowledgehub.pipeline.config import RagConfig
from knowledgehub.pipeline.models import ChunkRecord
from knowledgehub.retrieval.models import SearchHit, SearchResponse
from knowledgehub.writing_rag.v2 import WritingFeedbackStore


class Pool:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts):  # type: ignore[no-untyped-def]
        self.calls += 1
        return SimpleNamespace(vectors=tuple((1.0, 0.0) for _ in texts))

    def close(self) -> None:
        return None


class Sparse:
    def encode(self, texts):  # type: ignore[no-untyped-def]
        return [([1], [1.0]) for _ in texts]


class Index:
    def __init__(self) -> None:
        self.replaced: list[str] = []
        self.deleted: list[str] = []

    def ensure_collection(self) -> None:
        return None

    def replace_document(self, document_id, chunks, dense, sparse, **kwargs):  # type: ignore[no-untyped-def]
        self.replaced.append(document_id)

    def delete_document(self, document_id):  # type: ignore[no-untyped-def]
        self.deleted.append(document_id)


def _input(document_id: str) -> IndexInput:
    text = "official API example"
    document = KnowledgeDocument(
        document_id=document_id,
        knowledge_base="code",
        source_type="api_documentation",
        title="API",
        content_hash=sha256_text(text),
        source_url="https://example.test",
        retrieved_at="now",
        content=text,
        metadata={"library": "example", "version": "1.0"},
    )
    chunk = ChunkRecord(
        chunk_id="a5ce64a0-8791-5f07-a420-b280472c321d",
        document_id=document_id,
        attachment_key="",
        chunk_index=0,
        text=text,
        text_sha256=sha256_text(text),
        chunk_fingerprint=sha256_text("chunk"),
        token_count=3,
        metadata={"knowledge_base": "code"},
    )
    return IndexInput(document, (chunk,), "v1")


def test_incremental_index_is_idempotent_and_prune_tombstones(tmp_path: Path) -> None:
    pool, index = Pool(), Index()
    service = IncrementalChunkIndexer(
        RagConfig(data_dir=tmp_path, gpu_mode="cpu", embedding_dim=2),
        endpoint_pool=pool,
        sparse_encoder=Sparse(),
        index=index,
    )
    first = service.build([_input("doc-1")], knowledge_base="code")
    second = service.build([_input("doc-1")], knowledge_base="code")
    pruned = service.build([], knowledge_base="code", prune=True)
    assert first.indexed == 1 and second.skipped == 1
    assert pool.calls == 1 and index.replaced == ["doc-1"]
    assert pruned.tombstoned == 1 and index.deleted == ["doc-1"]
    assert service.state.documents()["doc-1"]["active"] == 0


def test_code_intent_and_query_priority() -> None:
    assert classify_code_intent("Traceback: old argument is deprecated") == "debug"

    class Service:
        endpoint_pool = SimpleNamespace(close=lambda: None)
        reranker = None

        def search(self, request):  # type: ignore[no-untyped-def]
            return SearchResponse(
                query=request.query,
                mode="hybrid",
                collection="code",
                embedding_model="m",
                embedding_revision="r",
                embedding_dimension=2,
                reranker_profile="off",
                reranker_model=None,
                reranker_revision=None,
                reranker_fallback=None,
                hits=(
                    SearchHit(
                        point_id="a",
                        score=0.9,
                        payload={"source_type": "source_code", "version": "2"},
                    ),
                    SearchHit(
                        point_id="b",
                        score=0.8,
                        payload={"source_type": "release_note", "version": "1"},
                    ),
                ),
                timings={},
            )

    config = SimpleNamespace(
        rag_config=lambda _kb: RagConfig(
            data_dir=Path("/tmp/test"), gpu_mode="cpu", embedding_dim=2
        )
    )
    response = HubQueryService(config, service_factory=lambda _: Service()).search(
        HubQueryRequest(
            knowledge_base="code",
            query="migrate old API",
            intent="compatibility",
            filters={"target_version": "1", "installed_version": "2"},
        )
    )
    assert response.hits[0].payload["source_type"] == "release_note"
    assert response.hits[0].payload["evidence_role"] == "target_version_evidence"


def test_writing_feedback_changes_subsequent_ranking(tmp_path: Path) -> None:
    WritingFeedbackStore(tmp_path / "state" / "feedback.sqlite3").submit(
        "writing:w2", "useful"
    )

    class Service:
        endpoint_pool = SimpleNamespace(close=lambda: None)
        reranker = None

        def search(self, request):  # type: ignore[no-untyped-def]
            return SearchResponse(
                query=request.query,
                mode="hybrid",
                collection="writing",
                embedding_model="m",
                embedding_revision="r",
                embedding_dimension=2,
                reranker_profile="off",
                reranker_model=None,
                reranker_revision=None,
                reranker_fallback=None,
                hits=(
                    SearchHit(
                        point_id="a",
                        score=0.85,
                        payload={"document_id": "writing:w1", "quality_score": 0.6},
                    ),
                    SearchHit(
                        point_id="b",
                        score=0.8,
                        payload={"document_id": "writing:w2", "quality_score": 0.6},
                    ),
                ),
                timings={},
            )

    config = SimpleNamespace(
        rag_config=lambda _kb: RagConfig(
            data_dir=tmp_path,
            gpu_mode="cpu",
            embedding_dim=2,
        ),
        writing=SimpleNamespace(data_root=tmp_path),
    )
    response = HubQueryService(config, service_factory=lambda _: Service()).search(
        HubQueryRequest(knowledge_base="writing", query="research gap")
    )
    assert response.hits[0].payload["writing_id"] == "writing:w2"
    assert response.hits[0].payload["feedback_adjustment"] == 0.1
    assert response.hits[0].payload["adjusted_quality_score"] == 0.7


def test_writing_section_filter_normalizes_legacy_headings(tmp_path: Path) -> None:
    observed = SimpleNamespace(limit=None)

    class Service:
        endpoint_pool = SimpleNamespace(close=lambda: None)
        reranker = None

        def search(self, request):  # type: ignore[no-untyped-def]
            observed.limit = request.limit
            return SearchResponse(
                query=request.query,
                mode="hybrid",
                collection="writing",
                embedding_model="m",
                embedding_revision="r",
                embedding_dimension=2,
                reranker_profile="off",
                reranker_model=None,
                reranker_revision=None,
                reranker_fallback=None,
                hits=(
                    SearchHit(
                        point_id="intro",
                        score=0.9,
                        payload={
                            "document_id": "w1",
                            "section": "1 Introduction",
                            "source_title": "Visual object detection with image features",
                        },
                    ),
                    SearchHit(
                        point_id="end",
                        score=0.8,
                        payload={"document_id": "w2", "section": "5 Conclusion"},
                    ),
                ),
                timings={},
            )

    config = SimpleNamespace(
        rag_config=lambda _kb: RagConfig(
            data_dir=tmp_path,
            gpu_mode="cpu",
            embedding_dim=2,
        ),
        writing=SimpleNamespace(data_root=tmp_path),
    )
    response = HubQueryService(config, service_factory=lambda _: Service()).search(
        HubQueryRequest(
            knowledge_base="writing",
            query="gap",
            filters={"section": "Introduction", "research_domain": "computer_vision"},
            top_k=1,
            prefetch_limit=3,
        )
    )
    assert observed.limit == 3
    assert [hit.point_id for hit in response.hits] == ["intro"]
    assert response.hits[0].payload["inferred_research_domain"] == ["computer_vision"]
    assert response.hits[0].payload["research_domain_inference"] is True
