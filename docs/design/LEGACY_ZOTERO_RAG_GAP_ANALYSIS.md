# Legacy Zotero RAG gap analysis

## Audited material

The read-only legacy inputs were the workstation starter ZIP and the existing
artifact, model-cache, and Qdrant directories under `/home/lengmo/zotero_rag`.
No legacy source, artifact, state, or collection is modified by the new
pipeline.

## Reused behavior

- Docling conversion with explicit OCR control and PyMuPDF fallback.
- `HybridChunker`, Qwen tokenizer, 768-token chunks, peer merging,
  per-document Parquet artifacts, and deterministic IDs.
- Qwen3-Embedding-4B through TEI, MRL truncation to 1024 dimensions and a
  second L2 normalization.
- FastEmbed `Qdrant/bm25`, named dense/sparse vectors, IDF, RRF, payload
  filters, loopback APIs, and optional reranking.
- Existing embedding/light-reranker cache contents may be copied one way into
  the new model-cache directory.

## Replaced behavior

| Legacy behavior | Unified replacement |
| --- | --- |
| Independent input manifest | Current KnowledgeHub snapshot/delta only |
| One large pipeline script | Pipeline, parser, chunker, embedding, indexing and retrieval modules |
| Per-document JSON state | Transactional pipeline SQLite and audited runs |
| Single parser process | Stable-hash, one-process-per-GPU workers |
| One TEI endpoint | Bounded endpoint pool with failover |
| `gpus: all` | Compose `device_ids` per service |
| Metadata invalidates parsing | Metadata-only payload update by default |
| Concurrent replacement risk | One coordinator owns Qdrant writes |
| No durable delta order | Atomic sequence/predecessor/hash catalog |
| Implicit `.cuda()` | Explicit visible reranker device and OOM batch reduction |

## Compatibility boundary

The legacy manifest is not a production input. Current source sync must publish
`/data/KnowledgeHub/zotero/manifests/documents.jsonl`. Legacy artifacts can be
used for read-only comparison. The old v1 Qdrant collection is untouched; the
new default is `zotero_papers_qwen3_4b_1024_v2`.
