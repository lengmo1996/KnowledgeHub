from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from knowledgehub.code_rag.symbols import SymbolIndex
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
    def __init__(self) -> None:
        self.texts: list[str] = []

    def encode(self, texts):  # type: ignore[no-untyped-def]
        self.texts.extend(texts)
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


def test_candidate_index_requires_a_new_collection_only_on_first_build(
    tmp_path: Path,
) -> None:
    class CandidateIndex(Index):
        def __init__(self) -> None:
            super().__init__()
            self.ensure_modes: list[bool] = []

        def ensure_collection(self, *, require_new: bool = False) -> None:
            self.ensure_modes.append(require_new)

    index = CandidateIndex()
    service = IncrementalChunkIndexer(
        RagConfig(data_dir=tmp_path, gpu_mode="cpu", embedding_dim=2),
        endpoint_pool=Pool(),
        sparse_encoder=Sparse(),
        index=index,
        require_new_collection=True,
    )
    service.build([_input("doc-1")], knowledge_base="code")
    service.build([_input("doc-2")], knowledge_base="code")
    assert index.ensure_modes == [True, False]


def test_incremental_index_uses_sparse_text_without_changing_dense_text(tmp_path: Path) -> None:
    value = _input("doc-sparse")
    chunk = replace(value.chunks[0], sparse_text="lexical aliases")
    pool, sparse, index = Pool(), Sparse(), Index()
    service = IncrementalChunkIndexer(
        RagConfig(data_dir=tmp_path, gpu_mode="cpu", embedding_dim=2),
        endpoint_pool=pool,
        sparse_encoder=sparse,
        index=index,
    )

    result = service.build(
        [replace(value, chunks=(chunk,))],
        knowledge_base="code",
    )

    assert result.status == "success"
    assert sparse.texts == ["lexical aliases"]


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


def test_code_query_merges_user_requested_exact_symbol(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    module = source / "src" / "pkg" / "model.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "class Model:\n    def run(self, value: int = 1) -> int:\n        return value\n",
        encoding="utf-8",
    )
    data_root = tmp_path / "code"
    SymbolIndex(data_root / "state" / "symbols.sqlite3").build("demo", "1.0", source, [module])
    marker = data_root / "sources" / "repositories" / "demo" / "1.0" / "current.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        '{"repository":"owner/demo","commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
        encoding="utf-8",
    )

    class EmptyService:
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
                hits=(),
                timings={},
            )

    config = SimpleNamespace(
        code=SimpleNamespace(data_root=data_root),
        rag_config=lambda _kb: RagConfig(
            data_dir=tmp_path / "rag", gpu_mode="cpu", embedding_dim=2
        ),
    )
    response = HubQueryService(config, service_factory=lambda _: EmptyService()).search(
        HubQueryRequest(
            knowledge_base="code",
            query="How do I call Model.run?",
            filters={"library": "demo", "version": "1.0", "symbol": "Model.run"},
        )
    )
    assert len(response.hits) == 1
    assert response.hits[0].payload["symbol"] == "Model.run"
    assert response.hits[0].payload["qualified_symbol"] == "src.pkg.model.Model.run"
    assert response.hits[0].payload["path"] == "src/pkg/model.py"
    assert response.hits[0].payload["evidence_role"] == "exact_symbol_source"


def test_writing_feedback_changes_subsequent_ranking(tmp_path: Path) -> None:
    WritingFeedbackStore(tmp_path / "state" / "feedback.sqlite3").submit("writing:w2", "useful")

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


def test_writing_query_infers_one_explicit_material_asset_type(tmp_path: Path) -> None:
    observed = SimpleNamespace(asset_type=None)

    class Service:
        endpoint_pool = SimpleNamespace(close=lambda: None)
        reranker = None

        def search(self, request):  # type: ignore[no-untyped-def]
            observed.asset_type = request.writing_asset_type
            return SearchResponse(
                query=request.query,
                mode="sparse",
                collection="writing",
                embedding_model="m",
                embedding_revision="r",
                embedding_dimension=2,
                reranker_profile="off",
                reranker_model=None,
                reranker_revision=None,
                reranker_fallback=None,
                hits=(),
                timings={},
            )

    config = SimpleNamespace(
        rag_config=lambda _kb: RagConfig(data_dir=tmp_path, gpu_mode="cpu", embedding_dim=2),
        writing=SimpleNamespace(data_root=tmp_path),
    )
    HubQueryService(config, service_factory=lambda _: Service()).search(
        HubQueryRequest(
            knowledge_base="writing",
            query="template for theoretical contributions",
            mode="sparse",
        )
    )

    assert observed.asset_type == "template"


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


def test_writing_method_filter_does_not_match_paper_title_substrings(tmp_path: Path) -> None:
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
                        point_id="title",
                        score=0.9,
                        payload={
                            "document_id": "w1",
                            "section": "A Practical Approach to Small Data Learning",
                        },
                    ),
                    SearchHit(
                        point_id="method",
                        score=0.8,
                        payload={"document_id": "w2", "section": "3. Methods"},
                    ),
                ),
                timings={},
            )

    config = SimpleNamespace(
        rag_config=lambda _kb: RagConfig(data_dir=tmp_path, gpu_mode="cpu", embedding_dim=2),
        writing=SimpleNamespace(data_root=tmp_path),
    )
    response = HubQueryService(config, service_factory=lambda _: Service()).search(
        HubQueryRequest(
            knowledge_base="writing",
            query="method overview",
            filters={"section": "Method"},
            top_k=2,
        )
    )
    assert [hit.point_id for hit in response.hits] == ["method"]


def test_literature_demotes_bibliography_unless_explicitly_requested(tmp_path: Path) -> None:
    observed: list[int] = []

    class Service:
        endpoint_pool = SimpleNamespace(close=lambda: None)
        reranker = None

        def search(self, request):  # type: ignore[no-untyped-def]
            observed.append(request.limit)
            return SearchResponse(
                query=request.query,
                mode="hybrid",
                collection="literature",
                embedding_model="m",
                embedding_revision="r",
                embedding_dimension=2,
                reranker_profile="off",
                reranker_model=None,
                reranker_revision=None,
                reranker_fallback=None,
                hits=(
                    SearchHit(
                        point_id="refs",
                        score=0.99,
                        payload={"section_path": ["Paper", "References"]},
                    ),
                    SearchHit(point_id="intro", score=0.9, payload={"section": "Introduction"}),
                    SearchHit(point_id="method", score=0.8, payload={"section": "Methods"}),
                    SearchHit(point_id="result", score=0.7, payload={"section": "Results"}),
                ),
                timings={},
            )

    config = SimpleNamespace(
        rag_config=lambda _kb: RagConfig(data_dir=tmp_path, gpu_mode="cpu", embedding_dim=2)
    )
    service = HubQueryService(config, service_factory=lambda _: Service())
    ordinary = service.search(
        HubQueryRequest(
            knowledge_base="literature",
            query="retrieval augmented generation",
            top_k=3,
            prefetch_limit=10,
        )
    )
    explicit = service.search(
        HubQueryRequest(
            knowledge_base="literature",
            query="references about retrieval augmented generation",
            top_k=3,
            prefetch_limit=10,
        )
    )
    assert observed == [10, 3]
    assert [hit.point_id for hit in ordinary.hits] == ["intro", "method", "result"]
    assert ordinary.warnings == ("bibliography_sections_demoted",)
    assert explicit.hits[0].point_id == "refs"
